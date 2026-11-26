"""Render review results as GitHub-flavored Markdown comments."""

from __future__ import annotations

import re
from typing import Any

from markdown_it import MarkdownIt

from pr_lens.diff_parser import ParsedDiff
from pr_lens.github_client import PullRequest
from pr_lens.llm_engine import QualityEstimate, ReviewResult

DEFAULT_MAX_COMMENT_CHARS = 60_000
PR_LENS_COMMENT_MARKER = "<!-- pr-lens:review-comment -->"
_TRUNCATION_NOTE = "\n\n---\n\n_Review truncated because it exceeded the configured comment limit._"
_MIN_COMMENT_CHARS = 120
_ZERO_WIDTH_SPACE = "\u200b"
_RATING_LABEL_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:ai(?:\s+quality)?\s+estimate|estimate|verdict|rating|quality|score)\s*:",
    re.IGNORECASE,
)
_RATING_VALUE = (
    r"(?:looks good|needs attention|high risk|[1-5]\s*/\s*5"
    r"|score\s*:?\s*[1-5](?:\s*/\s*5)?)"
)
_RATING_ONLY_RE = re.compile(
    rf"^{_RATING_VALUE}"
    rf"(?:\s*(?:[|,;:·\\-]+|\band\b)\s*{_RATING_VALUE})*$",
    re.IGNORECASE,
)
_RATING_PREFIX_RE = re.compile(r"^(?:>\s*|(?:[-+*]|\d+[.)])\s+|#{1,6}\s+)+")
_EMPHASIZED_LABEL_AFTER_COLON_RE = re.compile(r"^([*_]{1,3})(.+?):\1\s*")
_EMPHASIZED_LABEL_BEFORE_COLON_RE = re.compile(r"^([*_]{1,3})(.+?)\1\s*:")

# GitHub tables split cells at unescaped pipes even inside backticks, so code
# detection must use table-aware parsing to avoid hiding untrusted cell text.
_MARKDOWN = MarkdownIt("default").disable("linkify")
_ORIGINAL_BACKTICK_RULE = next(
    rule.fn for rule in _MARKDOWN.inline.ruler.__rules__ if rule.name == "backticks"
)


def _record_backtick_positions(state: Any, silent: bool) -> bool:
    start = state.pos
    matched = _ORIGINAL_BACKTICK_RULE(state, silent)
    if (
        matched
        and not silent
        and state.tokens
        and state.tokens[-1].type == "code_inline"
    ):
        state.tokens[-1].meta["start"] = start
        state.tokens[-1].meta["end"] = state.pos
    return matched


_MARKDOWN.inline.ruler.at("backticks", _record_backtick_positions)


def _line_start_offsets(text: str) -> list[int]:
    starts = [0]
    for match in re.finditer(r"\n", text):
        starts.append(match.end())
    return starts


def _fence_ranges(text: str) -> list[tuple[int, int, str, bool]]:
    lines = text.splitlines(keepends=True)
    starts = _line_start_offsets(text)
    ranges: list[tuple[int, int, str, bool]] = []
    for token in _MARKDOWN.parse(text):
        if token.type != "fence" or not token.map:
            continue
        start_line, end_line = token.map
        if start_line >= len(lines):
            continue
        opening = re.match(r"([`~]{3,})", lines[start_line].lstrip())
        if opening is None:
            continue
        marker = opening.group(1)
        char = marker[0]
        closed = False
        close_marker = marker
        if end_line > start_line + 1 and end_line - 1 < len(lines):
            closing = lines[end_line - 1].strip()
            if re.fullmatch(rf"{re.escape(char)}{{{len(marker)},}}", closing):
                closed = True
                close_marker = closing
        start_offset = starts[start_line]
        end_offset = starts[end_line] if end_line < len(starts) else len(text)
        ranges.append((start_offset, end_offset, close_marker, closed))
    return ranges


def _absolute_inline_position(
    marked: int,
    content_lines: list[str],
    content_starts: list[int],
    bases: list[int],
) -> int:
    index = 0
    for number, content_line in enumerate(content_lines):
        index = number
        if marked <= content_starts[number] + len(content_line):
            break
    return bases[index] + marked - content_starts[index]


