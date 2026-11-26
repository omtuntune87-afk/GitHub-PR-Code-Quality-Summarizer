"""Parse unified diff text into structured, testable objects."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

HunkLineKind = Literal["context", "addition", "deletion"]
FileStatus = Literal["added", "deleted", "modified", "renamed"]

_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@ ?(?P<section>.*)$"
)

_HEADER_PREFIXES = (
    "index ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "Binary files ",
    "GIT binary patch",
)


@dataclass(frozen=True, slots=True)
class DiffLine:
    """A single line inside a diff hunk."""

    kind: HunkLineKind
    content: str
    old_lineno: int | None = None
    new_lineno: int | None = None


@dataclass(slots=True)
class DiffHunk:
    """One ``@@ ... @@`` hunk."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str = ""
    lines: list[DiffLine] = field(default_factory=list)


@dataclass(slots=True)
class FileDiff:
    """All changes for a single file in a pull request."""

    old_path: str | None = None
    new_path: str | None = None
    status: FileStatus = "modified"
    is_binary: bool = False
    additions: int = 0
    deletions: int = 0
    hunks: list[DiffHunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        """Return the most useful path for display."""
        return self.new_path or self.old_path or "(unknown)"

    @property
    def display_path(self) -> str:
        """Return ``old -> new`` for renames and the path otherwise."""
        if self.status == "renamed" and self.old_path and self.new_path:
            return f"{self.old_path} -> {self.new_path}"
        return self.path

    @property
    def changes(self) -> int:
        """Return the number of added plus deleted lines."""
        return self.additions + self.deletions


@dataclass(slots=True)
class ParsedDiff:
    """The parsed representation of a complete unified diff."""

    files: list[FileDiff] = field(default_factory=list)

    @property
    def changed_files(self) -> int:
        return len(self.files)

    @property
    def additions(self) -> int:
        return sum(file.additions for file in self.files)

    @property
    def deletions(self) -> int:
        return sum(file.deletions for file in self.files)

    @property
    def is_empty(self) -> bool:
        return not self.files

    def file_by_path(self, path: str) -> FileDiff | None:
        """Look up a file by either its old or new path."""
        for file in self.files:
            if path in (file.old_path, file.new_path):
                return file
        return None

    def summary_line(self) -> str:
        """Return a compact human-readable size summary."""
        files = "file" if self.changed_files == 1 else "files"
        return f"{self.changed_files} {files}, +{self.additions} / -{self.deletions} lines"


def _clean_path(raw: str) -> str | None:
    """Normalize a path from a ``---``/``+++`` header."""
    path = raw.split("\t", 1)[0].strip()
    if not path or path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _paths_from_git_header(line: str) -> tuple[str | None, str | None]:
    """Best-effort path extraction from ``diff --git a/x b/x``."""
    rest = line.removeprefix("diff --git ").strip()
    if rest.startswith("a/"):
        separator = rest.find(" b/")
        if separator > 0:
            old = _clean_path(rest[:separator])
            new = _clean_path(rest[separator + 1 :])
            return old, new
    parts = rest.split()
    if len(parts) == 2:
        return _clean_path(parts[0]), _clean_path(parts[1])
    return None, None


def _finalize_file(
    file: FileDiff,
    *,
    declared_new: bool,
    declared_deleted: bool,
    renamed: bool,
) -> None:
    if declared_new:
        file.old_path = None
        file.status = "added"
    elif declared_deleted:
        file.new_path = None
        file.status = "deleted"
    elif file.old_path is None and file.new_path is not None:
        file.status = "added"
    elif file.old_path is not None and file.new_path is None:
        file.status = "deleted"
    elif renamed:
        file.status = "renamed"
    else:
        file.status = "modified"


def _append_hunk_line(
    hunk: DiffHunk,
    file: FileDiff,
    raw_line: str,
    *,
    old_cursor: int,
    new_cursor: int,
) -> tuple[int, int]:
    """Append one hunk line and return the updated line cursors."""
    prefix = raw_line[0]
    content = raw_line[1:]

    if prefix == "\\":
        # "\ No newline at end of file" is metadata, not a diff line.
        return old_cursor, new_cursor
    if prefix == " ":
        hunk.lines.append(DiffLine("context", content, old_cursor, new_cursor))
        return old_cursor + 1, new_cursor + 1
    if prefix == "+":
        hunk.lines.append(DiffLine("addition", content, None, new_cursor))
        file.additions += 1
        return old_cursor, new_cursor + 1
    if prefix == "-":
        hunk.lines.append(DiffLine("deletion", content, old_cursor, None))
        file.deletions += 1
        return old_cursor + 1, new_cursor

    return old_cursor, new_cursor


def parse_diff(text: str) -> ParsedDiff:
    """Parse unified diff text.

    The parser intentionally focuses on the information pr-lens needs for a
    review: file paths, change status, line counts, and hunk contents. Unknown
    metadata lines are ignored rather than treated as errors.
    """
    parsed = ParsedDiff()
    lines = text.splitlines()

    current: FileDiff | None = None
    hunk: DiffHunk | None = None
    old_cursor = 0
    new_cursor = 0
    in_hunk = False
    declared_new = False
    declared_deleted = False
    renamed = False

    def finish_file() -> None:
        nonlocal current, hunk, in_hunk
        if current is not None:
            _finalize_file(
                current,
                declared_new=declared_new,
                declared_deleted=declared_deleted,
                renamed=renamed,
            )
        current = None
        hunk = None
        in_hunk = False

    def start_file() -> FileDiff:
        nonlocal declared_new, declared_deleted, renamed, hunk, in_hunk
        declared_new = False
        declared_deleted = False
        renamed = False
        hunk = None
        in_hunk = False
        file = FileDiff()
        parsed.files.append(file)
        return file

    for line in lines:
        if line.startswith("diff --git "):
            finish_file()
            current = start_file()
            current.old_path, current.new_path = _paths_from_git_header(line)
            continue

        if current is None:
            if line.startswith("--- "):
                current = start_file()
                current.old_path = _clean_path(line[4:])
                continue
            continue

        if line.startswith("@@ "):
            match = _HUNK_RE.match(line)
            if match is None:
                continue
            old_start = int(match.group("old_start"))
            new_start = int(match.group("new_start"))
            old_count = int(match.group("old_count") or 1)
            new_count = int(match.group("new_count") or 1)
            hunk = DiffHunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                section=match.group("section") or "",
            )
            current.hunks.append(hunk)
            old_cursor = old_start
            new_cursor = new_start
            in_hunk = True
            continue

        if in_hunk and hunk is not None and line:
            if line[0] in " +-\\":
                old_cursor, new_cursor = _append_hunk_line(
                    hunk,
                    current,
                    line,
                    old_cursor=old_cursor,
                    new_cursor=new_cursor,
                )
                continue
            in_hunk = False

        if line.startswith("--- "):
            current.old_path = _clean_path(line[4:])
            in_hunk = False
        elif line.startswith("+++ "):
            current.new_path = _clean_path(line[4:])
            in_hunk = False
        elif line.startswith("new file mode "):
            declared_new = True
        elif line.startswith("deleted file mode "):
            declared_deleted = True
        elif line.startswith("rename from "):
            current.old_path = _clean_path(line.removeprefix("rename from "))
            renamed = True
        elif line.startswith("rename to "):
            current.new_path = _clean_path(line.removeprefix("rename to "))
            renamed = True
        elif line.startswith(("copy from ", "copy to ")):
            renamed = True
        elif line.startswith(("Binary files ", "GIT binary patch")):
            current.is_binary = True
        elif line.startswith(_HEADER_PREFIXES):
            continue

    finish_file()

    return parsed
