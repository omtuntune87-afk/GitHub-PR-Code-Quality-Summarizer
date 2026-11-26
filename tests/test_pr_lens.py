"""Offline tests for pr-lens."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

import pr_lens.cli as cli_module
from pr_lens import __version__
from pr_lens.cli import cli as cli_command
from pr_lens.diff_parser import parse_diff
from pr_lens.formatter import format_review, truncate_comment
from pr_lens.github_client import (
    GitHubClient,
    GitHubError,
    PullRequest,
    parse_pr_reference,
)
from pr_lens.llm_engine import (
    LLMClient,
    LLMError,
    LLMResponse,
    ReviewResult,
    review_diff,
)
from pr_lens.prompts import build_prompt_bundle, truncate_diff

SIMPLE_DIFF = """\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 import os
-old = 1
+new = 2
+extra = 3
 print("done")
"""

NEW_FILE_DIFF = """\
diff --git a/new.txt b/new.txt
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+hello
+world
"""

DELETED_FILE_DIFF = """\
diff --git a/old.txt b/old.txt
deleted file mode 100644
index 1111111..0000000
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-goodbye
-world
"""

RENAME_DIFF = """\
diff --git a/old.py b/new.py
similarity index 100%
rename from old.py
rename to new.py
"""

BINARY_DIFF = """\
diff --git a/image.png b/image.png
index 1111111..2222222 100644
Binary files a/image.png and b/image.png differ
"""


@pytest.fixture
def pull_request() -> PullRequest:
    return PullRequest(
        repository="octo/demo",
        number=7,
        title="Add a feature",
        body="This PR adds a new feature and updates tests.",
        author="alice",
        state="open",
        base_ref="main",
        head_ref="feature",
        html_url="https://github.com/octo/demo/pull/7",
        additions=3,
        deletions=1,
        changed_files=1,
    )


@pytest.fixture
def review_result() -> ReviewResult:
    return ReviewResult(
        content="## Summary\nAdds a feature.\n\n## Key changes\n- Added `new`.",
        model="test-model",
        prompt_tokens=100,
        completion_tokens=40,
    )


def test_version_is_exposed() -> None:
    assert __version__ == "0.1.0"


# ---------------------------------------------------------------------------
# Diff parser
# ---------------------------------------------------------------------------


def test_parse_modified_file() -> None:
    parsed = parse_diff(SIMPLE_DIFF)
    assert parsed.changed_files == 1
    assert parsed.additions == 2
    assert parsed.deletions == 1
    assert parsed.summary_line() == "1 file, +2 / -1 lines"

    file = parsed.files[0]
    assert file.old_path == "app.py"
    assert file.new_path == "app.py"
    assert file.status == "modified"
    assert not file.is_binary
    assert len(file.hunks) == 1

    hunk = file.hunks[0]
    assert (hunk.old_start, hunk.old_count) == (1, 3)
    assert (hunk.new_start, hunk.new_count) == (1, 4)
    assert hunk.section == ""


def test_parse_hunk_line_numbers_and_kinds() -> None:
    parsed = parse_diff(SIMPLE_DIFF)
    lines = parsed.files[0].hunks[0].lines
    assert [(line.kind, line.content) for line in lines] == [
        ("context", "import os"),
        ("deletion", "old = 1"),
        ("addition", "new = 2"),
        ("addition", "extra = 3"),
        ("context", 'print("done")'),
    ]
    assert lines[0].old_lineno == 1
    assert lines[0].new_lineno == 1
    assert lines[1].old_lineno == 2
    assert lines[1].new_lineno is None
    assert lines[2].old_lineno is None
    assert lines[2].new_lineno == 2
    assert lines[4].old_lineno == 3
    assert lines[4].new_lineno == 4


def test_parse_new_file() -> None:
    parsed = parse_diff(NEW_FILE_DIFF)
    file = parsed.files[0]
    assert file.status == "added"
    assert file.old_path is None
    assert file.new_path == "new.txt"
    assert file.additions == 2
    assert file.deletions == 0


def test_parse_deleted_file() -> None:
    parsed = parse_diff(DELETED_FILE_DIFF)
    file = parsed.files[0]
    assert file.status == "deleted"
    assert file.old_path == "old.txt"
    assert file.new_path is None
    assert file.additions == 0
    assert file.deletions == 2


def test_parse_rename_without_hunks() -> None:
    parsed = parse_diff(RENAME_DIFF)
    file = parsed.files[0]
    assert file.status == "renamed"
    assert file.old_path == "old.py"
    assert file.new_path == "new.py"
    assert file.display_path == "old.py -> new.py"
    assert file.hunks == []
    assert file.additions == 0
    assert file.deletions == 0


def test_parse_binary_file() -> None:
    parsed = parse_diff(BINARY_DIFF)
    file = parsed.files[0]
    assert file.is_binary
    assert file.status == "modified"
    assert file.hunks == []


def test_parse_added_binary_file_has_no_old_path() -> None:
    diff = """\
