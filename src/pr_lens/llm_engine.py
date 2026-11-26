"""OpenAI-compatible LLM client and review orchestration."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from pr_lens.diff_parser import FileDiff, ParsedDiff
from pr_lens.github_client import PullRequest
from pr_lens.prompts import PromptBundle, build_prompt_bundle

DEFAULT_LLM_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_LLM_MODEL = "gemini-3.5-flash-lite"
DEFAULT_MAX_TOKENS = 4_000
MIN_MAX_TOKENS = 1
MAX_MAX_TOKENS = 100_000


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
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class QualityEstimate:
    """A validated AI-generated quality estimate."""

    verdict: str
    score: int
    reasons: str


@dataclass(slots=True)
class ReviewResult:
    """A formatted-agnostic review ready for output."""

    content: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    diff_truncated: bool = False
    skipped_files: list[str] = field(default_factory=list)
    unreviewable_files: list[str] = field(default_factory=list)
    quality: QualityEstimate | None = None

    @property
    def is_partial(self) -> bool:
        """Whether any reviewable content was unavailable to the model."""
        return (
            self.diff_truncated
            or bool(self.skipped_files)
            or bool(self.unreviewable_files)
        )


VALID_VERDICTS = ("Looks good", "Needs attention", "High risk")
_QUALITY_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?AI\s*estimate\s*:\s*"
    r"(?P<verdict>[^|]+?)\s*\|\s*"
    r"(?P<score>\d+)\s*(?:/\s*5)?\s*\|\s*"
    r"(?P<reasons>.+?)\s*$",
    re.IGNORECASE,
)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _redact(text: str, secret: str | None) -> str:
    """Remove a configured credential from an error message."""
    return text.replace(secret, "[REDACTED]") if secret else text


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


def _extract_choice(payload: object) -> tuple[str, str | None]:
    """Return the content and finish reason from the first choice."""
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
    if not isinstance(content, str):
        raise LLMError("LLM response did not contain text content.")
    finish_reason = first.get("finish_reason")
    if not isinstance(finish_reason, str) or not finish_reason:
        finish_reason = None
    return content, finish_reason


def _is_truncated(finish_reason: str | None) -> bool:
    return finish_reason is not None and finish_reason.lower() in {
        "length",
        "max_tokens",
        "max_output_tokens",
    }


def parse_quality_estimate(content: str) -> tuple[str, QualityEstimate | None]:
    """Extract and validate an estimate only from the first non-empty line."""
    lines = content.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return "", None

    probe = lines[first].strip().lstrip("-* ").strip()
    if not probe.lower().startswith("ai estimate:"):
        return content.strip(), None

    cleaned = "\n".join(lines[:first] + lines[first + 1 :]).strip()
    match = _QUALITY_LINE_RE.match(probe)
    if match is None:
        return cleaned, None

    verdict_text = " ".join(match.group("verdict").split())
    verdict = next(
        (
            allowed
            for allowed in VALID_VERDICTS
            if allowed.casefold() == verdict_text.casefold()
        ),
        None,
    )
    score = int(match.group("score"))
    reasons = " ".join(match.group("reasons").split())
    if verdict is None or not 1 <= score <= 5 or not reasons:
        return cleaned, None
    return cleaned, QualityEstimate(verdict=verdict, score=score, reasons=reasons[:300])


def _parse_retry_after(value: str) -> float | None:
    """Parse a Retry-After header expressed as seconds or an HTTP date."""
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
    return max(0.0, seconds)


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
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        max_retry_wait: float = 10.0,
        retry_sleep: Callable[[float], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("An LLM API key is required.")
        if not base_url.strip():
            raise ValueError("An LLM base URL is required.")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative.")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must not be negative.")
        if max_retry_wait < 0:
            raise ValueError("max_retry_wait must not be negative.")
        self._api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._max_retry_wait = max_retry_wait
        self._sleep = retry_sleep or time.sleep
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

    def _backoff(self, attempt: int) -> float:
        return min(self._retry_backoff * (2**attempt), self._max_retry_wait)

    def _retry_wait(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                seconds = _parse_retry_after(retry_after)
                if seconds is not None:
                    return min(seconds, self._max_retry_wait)
        return self._backoff(attempt)

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
    ) -> LLMResponse:
        """Request a chat completion, retrying transient failures."""
        payload = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        attempt = 0
        while True:
            try:
                response = self._http.post(self._endpoint, json=payload)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise LLMError(
                        _redact(
                            f"LLM request timed out after {attempt + 1} attempt(s): {exc}",
                            self._api_key,
                        )
                    ) from exc
                self._sleep(self._retry_wait(attempt, None))
                attempt += 1
                continue
            except httpx.HTTPError as exc:
                raise LLMError(
                    _redact(f"LLM request failed: {exc}", self._api_key)
                ) from exc

            status = response.status_code
            retryable = status == 429 or 500 <= status <= 599
            if retryable and attempt < self._max_retries:
                self._sleep(self._retry_wait(attempt, response))
                attempt += 1
                continue

            if response.is_error:
                attempts = f" after {attempt + 1} attempts" if retryable else ""
                message = _redact(_error_message(response), self._api_key)
                raise LLMError(
                    f"LLM API error ({status}){attempts}: {message}",
                    status_code=status,
                )
            break

        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError("LLM returned invalid JSON.") from exc

        content, finish_reason = _extract_choice(data)
        if _is_truncated(finish_reason):
            raise LLMError(
                "LLM response was truncated at the token limit "
                f"(finish_reason={finish_reason!r}); refusing to present or post "
                "an incomplete review. Increase --max-tokens or "
                "PR_LENS_MAX_TOKENS."
            )
        if finish_reason != "stop":
            reason = finish_reason if finish_reason is not None else "missing"
            raise LLMError(
                "LLM response did not complete successfully "
                f"(finish_reason={reason!r}); refusing to present or post an "
                "incomplete review. Expected 'stop'."
            )
        if not content.strip():
            raise LLMError("LLM returned an empty response.")
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        response_model = data.get("model") if isinstance(data, dict) else None
        return LLMResponse(
            content=content.strip(),
            model=str(response_model or model or self.model),
            prompt_tokens=_as_int(usage.get("prompt_tokens")),
            completion_tokens=_as_int(usage.get("completion_tokens")),
            finish_reason=finish_reason,
        )


def _has_reviewable_text(file: FileDiff) -> bool:
    """Return whether a changed file has text the model could actually review."""
    if file.is_binary:
        return False
    return any(line.content.strip() for hunk in file.hunks for line in hunk.lines)


def review_diff(
    pull_request: PullRequest,
    parsed: ParsedDiff,
    diff_text: str,
    client: LLMClient,
    *,
    max_diff_chars: int = 60_000,
    max_description_chars: int = 4_000,
    skipped_files: tuple[str, ...] = (),
) -> ReviewResult:
    """Build prompts, ask the model for a review, and return the result."""
    bundle: PromptBundle = build_prompt_bundle(
        pull_request,
        parsed,
        diff_text,
        max_diff_chars=max_diff_chars,
        max_description_chars=max_description_chars,
        skipped_files=skipped_files,
    )
    response = client.complete(bundle.system, bundle.user)
    content, quality = parse_quality_estimate(response.content)
    unreviewable_files = [
        file.display_path for file in parsed.files if not _has_reviewable_text(file)
    ]
    return ReviewResult(
        content=content,
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        diff_truncated=bundle.truncated,
        skipped_files=list(skipped_files),
        unreviewable_files=unreviewable_files,
        quality=quality,
    )
