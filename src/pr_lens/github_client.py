"""Small, typed GitHub REST API client used by pr-lens."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

DEFAULT_GITHUB_API_URL = "https://api.github.com"
GITHUB_ACCEPT_JSON = "application/vnd.github+json"
GITHUB_ACCEPT_DIFF = "application/vnd.github.v3.diff"
GITHUB_API_VERSION = "2022-11-28"
DEFAULT_COMMENT_AUTHOR = "github-actions[bot]"

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PR_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)/?$"
)
_HASH_REF_RE = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>\d+)$")


class GitHubError(RuntimeError):
    """Raised when GitHub returns an error or cannot be reached."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class IssueComment:
    """A pull request issue comment relevant to comment idempotency."""

    id: int
    body: str
    author: str
    url: str = ""


@dataclass(slots=True)
class PullRequest:
    """The pull request fields pr-lens needs for a review."""

    repository: str
    number: int
    title: str
    body: str = ""
    author: str = "unknown"
    state: str = "open"
    base_ref: str = ""
    head_ref: str = ""
    html_url: str = ""
    draft: bool = False
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0


def parse_pr_reference(target: str, number: int | None = None) -> tuple[str, int]:
    """Resolve a PR URL, ``owner/repo#123``, or ``owner/repo`` plus number.

    Returns ``(repository, pull_request_number)``.
    """
    target = target.strip()
    if not target:
        raise ValueError("A repository or pull request URL is required.")

    url_match = _PR_URL_RE.match(target)
    if url_match:
        url_number = int(url_match.group("number"))
        if number is not None and number != url_number:
            raise ValueError(
                f"Pull request number {number} does not match the URL (#{url_number})."
            )
        repository = f"{url_match.group('owner')}/{url_match.group('repo')}"
        return repository, url_number

    hash_match = _HASH_REF_RE.match(target)
    if hash_match:
        hash_number = int(hash_match.group("number"))
        if number is not None and number != hash_number:
            raise ValueError(
                f"Pull request number {number} does not match the reference (#{hash_number})."
            )
        repository = f"{hash_match.group('owner')}/{hash_match.group('repo')}"
        return repository, hash_number

    if not _REPO_RE.match(target) or any(part in {".", ".."} for part in target.split("/")):
        raise ValueError(
            f"Invalid repository {target!r}. Use 'owner/repo' or a full pull request URL."
        )
    if number is None:
        raise ValueError("A pull request number is required when using 'owner/repo'.")
    if number < 1:
        raise ValueError("Pull request number must be positive.")
    return target, number


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
    text = response.text.strip()
    return text or response.reason_phrase or f"HTTP {response.status_code}"


