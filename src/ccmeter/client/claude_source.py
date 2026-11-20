"""Claude Code source: new complete lines of `~/.claude/projects/**/*.jsonl`, minimized."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ccmeter.client.state import OffsetState
from ccmeter.shared.claude_lines import minimize_line

logger = logging.getLogger(__name__)

# Server-side limit per line. Minimized lines are a few hundred bytes, so this only guards
# against a pathological line; the full original lines are never sent.
MAX_LINE_BYTES = 5_000_000


@dataclass(frozen=True)
class PendingLine:
    """One minimized line ready to be shipped, with the offsets that locate it."""

    file_path: Path
    relative_file: str
    line: str
    byte_offset: int
    next_offset: int
    skip: bool = False


def scan_pending_lines(root: Path, state: OffsetState) -> list[PendingLine]:
    """Every complete line written after the saved offset of each transcript under `root`.

    A file that shrank (rewritten) is read again from the start; a file deleted between the
    listing and the read is skipped, the others go on.
    """
    pending: list[PendingLine] = []
    if not root.exists():
        return pending

    for file_path in sorted(root.rglob("*.jsonl")):
        offset = state.offsets.get(str(file_path), 0)
        try:
            if file_path.stat().st_size < offset:
                offset = 0
            pending.extend(_read_new_complete_lines(root, file_path, offset))
        except FileNotFoundError:
            # Claude Code cleans up old sessions: nothing left to ship for this file
            logger.info("transcript vanished while scanning: %s", file_path)
    return pending


def _read_new_complete_lines(root: Path, file_path: Path, offset: int) -> list[PendingLine]:
    with file_path.open("rb") as handle:
        handle.seek(offset)
        new_data = handle.read()
    if not new_data:
        return []

    complete_length = len(new_data)
    if not new_data.endswith(b"\n"):
        last_newline = new_data.rfind(b"\n")
        if last_newline < 0:
            return []
        complete_length = last_newline + 1

    relative_file = file_path.relative_to(root).as_posix()
    lines: list[PendingLine] = []
    running_offset = offset
    for raw_line in new_data[:complete_length].splitlines(keepends=True):
        next_offset = running_offset + len(raw_line)
        line = minimize_line(raw_line.rstrip(b"\r\n").decode("utf-8", errors="replace"))
        skip = len(line.encode("utf-8")) > MAX_LINE_BYTES
        if skip:
            logger.warning(
                "skipping line larger than the server limit: %s at byte %s",
                file_path,
                running_offset,
            )
        lines.append(
            PendingLine(
                file_path=file_path,
                relative_file=relative_file,
                line="" if skip else line,
                byte_offset=running_offset,
                next_offset=next_offset,
                skip=skip,
            )
        )
        running_offset = next_offset
    return lines
