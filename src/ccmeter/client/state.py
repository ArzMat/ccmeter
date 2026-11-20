"""Agent state: how far each local file has been shipped.

The JSON format is stable across versions, so an upgraded agent resumes from the same byte
offsets and never re-sends what the server already has.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast


@dataclass
class OffsetState:
    """Mutable byte offsets for watched transcript files."""

    offsets: dict[str, int]
    # per Codex rollout: offset, current model, format, last cumulative usage, metadata
    codex: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])


def load_offset_state(path: Path) -> OffsetState:
    """Load persisted offset state from JSON, returning empty state if absent."""
    if not path.exists():
        return OffsetState(offsets={})
    data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8-sig")))
    offsets = {str(key): int(value) for key, value in cast(dict[str, Any], data["offsets"]).items()}
    raw_codex = data.get("codex")
    codex: dict[str, dict[str, object]] = {}
    if isinstance(raw_codex, dict):
        for key, value in cast(dict[str, object], raw_codex).items():
            if isinstance(value, dict):
                codex[str(key)] = cast(dict[str, object], value)
    return OffsetState(offsets=offsets, codex=codex)


def save_offset_state(path: Path, state: OffsetState) -> None:
    """Persist offset state as JSON (atomic replace)."""
    write_state_text(path, state_text(state))


def state_text(state: OffsetState) -> str:
    """Serialized state; the loop compares it to skip rewriting an unchanged file."""
    data: dict[str, object] = {"offsets": state.offsets}
    if state.codex:
        data["codex"] = state.codex
    return json.dumps(data, indent=2, sort_keys=True)


def write_state_text(path: Path, text: str) -> None:
    """Write `text` to `path` through a temporary file, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