diff --git a/logo.png b/logo.png
new file mode 100644
index 0000000..1111111
Binary files /dev/null and b/logo.png differ
"""
    file = parse_diff(diff).files[0]
    assert file.status == "added"
    assert file.old_path is None
    assert file.new_path == "logo.png"
    assert file.is_binary


def test_parse_deleted_binary_file_has_no_new_path() -> None:
    diff = """\
diff --git a/logo.png b/logo.png
deleted file mode 100644
index 1111111..0000000
Binary files a/logo.png and /dev/null differ
"""
    file = parse_diff(diff).files[0]
    assert file.status == "deleted"
    assert file.old_path == "logo.png"
    assert file.new_path is None
    assert file.is_binary


def test_parse_multiple_files_and_stats() -> None:
    parsed = parse_diff(SIMPLE_DIFF + NEW_FILE_DIFF + RENAME_DIFF)
    assert parsed.changed_files == 3
    assert parsed.additions == 4
    assert parsed.deletions == 1
    assert parsed.summary_line() == "3 files, +4 / -1 lines"
    assert [file.status for file in parsed.files] == ["modified", "added", "renamed"]


def test_parse_empty_diff() -> None:
    parsed = parse_diff("")
    assert parsed.is_empty
    assert parsed.changed_files == 0
    assert parsed.additions == 0
    assert parsed.deletions == 0
    assert parsed.summary_line() == "0 files, +0 / -0 lines"


def test_file_by_path_uses_old_and_new_paths() -> None:
    parsed = parse_diff(RENAME_DIFF)
    assert parsed.file_by_path("old.py") is parsed.files[0]
    assert parsed.file_by_path("new.py") is parsed.files[0]
    assert parsed.file_by_path("missing.py") is None


def test_parse_ignores_no_newline_marker() -> None:
    diff = """\
diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""
    parsed = parse_diff(diff)
    file = parsed.files[0]
    assert file.additions == 1
    assert file.deletions == 1
    assert [line.content for line in file.hunks[0].lines] == ["old", "new"]


def test_parse_paths_with_spaces() -> None:
    diff = """\
diff --git a/docs/my file.md b/docs/my file.md
index 1111111..2222222 100644
--- a/docs/my file.md
+++ b/docs/my file.md
@@ -1 +1 @@
-old
+new
"""
    parsed = parse_diff(diff)
    assert parsed.files[0].old_path == "docs/my file.md"
    assert parsed.files[0].new_path == "docs/my file.md"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def test_build_prompt_bundle_contains_metadata_and_diff(
    pull_request: PullRequest,
) -> None:
    parsed = parse_diff(SIMPLE_DIFF)
    bundle = build_prompt_bundle(pull_request, parsed, SIMPLE_DIFF)
    assert bundle.truncated is False
    assert "octo/demo" in bundle.user
    assert "#7" in bundle.user
    assert "Add a feature" in bundle.user
    assert "@alice" in bundle.user
    assert "app.py" in bundle.user
    assert "+new = 2" in bundle.user
    assert "<diff>" in bundle.user
    assert "untrusted data" in bundle.system
    assert "## Summary" in bundle.system


def test_build_prompt_bundle_clips_description(pull_request: PullRequest) -> None:
    pull_request.body = "x" * 500
    parsed = parse_diff(SIMPLE_DIFF)
    bundle = build_prompt_bundle(pull_request, parsed, SIMPLE_DIFF, max_description_chars=50)
    assert "x" * 50 in bundle.user
    assert "Description truncated" in bundle.user