class GitHubClient:
    """A minimal synchronous client for the GitHub pull request endpoints."""

    def __init__(
        self,
        token: str | None = None,
        *,
        api_url: str = DEFAULT_GITHUB_API_URL,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        headers = {
            "Accept": GITHUB_ACCEPT_JSON,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "pr-lens/0.1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _validate_repository(repository: str) -> None:
        if not _REPO_RE.match(repository):
            raise ValueError(f"Invalid GitHub repository: {repository!r}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = GITHUB_ACCEPT_JSON,
        **kwargs: object,
    ) -> httpx.Response:
        try:
            response = self._http.request(
                method,
                f"{self.api_url}{path}",
                headers={"Accept": accept},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub request failed: {exc}") from exc

        if response.is_error:
            raise GitHubError(
                f"GitHub API error ({response.status_code}): {_error_message(response)}",
                status_code=response.status_code,
            )
        return response

    def get_pull_request(self, repository: str, number: int) -> PullRequest:
        """Fetch pull request metadata."""
        self._validate_repository(repository)
        response = self._request("GET", f"/repos/{repository}/pulls/{number}")
        try:
            data = response.json()
        except ValueError as exc:
            raise GitHubError("GitHub returned an invalid JSON response.") from exc
        if not isinstance(data, dict):
            raise GitHubError("GitHub returned an unexpected pull request payload.")

        base = data.get("base") or {}
        head = data.get("head") or {}
        user = data.get("user") or {}
        return PullRequest(
            repository=repository,
            number=number,
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            author=str(user.get("login") or "unknown") if isinstance(user, dict) else "unknown",
            state=str(data.get("state") or "open"),
            base_ref=str(base.get("ref") or "") if isinstance(base, dict) else "",
            head_ref=str(head.get("ref") or "") if isinstance(head, dict) else "",
            html_url=str(data.get("html_url") or f"https://github.com/{repository}/pull/{number}"),
            draft=bool(data.get("draft")),
            additions=int(data.get("additions") or 0),
            deletions=int(data.get("deletions") or 0),
            changed_files=int(data.get("changed_files") or 0),
        )

    def get_diff(self, repository: str, number: int) -> str:
        """Fetch the pull request's unified diff."""
        self._validate_repository(repository)
        response = self._request(
            "GET",
            f"/repos/{repository}/pulls/{number}",
            accept=GITHUB_ACCEPT_DIFF,
        )
        return response.text

    def post_comment(self, repository: str, number: int, body: str) -> str:
        """Post a review comment and return its HTML URL when available."""
        self._validate_repository(repository)
        response = self._request(
            "POST",
            f"/repos/{repository}/issues/{number}/comments",
            json={"body": body},
        )
        return _comment_url(response)

    def list_comments(
        self,
        repository: str,
        number: int,
        *,
        per_page: int = 100,
    ) -> list[IssueComment]:
        """List issue comments, following pagination until GitHub runs out."""
        self._validate_repository(repository)
        comments: list[IssueComment] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/repos/{repository}/issues/{number}/comments",
                params={"per_page": per_page, "page": page},
            )
            try:
                data = response.json()
            except ValueError as exc:
                raise GitHubError("GitHub returned invalid comment JSON.") from exc
            if not isinstance(data, list):
                raise GitHubError("GitHub returned an unexpected comment payload.")

            for item in data:
                comment = _parse_comment(item)
                if comment is not None:
                    comments.append(comment)

            if len(data) < per_page:
                break
            page += 1
        return comments

    def update_comment(self, repository: str, comment_id: int, body: str) -> str:
        """Replace the body of an existing issue comment."""
        self._validate_repository(repository)
        response = self._request(
            "PATCH",
            f"/repos/{repository}/issues/comments/{comment_id}",
            json={"body": body},
        )
        return _comment_url(response)

    def find_review_comment(
        self,
        repository: str,
        number: int,
        *,
        marker: str,
        expected_author: str,
        per_page: int = 100,
    ) -> IssueComment | None:
        """Find a marked comment written by the expected account."""
        if not marker:
            raise ValueError("A comment marker is required.")
        if not expected_author:
            raise ValueError("An expected comment author is required.")
        expected = expected_author.casefold()
        for comment in self.list_comments(repository, number, per_page=per_page):
            if marker in comment.body and comment.author.casefold() == expected:
                return comment
        return None

    def upsert_review_comment(
        self,
        repository: str,
        number: int,
        body: str,
        *,
        marker: str,
        expected_author: str,
        per_page: int = 100,
    ) -> tuple[str, str]:
        """Update a matching marked comment, or create a new one.

        Returns ``(action, url)`` where ``action`` is either ``"updated"`` or
        ``"created"``.
        """
        existing = self.find_review_comment(
            repository,
            number,
            marker=marker,
            expected_author=expected_author,
            per_page=per_page,
        )
        if existing is not None:
            return "updated", self.update_comment(repository, existing.id, body)
        return "created", self.post_comment(repository, number, body)


def _comment_url(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("html_url") or "")


def _parse_comment(item: object) -> IssueComment | None:
    if not isinstance(item, dict):
        return None
    try:
        comment_id = int(item.get("id"))
    except (TypeError, ValueError):
        return None
    user = item.get("user")
    author = "unknown"
    if isinstance(user, dict):
        author = str(user.get("login") or "unknown")
    return IssueComment(
        id=comment_id,
        body=str(item.get("body") or ""),
        author=author,
        url=str(item.get("html_url") or ""),
    )
