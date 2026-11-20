"""Synthetic Codex rollout generators for tests (made-up values, no real text)."""

from __future__ import annotations

import json
from compression import zstd
from pathlib import Path
from typing import Any

SESSION = "01a00000-0000-7000-8000-000000000001"
CHILD = "01a00000-0000-7000-8000-000000000002"
OLD_CLI = "0.150.0"
NEW_CLI = "0.158.0"


class Rollout:
    """Builds the lines of a rollout with increasing ordinals and timestamps."""

    def __init__(self, thread_id: str = SESSION) -> None:
        self.thread_id = thread_id
        self.lines: list[str] = []
        self.ordinal = 0

    def _add(self, line_type: str, payload: dict[str, Any]) -> Rollout:
        second = self.ordinal
        record = {
            "timestamp": f"2026-07-20T10:{second // 60:02d}:{second % 60:02d}.000Z",
            "ordinal": self.ordinal,
            "type": line_type,
            "payload": payload,
        }
        self.lines.append(json.dumps(record))
        self.ordinal += 1
        return self

    def meta(
        self,
        *,
        cli: str = OLD_CLI,
        session_id: str = SESSION,
        parent: str | None = None,
        source: object = "cli",
        cwd: str = "C:\\work\\demo-project",
        forked_from: str | None = None,
    ) -> Rollout:
        payload: dict[str, Any] = {
            "id": self.thread_id,
            "session_id": session_id,
            "cwd": cwd,
            "cli_version": cli,
            "source": source,
            "base_instructions": {"text": "invented instructions"},
        }
        if parent is not None:
            payload["parent_thread_id"] = parent
        if forked_from is not None:
            payload["forked_from_id"] = forked_from
        return self._add("session_meta", payload)

    def turn(self, model: str, turn_id: str = "turn-1", root_turn_id: str | None = None) -> Rollout:
        payload: dict[str, Any] = {"turn_id": turn_id, "model": model, "cwd": "C:\\x"}
        if root_turn_id is not None:
            payload["root_turn_id"] = root_turn_id
        return self._add("turn_context", payload)

    def text(self, size: int = 40) -> Rollout:
        return self._add(
            "response_item",
            {"type": "message", "content": [{"type": "output_text", "text": "x" * size}]},
        )

    def token_count(self, last: dict[str, int], total: dict[str, int]) -> Rollout:
        info = {"last_token_usage": last, "total_token_usage": total, "model_context_window": 1}
        return self._add("event_msg", {"type": "token_count", "info": info, "rate_limits": None})

    def total_only(self, total_tokens: int) -> Rollout:
        zero = usage(0, 0, 0, 0, total=total_tokens)
        return self.token_count(zero, zero)

    def no_info(self) -> Rollout:
        return self._add("event_msg", {"type": "token_count", "info": None})

    def record(self, response_id: str, used: dict[str, int], turn_id: str = "turn-1") -> Rollout:
        return self._add(
            "token_usage_record",
            {
                "thread_id": self.thread_id,
                "session_id": SESSION,
                "turn_id": turn_id,
                "root_turn_id": "root-" + turn_id,
                "response_id": response_id,
                "usage": used,
                "turn_token_usage": used,
                "thread_token_usage": used,
            },
        )

    def text_payload(self) -> bytes:
        return ("\n".join(self.lines) + "\n").encode("utf-8")

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.text_payload())
        return path

    def write_zst(self, path: Path) -> Path:
        """Write the rollout zstd-compressed (`rollout-*.jsonl.zst`)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(zstd.compress(self.text_payload()))
        return path


def compress(path: Path) -> Path:
    """Replace a `.jsonl` rollout with its `.jsonl.zst`, the way Codex compresses idle rollouts."""
    target = path.with_name(path.name + ".zst")
    target.write_bytes(zstd.compress(path.read_bytes()))
    path.unlink()
    return target


def usage(
    input_tokens: int,
    cached: int,
    output: int,
    reasoning: int,
    *,
    total: int | None = None,
    cache_write: int = 0,
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_tokens + output if total is None else total,
    }


def add(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {key: left[key] + right[key] for key in left}


class Cumulative:
    """Keeps the thread cumulative total the way Codex does (total_token_usage)."""

    def __init__(self, start: dict[str, int] | None = None) -> None:
        self.total = start or usage(0, 0, 0, 0)

    def step(self, last: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
        self.total = add(self.total, last)
        return last, dict(self.total)