def test_build_prompt_bundle_marks_truncated_diff(pull_request: PullRequest) -> None:
    parsed = parse_diff(SIMPLE_DIFF)
    bundle = build_prompt_bundle(pull_request, parsed, SIMPLE_DIFF, max_diff_chars=40)
    assert bundle.truncated is True
    assert "Diff truncated" in bundle.diff_text
    assert "partial diff" in bundle.system


def test_truncate_diff_keeps_small_input_unchanged() -> None:
    text = "diff --git a/a b/a\n"
    result, truncated = truncate_diff(text, 100)
    assert result == text
    assert truncated is False


def test_truncate_diff_prefers_file_boundary() -> None:
    first = SIMPLE_DIFF
    second = NEW_FILE_DIFF.replace("new.txt", "second.txt")
    combined = first + second
    result, truncated = truncate_diff(combined, len(first) + 10)
    assert truncated is True
    assert "app.py" in result
    assert "second.txt" not in result
    assert "Diff truncated" in result


def test_truncate_diff_with_zero_budget() -> None:
    result, truncated = truncate_diff(SIMPLE_DIFF, 0)
    assert truncated is True
    assert "omitted" in result


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


def test_format_review_contains_metadata(
    review_result: ReviewResult, pull_request: PullRequest
) -> None:
    parsed = parse_diff(SIMPLE_DIFF)
    comment = format_review(review_result, pull_request, parsed)
    assert "PR Lens review" in comment
    assert "> **Add a feature**" in comment
    assert "octo/demo#7" in comment
    assert "@alice" in comment
    assert "`test-model`" in comment
    assert "1 file, +2 / -1 lines" in comment
    assert "## Summary" in comment
    assert "Generated by pr-lens" in comment


def test_format_review_can_omit_footer(
    review_result: ReviewResult, pull_request: PullRequest
) -> None:
    comment = format_review(review_result, pull_request, include_footer=False)
    assert "Generated by pr-lens" not in comment


def test_format_review_notes_truncated_diff(
    review_result: ReviewResult, pull_request: PullRequest
) -> None:
    review_result.diff_truncated = True
    comment = format_review(review_result, pull_request)
    assert "The diff was truncated" in comment


def test_format_review_handles_empty_content(
    review_result: ReviewResult, pull_request: PullRequest
) -> None:
    review_result.content = "   "
    comment = format_review(review_result, pull_request)
    assert "model returned an empty review" in comment


def test_format_review_collapses_multiline_title(
    review_result: ReviewResult, pull_request: PullRequest
) -> None:
    pull_request.title = "Line one\n\nLine two"
    comment = format_review(review_result, pull_request)
    assert "> **Line one Line two**" in comment


def test_format_review_truncates_long_comments(
    review_result: ReviewResult, pull_request: PullRequest
) -> None:
    review_result.content = "word " * 10_000
    comment = format_review(review_result, pull_request, max_chars=1_000)
    assert len(comment) <= 1_000
    assert "Review truncated" in comment


def test_truncate_comment_with_tiny_budget() -> None:
    assert truncate_comment("x" * 100, 10) == "x" * 10
    assert truncate_comment("short", 100) == "short"
    assert truncate_comment("short", 0) == ""


# ---------------------------------------------------------------------------
# GitHub client
# ---------------------------------------------------------------------------


def _github_client(handler: object, token: str | None = "token") -> GitHubClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return GitHubClient(token=token, transport=transport)


def test_get_pull_request_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://api.github.com/repos/octo/demo/pulls/7"
        assert request.headers["Authorization"] == "Bearer token"
        assert request.headers["Accept"] == "application/vnd.github+json"
        return httpx.Response(
            200,
            json={
                "title": "Add a feature",
                "body": "Body text",
                "state": "open",
                "draft": False,
                "additions": 3,
                "deletions": 1,
                "changed_files": 1,
                "html_url": "https://github.com/octo/demo/pull/7",
                "user": {"login": "alice"},
                "base": {"ref": "main"},
                "head": {"ref": "feature"},
            },
        )

    with _github_client(handler) as github:
        pr = github.get_pull_request("octo/demo", 7)

    assert pr.repository == "octo/demo"
    assert pr.number == 7
    assert pr.title == "Add a feature"
    assert pr.author == "alice"
    assert pr.base_ref == "main"
    assert pr.head_ref == "feature"
    assert pr.additions == 3
    assert pr.deletions == 1