def _inline_code_ranges(text: str) -> list[tuple[int, int]]:
    lines = text.splitlines(keepends=True)
    starts = _line_start_offsets(text)
    ranges: list[tuple[int, int]] = []
    for token in _MARKDOWN.parse(text):
        if token.type != "inline" or not token.map or not token.children:
            continue
        start_line, end_line = token.map
        source_lines = lines[start_line:end_line]
        content_lines = token.content.split("\n")
        if len(source_lines) != len(content_lines):
            continue
        bases: list[int] = []
        for index, (source_line, content_line) in enumerate(
            zip(source_lines, content_lines, strict=False)
        ):
            relative = source_line.find(content_line)
            if relative < 0:
                bases = []
                break
            bases.append(starts[start_line + index] + relative)
        if len(bases) != len(content_lines):
            continue
        content_starts: list[int] = []
        position = 0
        for content_line in content_lines:
            content_starts.append(position)
            position += len(content_line) + 1

        for child in token.children:
            if child.type != "code_inline":
                continue
            marked_start = child.meta.get("start")
            marked_end = child.meta.get("end")
            if isinstance(marked_start, int) and isinstance(marked_end, int):
                ranges.append(
                    (
                        _absolute_inline_position(
                            marked_start, content_lines, content_starts, bases
                        ),
                        _absolute_inline_position(
                            marked_end, content_lines, content_starts, bases
                        ),
                    )
                )
    return ranges


def _code_ranges(
    text: str,
) -> tuple[list[tuple[int, int]], list[tuple[int, int, str, bool]]]:
    fences = _fence_ranges(text)
    inline = [
        item
        for item in _inline_code_ranges(text)
        if not any(start <= item[0] and item[1] <= end for start, end, _, _ in fences)
    ]
    return inline, fences


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _truncate_body(text: str, cutoff: int, *, start: int = 0) -> str:
    inline, fences = _code_ranges(text)
    for code_start, code_end in inline:
        if code_start < cutoff < code_end:
            cutoff = max(start, code_start)
            break
    closing = next(
        (
            marker
            for fence_start, fence_end, marker, _ in fences
            if fence_start < cutoff < fence_end
        ),
        None,
    )
    body = text[:cutoff].rstrip()
    return f"{body}\n{closing}" if closing else body


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
        return f"{_truncate_body(text, cutoff)}{_TRUNCATION_NOTE}"

    prefix = protected_prefix.rstrip("\n")
    budget = max_chars - len(_TRUNCATION_NOTE)
    if budget <= len(prefix):
        return _compact_comment("Not rated.", max_chars)
    cutoff = text.rfind("\n", len(prefix), budget)
    if cutoff <= len(prefix):
        cutoff = budget
    return f"{_truncate_body(text, cutoff, start=len(prefix))}{_TRUNCATION_NOTE}"


def _compact_comment(status: str, max_chars: int) -> str:
    """Return a complete, minimal comment when the size budget is very small."""
    return f"{PR_LENS_COMMENT_MARKER}\n\n> **Review truncated.** {status}"[:max_chars]


def _escape_untrusted(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("@", f"@{_ZERO_WIDTH_SPACE}")
    )


def _sanitize(value: str) -> str:
    """Escape HTML and mentions outside valid Markdown code; keep code intact."""
    inline, fences = _code_ranges(value)
    protected = _merge_ranges(
        inline + [(start, end) for start, end, _, closed in fences if closed]
    )
    chunks: list[str] = []
    position = 0
    for start, end in protected:
        if start > position:
            chunks.append(_escape_untrusted(value[position:start]))
        chunks.append(value[start:end])
        position = end
    chunks.append(_escape_untrusted(value[position:]))
    return "".join(chunks).replace(
        PR_LENS_COMMENT_MARKER, f"<!--{_ZERO_WIDTH_SPACE}pr-lens:review-comment -->"
    )


def _safe_inline(value: str) -> str:
    """Render a value safely inside inline code."""
    return f"`{_sanitize(value).replace('`', "'")}`"


def _normalize_rating_line(line: str) -> str:
    candidate = line.strip()
    while True:
        stripped = _RATING_PREFIX_RE.sub("", candidate, count=1)
        if stripped == candidate:
            break
        candidate = stripped
    for pattern in (
        _EMPHASIZED_LABEL_AFTER_COLON_RE,
        _EMPHASIZED_LABEL_BEFORE_COLON_RE,
    ):
        stripped = pattern.sub(r"\2: ", candidate)
        if stripped != candidate:
            return stripped
    return candidate


def _is_explicit_rating_line(line: str) -> bool:
    """Return true only for a declared or rating-only line, not a finding."""
    normalized = _normalize_rating_line(line)
    if _RATING_LABEL_RE.match(normalized):
        return True

    candidate = normalized.strip().strip("*_`").strip().rstrip(".!").strip()
    return bool(candidate) and bool(_RATING_ONLY_RE.fullmatch(candidate))


def _remove_rating_lines(content: str) -> str:
    """Drop explicit rating declarations from a partial review only."""
    kept = [line for line in content.splitlines() if not _is_explicit_rating_line(line)]
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
