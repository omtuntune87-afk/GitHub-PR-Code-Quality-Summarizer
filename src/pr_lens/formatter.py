"""Render review results as GitHub-flavored Markdown comments."""

from __future__ import annotations

import re

from pr_lens.diff_parser import ParsedDiff
from pr_lens.github_client import PullRequest
from pr_lens.llm_engine import QualityEstimate, ReviewResult

DEFAULT_MAX_COMMENT_CHARS = 60_000
PR_LENS_COMMENT_MARKER = "<!-- pr-lens:review-comment -->"
_TRUNCATION_NOTE = "\n\n---\n\n_Review truncated because it exceeded the configured comment limit._"
_MIN_COMMENT_CHARS = 120
_ZERO_WIDTH_SPACE = "\u200b"
_ESTIMATE_PREFIX_RE = re.compile(r"^\s*(?:[-*]\s*)?ai\s*estimate\s*:", re.IGNORECASE)
_VERDICT_RE = re.compile(
    r"\b(?:looks good|needs attention|high risk)\b", re.IGNORECASE
)
_SCORE_RE = re.compile(
    r"\b(?:[1-5]\s*/\s*5|score\s*:?\s*[1-5])\b", re.IGNORECASE
)


def truncate_comment(
    text: str, max_chars: int, *, protected_prefix: str | None = None
) -> str:
    """Truncate a comment while preserving its safety prefix and a notice."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if protected_prefix is None:
        if max_chars <= len(_TRUNCATION_NOTE):
            return text[:max_chars]
        budget = max_chars - len(_TRUNCATION_NOTE)
        cutoff = text.rfind("\n", 0, budget)
        if cutoff < budget // 2:
            cutoff = budget
        return f"{text[:cutoff].rstrip()}{_TRUNCATION_NOTE}"

    prefix = protected_prefix.rstrip("\n")
    budget = max_chars - len(_TRUNCATION_NOTE)
    if budget <= len(prefix):
        return _compact_comment("Not rated.", max_chars)
    cutoff = text.rfind("\n", len(prefix), budget)
    if cutoff <= len(prefix):
        cutoff = budget
    return f"{text[:cutoff].rstrip()}{_TRUNCATION_NOTE}"


def _compact_comment(status: str, max_chars: int) -> str:
    """Return a complete, minimal comment when the size budget is very small."""
    return f"{PR_LENS_COMMENT_MARKER}\n\n> **Review truncated.** {status}"[:max_chars]


def _sanitize(value: str) -> str:
    """Neutralize HTML and mentions in untrusted PR or model text."""
    escaped = (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("@", f"@{_ZERO_WIDTH_SPACE}")
    )
    return escaped


def _safe_inline(value: str) -> str:
    """Render a value safely inside inline code."""
    return f"`{_sanitize(value).replace('`', "'")}`"


def _remove_rating_lines(content: str) -> str:
    """Drop model lines that repeat a rating in a partial review."""
    kept = [
        line
        for line in content.splitlines()
        if not (
            _ESTIMATE_PREFIX_RE.match(line)
            or _VERDICT_RE.search(line)
            or _SCORE_RE.search(line)
        )
    ]
    return "\n".join(kept).strip()


def _one_line(value: str, limit: int = 200) -> str:
    collapsed = " ".join(value.split())
    if not collapsed:
        return "(untitled)"
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit].rstrip()}..."


def _stats(pull_request: PullRequest, parsed: ParsedDiff | None) -> str:
    if parsed is not None and not parsed.is_empty:
        return parsed.summary_line()
    if pull_request.changed_files or pull_request.additions or pull_request.deletions:
        files = "file" if pull_request.changed_files == 1 else "files"
        return (
            f"{pull_request.changed_files} {files}, "
            f"+{pull_request.additions} / -{pull_request.deletions} lines"
        )
    return "diff stats unavailable"


def format_review(
    result: ReviewResult,
    pull_request: PullRequest,
    parsed: ParsedDiff | None = None,
    *,
    max_chars: int = DEFAULT_MAX_COMMENT_CHARS,
    include_footer: bool = True,
) -> str:
    """Format a model review as a Markdown comment suitable for GitHub."""
    if max_chars < _MIN_COMMENT_CHARS:
        status = (
            "Partial review; Not rated." if result.is_partial else "Not rated."
        )
        return _compact_comment(status, max_chars)

    stats = _stats(pull_request, parsed)
    content = result.content.strip() or "The model returned an empty review."
    if result.is_partial:
        content = _remove_rating_lines(content) or (
            "The model returned no usable review text after rating removal."
        )
    content = _sanitize(content)

    lines = [
        PR_LENS_COMMENT_MARKER,
        "## \U0001f50d PR Lens review",
        "",
        f"> **{_one_line(_sanitize(pull_request.title))}**",
        "",
        (
            f"{_sanitize(pull_request.repository)}#{pull_request.number} "
            f"\u00b7 {_sanitize(f'@{pull_request.author}')} "
            f"\u00b7 model {_safe_inline(result.model)} "
            f"\u00b7 {stats}"
        ),
    ]
    if result.is_partial:
        reasons: list[str] = []
        if result.diff_truncated:
            reasons.append("the diff was truncated")
        if result.skipped_files:
            reasons.append(f"{len(result.skipped_files)} file(s) were excluded")
        if result.unreviewable_files:
            reasons.append(
                f"{len(result.unreviewable_files)} file(s) had no reviewable text"
            )
        lines.extend(
            [
                "",
                "> [!WARNING]",
                f"> **Partial review:** {', '.join(reasons)}.",
            ]
        )

    lines.extend(["", "> **AI estimate:**"])
    if result.is_partial:
        lines.append("> Not rated (partial review; ratings are disabled).")
    elif result.quality is not None:
        quality: QualityEstimate = result.quality
        lines.append(f"> {_sanitize(quality.verdict)} · score {quality.score}/5")
        lines.append(f"> Reasons: {_sanitize(quality.reasons).replace('`', "'")}")
    else:
        lines.append("> Not rated (the model response did not include a valid estimate).")
    lines.append("> Automated estimate; verify with human review.")

    if result.diff_truncated:
        lines.extend(
            [
                "",
                "> [!NOTE]",
                "> The diff was truncated before review; findings may be incomplete.",
            ]
        )
    if result.skipped_files:
        lines.extend(["", "> [!NOTE]", "> Skipped files (excluded from this review):"])
        for path in result.skipped_files[:20]:
            lines.append(f"> - {_safe_inline(path)}")
        remaining = len(result.skipped_files) - 20
        if remaining > 0:
            lines.append(f"> - ...and {remaining} more file(s)")
    if result.unreviewable_files:
        lines.extend(
            ["", "> [!NOTE]", "> Files without reviewable text (not reviewed):"]
        )
        for path in result.unreviewable_files[:20]:
            lines.append(f"> - {_safe_inline(path)}")
        remaining = len(result.unreviewable_files) - 20
        if remaining > 0:
            lines.append(f"> - ...and {remaining} more file(s)")
    safety_prefix = "\n".join(lines)
    lines.extend(["", "---", "", content])

    if include_footer:
        lines.extend(
            [
                "",
                "---",
                "",
                "<sub>Generated by pr-lens \u00b7 automated review; verify before merging.</sub>",
            ]
        )

    return truncate_comment(
        "\n".join(lines), max_chars, protected_prefix=safety_prefix
    )


def format_no_review(
    pull_request: PullRequest,
    skipped_files: tuple[str, ...] | list[str] = (),
    *,
    max_chars: int = DEFAULT_MAX_COMMENT_CHARS,
    include_footer: bool = True,
) -> str:
    """Render an explicit no-review comment that cannot retain an old rating."""
    if max_chars < _MIN_COMMENT_CHARS:
        return _compact_comment("Nothing was reviewed; Not rated.", max_chars)

    lines = [
        PR_LENS_COMMENT_MARKER,
        "## 🔍 PR Lens review",
        "",
        f"> **{_one_line(_sanitize(pull_request.title))}**",
        "",
        (
            f"{_sanitize(pull_request.repository)}#{pull_request.number} "
            f"· {_sanitize(f'@{pull_request.author}')} "
            f"· {_stats(pull_request, None)}"
        ),
        "",
        "> [!WARNING]",
        "> **Nothing was reviewed.** No reviewable files remained after exclusions.",
        "",
        "> **AI estimate:**",
        "> Not rated (no files were reviewed).",
    ]
    if skipped_files:
        lines.extend(["", "> [!NOTE]", "> Excluded files:"])
        for path in list(skipped_files)[:20]:
            lines.append(f"> - {_safe_inline(path)}")
        remaining = len(skipped_files) - 20
        if remaining > 0:
            lines.append(f"> - ...and {remaining} more file(s)")
    safety_prefix = "\n".join(lines)
    lines.extend(
        [
            "",
            "---",
            "",
            "No LLM call was made. This comment replaces any previous pr-lens rating.",
        ]
    )
    if include_footer:
        lines.extend(
            [
                "",
                "---",
                "",
                "<sub>Generated by pr-lens · automated review; verify before merging.</sub>",
            ]
        )
    return truncate_comment(
        "\n".join(lines), max_chars, protected_prefix=safety_prefix
    )