def test_get_pull_request_defaults_missing_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _github_client(handler, token=None) as github:
        pr = github.get_pull_request("octo/demo", 7)

    assert pr.title == ""
    assert pr.author == "unknown"
    assert pr.html_url == "https://github.com/octo/demo/pull/7"
    assert pr.draft is False


def test_get_diff_requests_diff_media_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "application/vnd.github.v3.diff"
        return httpx.Response(200, text=SIMPLE_DIFF)

    with _github_client(handler) as github:
        assert github.get_diff("octo/demo", 7) == SIMPLE_DIFF


def test_post_comment_sends_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://api.github.com/repos/octo/demo/issues/7/comments"
        payload = json.loads(request.content)
        assert payload == {"body": "hello"}
        return httpx.Response(201, json={"html_url": "https://github.com/comment/1"})

    with _github_client(handler) as github:
        url = github.post_comment("octo/demo", 7, "hello")

    assert url == "https://github.com/comment/1"


def test_github_error_includes_api_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with _github_client(handler) as github, pytest.raises(GitHubError) as exc_info:
        github.get_pull_request("octo/demo", 7)

    assert exc_info.value.status_code == 404
    assert "Not Found" in str(exc_info.value)


def test_github_network_error_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    with (
        _github_client(handler) as github,
        pytest.raises(GitHubError, match="GitHub request failed"),
    ):
        github.get_diff("octo/demo", 7)


def test_github_rejects_invalid_repository() -> None:
    with (
        _github_client(lambda request: httpx.Response(200)) as github,
        pytest.raises(ValueError, match="Invalid GitHub repository"),
    ):
        github.get_diff("not a repo", 7)


@pytest.mark.parametrize(
    ("target", "number", "expected"),
    [
        ("octo/demo", 7, ("octo/demo", 7)),
        ("octo/demo#7", None, ("octo/demo", 7)),
        ("https://github.com/octo/demo/pull/7", None, ("octo/demo", 7)),
        ("github.com/octo/demo/pull/7", None, ("octo/demo", 7)),
        ("https://github.com/octo/demo/pull/7", 7, ("octo/demo", 7)),
    ],
)
def test_parse_pr_reference(target: str, number: int | None, expected: tuple[str, int]) -> None:
    assert parse_pr_reference(target, number) == expected


@pytest.mark.parametrize(
    "target, number",
    [
        ("", None),
        ("octo/demo", None),
        ("octo/demo", 0),
        ("bogus", 7),
        ("https://github.com/octo/demo/pull/7", 8),
        ("octo/demo#7", 8),
    ],
)
def test_parse_pr_reference_rejects_invalid_input(target: str, number: int | None) -> None:
    with pytest.raises(ValueError):
        parse_pr_reference(target, number)


# ---------------------------------------------------------------------------
# LLM client and orchestration
# ---------------------------------------------------------------------------


def test_llm_complete_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://llm.test/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer secret"
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["content"] == "hello"
        return httpx.Response(
            200,
            json={
                "model": "test-model-2024",
                "choices": [{"message": {"content": "the review"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )

    with LLMClient(
        "secret",
        base_url="https://llm.test/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
    ) as client:
        response = client.complete("system prompt", "hello")

    assert response.content == "the review"
    assert response.model == "test-model-2024"
    assert response.prompt_tokens == 12
    assert response.completion_tokens == 5
    assert response.finish_reason is None


def test_llm_rejects_length_truncated_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "## Summary\npartial review"},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    with (
        LLMClient("secret", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(LLMError, match="truncated"),
    ):
        client.complete("system", "user")


def test_llm_rejects_max_tokens_truncated_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "partial review"},
                        "finish_reason": "MAX_TOKENS",
                    }
                ]
            },
        )

    with (
        LLMClient("secret", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(LLMError, match="token limit"),
    ):
        client.complete("system", "user")


def test_llm_accepts_stop_finish_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "complete review"}, "finish_reason": "stop"}
                ]
            },
        )

    with LLMClient("secret", transport=httpx.MockTransport(handler)) as client:
        response = client.complete("system", "user")

    assert response.content == "complete review"
    assert response.finish_reason == "stop"


