"""OpenAI-compatible LLM client and review orchestration."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from pr_lens.diff_parser import ParsedDiff
from pr_lens.github_client import PullRequest
from pr_lens.prompts import PromptBundle, build_prompt_bundle

DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_MAX_TOKENS = 2_000


class LLMError(RuntimeError):
    """Raised when the LLM endpoint fails or returns an unusable response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class LLMResponse:
    """A successful chat completion."""

    content: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(slots=True)
class ReviewResult:
    """A formatted-agnostic review ready for output."""

    content: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    diff_truncated: bool = False


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
    text = response.text.strip()
    return text[:500] if text else response.reason_phrase or f"HTTP {response.status_code}"


def _extract_content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise LLMError("LLM returned an unexpected response payload.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("LLM response did not contain any choices.")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMError("LLM returned an invalid choice.")
    message = first.get("message")
    if not isinstance(message, dict):
        raise LLMError("LLM response did not contain a message.")
    content = message.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        content = "".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise LLMError("LLM returned an empty response.")
    return content


class LLMClient:
    """A minimal client for OpenAI-compatible ``/chat/completions`` APIs."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_LLM_BASE_URL,
        model: str = DEFAULT_LLM_MODEL,
        timeout: float = 120.0,
        temperature: float = 0.2,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("An LLM API key is required.")
        if not base_url.strip():
            raise ValueError("An LLM base URL is required.")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "pr-lens/0.1",
            },
            transport=transport,
        )

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
    ) -> LLMResponse:
        """Request a chat completion and validate the response shape."""
        payload = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        try:
            response = self._http.post(self._endpoint, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        if response.is_error:
            raise LLMError(
                f"LLM API error ({response.status_code}): {_error_message(response)}",
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError("LLM returned invalid JSON.") from exc

        content = _extract_content(data)
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        response_model = data.get("model") if isinstance(data, dict) else None
        return LLMResponse(
            content=content.strip(),
            model=str(response_model or model or self.model),
            prompt_tokens=_as_int(usage.get("prompt_tokens")),
            completion_tokens=_as_int(usage.get("completion_tokens")),
        )


def review_diff(
    pull_request: PullRequest,
    parsed: ParsedDiff,
    diff_text: str,
    client: LLMClient,
    *,
    max_diff_chars: int = 60_000,
    max_description_chars: int = 4_000,
) -> ReviewResult:
    """Build prompts, ask the model for a review, and return the result."""
    bundle: PromptBundle = build_prompt_bundle(
        pull_request,
        parsed,
        diff_text,
        max_diff_chars=max_diff_chars,
        max_description_chars=max_description_chars,
    )
    response = client.complete(bundle.system, bundle.user)
    return ReviewResult(
        content=response.content,
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        diff_truncated=bundle.truncated,
    )