def test_llm_accepts_content_parts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [{"type": "text", "text": "part one "}, {"text": "two"}]
                        }
                    }
                ]
            },
        )

    with LLMClient("secret", transport=httpx.MockTransport(handler)) as client:
        response = client.complete("system", "user")

    assert response.content == "part one two"


def test_llm_error_includes_provider_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    with (
        LLMClient(
            "secret",
            transport=httpx.MockTransport(handler),
            retry_sleep=lambda _: None,
        ) as client,
        pytest.raises(LLMError) as exc_info,
    ):
        client.complete("system", "user")

    assert exc_info.value.status_code == 429
    assert "slow down" in str(exc_info.value)
    assert "after 4 attempts" in str(exc_info.value)


def _success_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": "complete"}}]},
    )


def test_llm_retries_rate_limit_then_succeeds() -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return _success_response()

    with LLMClient(
        "secret",
        transport=httpx.MockTransport(handler),
        retry_backoff=0.5,
        retry_sleep=waits.append,
    ) as client:
        response = client.complete("system", "user")

    assert response.content == "complete"
    assert calls == 2
    assert waits == [0.5]


def test_llm_retries_server_error_then_succeeds() -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return _success_response()

    with LLMClient(
        "secret",
        transport=httpx.MockTransport(handler),
        retry_backoff=0.5,
        retry_sleep=waits.append,
    ) as client:
        response = client.complete("system", "user")

    assert response.content == "complete"
    assert calls == 3
    assert waits == [0.5, 1.0]


def test_llm_retries_timeout_then_succeeds() -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("timed out")
        return _success_response()

    with LLMClient(
        "secret",
        transport=httpx.MockTransport(handler),
        retry_backoff=0.5,
        retry_sleep=waits.append,
    ) as client:
        response = client.complete("system", "user")

    assert response.content == "complete"
    assert calls == 2
    assert waits == [0.5]


def test_llm_stops_retrying_after_limit() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": {"message": "unavailable"}})

    with (
        LLMClient(
            "secret",
            transport=httpx.MockTransport(handler),
            max_retries=2,
            retry_backoff=0.25,
            retry_sleep=lambda _: None,
        ) as client,
        pytest.raises(LLMError, match="after 3 attempts"),
    ):
        client.complete("system", "user")

    assert calls == 3


def test_llm_honors_and_caps_retry_after() -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "999"},
                json={"error": {"message": "slow down"}},
            )
        return _success_response()

    with LLMClient(
        "secret",
        transport=httpx.MockTransport(handler),
        retry_backoff=0.5,
        max_retry_wait=10.0,
        retry_sleep=waits.append,
    ) as client:
        response = client.complete("system", "user")

    assert response.content == "complete"
    assert waits == [10.0]


def test_llm_does_not_retry_other_client_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    with (
        LLMClient(
            "secret",
            transport=httpx.MockTransport(handler),
            retry_sleep=lambda _: None,
        ) as client,
        pytest.raises(LLMError) as exc_info,
    ):
        client.complete("system", "user")

    assert calls == 1
    assert exc_info.value.status_code == 400
    assert "after" not in str(exc_info.value)


def test_llm_invalid_json_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with (
        LLMClient("secret", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(LLMError, match="invalid JSON"),
    ):
        client.complete("system", "user")


def test_llm_empty_content_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})

    with (
        LLMClient("secret", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(LLMError, match="empty response"),
    ):
        client.complete("system", "user")


def test_llm_requires_api_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        LLMClient("")


def test_review_diff_uses_bundle_and_returns_result(pull_request: PullRequest) -> None:
    class CapturingClient:
        def __init__(self) -> None:
            self.system = ""
            self.user = ""

        def complete(self, system: str, user: str) -> LLMResponse:
            self.system = system
            self.user = user
            return LLMResponse(
                content="## Summary\nLooks good.",
                model="captured-model",
                prompt_tokens=10,
                completion_tokens=4,
            )

    parsed = parse_diff(SIMPLE_DIFF)
    client = CapturingClient()
    result = review_diff(
        pull_request,
        parsed,
        SIMPLE_DIFF,
        client,  # type: ignore[arg-type]
        max_diff_chars=30,
    )

    assert result.content == "## Summary\nLooks good."
    assert result.model == "captured-model"
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 4
    assert result.diff_truncated is True
    assert "Add a feature" in client.user
    assert "untrusted data" in client.system


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class FakeGitHubClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.token = kwargs.get("token")
        self.api_url = kwargs.get("api_url")
        self.posted: list[tuple[str, int, str]] = []
        self.diff_text = SIMPLE_DIFF
        self.next_error: Exception | None = None

    def __enter__(self) -> FakeGitHubClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def get_pull_request(self, repository: str, number: int) -> PullRequest:
        if self.next_error is not None:
            raise self.next_error
        return PullRequest(
            repository=repository,
            number=number,
            title="Add a feature",
            body="Body",
            author="alice",
            base_ref="main",
            head_ref="feature",
            html_url=f"https://github.com/{repository}/pull/{number}",
            additions=2,
            deletions=1,
            changed_files=1,
        )

    def get_diff(self, repository: str, number: int) -> str:
        return self.diff_text

    def post_comment(self, repository: str, number: int, body: str) -> str:
        self.posted.append((repository, number, body))
        return "https://github.com/comment/1"


class FakeLLMClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.api_key = args[0] if args else None
        self.model = kwargs.get("model")
        self.base_url = kwargs.get("base_url")
        self.max_tokens = kwargs.get("max_tokens")
        self.next_error: Exception | None = None

    def __enter__(self) -> FakeLLMClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def complete(self, system: str, user: str) -> LLMResponse:
        if self.next_error is not None:
            raise self.next_error
        return LLMResponse(
            content="## Summary\nAdds a feature.\n\n## Key changes\n- Added code.",
            model="fake-model",
            prompt_tokens=20,
            completion_tokens=10,
        )


@pytest.fixture
def fake_clients(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeGitHubClient, FakeLLMClient]:
    github = FakeGitHubClient()
    llm = FakeLLMClient()

    def github_factory(*args: object, **kwargs: object) -> FakeGitHubClient:
        github.token = kwargs.get("token")
        github.api_url = kwargs.get("api_url")
        return github

    def llm_factory(*args: object, **kwargs: object) -> FakeLLMClient:
        llm.api_key = args[0] if args else None
        llm.model = kwargs.get("model")
        llm.base_url = kwargs.get("base_url")
        llm.max_tokens = kwargs.get("max_tokens")
        return llm

    monkeypatch.setattr(cli_module, "GitHubClient", github_factory)
    monkeypatch.setattr(cli_module, "LLMClient", llm_factory)
    return github, llm


@pytest.fixture(autouse=True)
def _disable_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "load_dotenv", lambda *args, **kwargs: None)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PR_LENS_GITHUB_TOKEN",
        "GITHUB_TOKEN",
        "PR_LENS_LLM_API_KEY",
        "OPENAI_API_KEY",
        "PR_LENS_LLM_MODEL",
        "PR_LENS_LLM_BASE_URL",
        "PR_LENS_MAX_DIFF_CHARS",
        "PR_LENS_MAX_COMMENT_CHARS",
        "PR_LENS_MAX_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_cli_review_prints_markdown(
    fake_clients: tuple[FakeGitHubClient, FakeLLMClient],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    result = CliRunner().invoke(cli_command, ["review", "octo/demo", "7"])
    assert result.exit_code == 0, result.output
    assert "PR Lens review" in result.output
    assert "## Summary" in result.output
    assert "`fake-model`" in result.output
    assert "https://github.com/octo/demo/pull/7" not in result.output


def test_cli_accepts_pull_request_url(
    fake_clients: tuple[FakeGitHubClient, FakeLLMClient],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    result = CliRunner().invoke(cli_command, ["review", "https://github.com/octo/demo/pull/7"])
    assert result.exit_code == 0, result.output
    assert "octo/demo#7" in result.output


def test_cli_uses_model_and_base_url_env(
    fake_clients: tuple[FakeGitHubClient, FakeLLMClient],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, llm = fake_clients
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    monkeypatch.setenv("PR_LENS_LLM_MODEL", "env-model")
    monkeypatch.setenv("PR_LENS_LLM_BASE_URL", "https://env.example/v1")
    result = CliRunner().invoke(cli_command, ["review", "octo/demo", "7"])
    assert result.exit_code == 0, result.output
    assert llm.model == "env-model"
    assert llm.base_url == "https://env.example/v1"
    assert github.token is None


def test_cli_uses_max_tokens_env(
    fake_clients: tuple[FakeGitHubClient, FakeLLMClient],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, llm = fake_clients
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    monkeypatch.setenv("PR_LENS_MAX_TOKENS", "1234")
    result = CliRunner().invoke(cli_command, ["review", "octo/demo", "7"])
    assert result.exit_code == 0, result.output
    assert llm.max_tokens == 1234


def test_cli_max_tokens_option_overrides_env(
    fake_clients: tuple[FakeGitHubClient, FakeLLMClient],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, llm = fake_clients
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    monkeypatch.setenv("PR_LENS_MAX_TOKENS", "1234")
    result = CliRunner().invoke(
        cli_command, ["review", "octo/demo", "7", "--max-tokens", "500"]
    )
    assert result.exit_code == 0, result.output
    assert llm.max_tokens == 500


def test_cli_rejects_invalid_max_tokens_env(
    fake_clients: tuple[FakeGitHubClient, FakeLLMClient],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    monkeypatch.setenv("PR_LENS_MAX_TOKENS", "not-a-number")
    result = CliRunner().invoke(cli_command, ["review", "octo/demo", "7"])
    assert result.exit_code == 1
    assert "PR_LENS_MAX_TOKENS" in result.output


def test_cli_posts_comment(
    fake_clients: tuple[FakeGitHubClient, FakeLLMClient],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _ = fake_clients
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    monkeypatch.setenv("PR_LENS_GITHUB_TOKEN", "github-key")
    result = CliRunner().invoke(cli_command, ["review", "octo/demo", "7", "--post"])
    assert result.exit_code == 0, result.output
    assert len(github.posted) == 1
    repository, number, body = github.posted[0]
    assert repository == "octo/demo"
    assert number == 7
    assert "PR Lens review" in body


def test_cli_writes_output_file(
    fake_clients: tuple[FakeGitHubClient, FakeLLMClient],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    output = tmp_path / "review.md"
    result = CliRunner().invoke(cli_command, ["review", "octo/demo", "7", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert output.exists()
    assert "PR Lens review" in output.read_text(encoding="utf-8")


def test_cli_requires_llm_api_key(
    clean_env: None,
) -> None:
    result = CliRunner().invoke(cli_command, ["review", "octo/demo", "7"])
    assert result.exit_code == 1
    assert "LLM API key" in result.output


def test_cli_requires_token_for_posting(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    result = CliRunner().invoke(cli_command, ["review", "octo/demo", "7", "--post"])
    assert result.exit_code == 1
    assert "GitHub token" in result.output


def test_cli_rejects_invalid_reference(clean_env: None) -> None:
    result = CliRunner().invoke(cli_command, ["review", "bogus"])
    assert result.exit_code == 1
    assert "Invalid repository" in result.output


def test_cli_reports_github_errors(
    fake_clients: tuple[FakeGitHubClient, FakeLLMClient],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _ = fake_clients
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    github.next_error = GitHubError("boom", status_code=500)
    result = CliRunner().invoke(cli_command, ["review", "octo/demo", "7"])
    assert result.exit_code == 1
    assert "boom" in result.output


def test_cli_reports_empty_diff(
    fake_clients: tuple[FakeGitHubClient, FakeLLMClient],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, _ = fake_clients
    github.diff_text = ""
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    result = CliRunner().invoke(cli_command, ["review", "octo/demo", "7"])
    assert result.exit_code == 1
    assert "empty diff" in result.output


def test_cli_reports_llm_errors(
    fake_clients: tuple[FakeGitHubClient, FakeLLMClient],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, llm = fake_clients
    monkeypatch.setenv("PR_LENS_LLM_API_KEY", "llm-key")
    llm.next_error = LLMError("model unavailable", status_code=503)
    result = CliRunner().invoke(cli_command, ["review", "octo/demo", "7"])
    assert result.exit_code == 1
    assert "model unavailable" in result.output
