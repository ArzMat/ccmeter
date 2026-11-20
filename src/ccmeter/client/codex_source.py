"""Extraction of Codex (OpenAI) token usage from local rollouts.

The agent reads `~/.codex/sessions/**/rollout-*.jsonl` (and `~/.codex/archived_sessions`)
incrementally, by byte offset and in streaming, and sends the server ONLY normalized
numeric records: conversation text never leaves the PC.

Normalized record (an entry of the `records` list of `POST /ingest`, one per request to
the model):

- `provider`: always "codex".
- `message_id`: stable id, the dedup key on the server.
  Format B: `response_id` of the `token_usage_record` line.
  Format A: `codex-a:<session_id>:<hash>` where hash = sha256 of last_token_usage +
  total_token_usage (24 hex characters). The id depends on the content and not on the
  position because forked threads copy the parent's events at their start (verified on
  real data: 4734 copies in 53 files, 428M tokens): this way the copies collide with the
  original and the server discards them.
- `session_id`: root session (`session_meta.session_id`, shared by parent and sub-agents).
- `thread_id`: the file's thread (`session_meta.id`).
- `turn_id`: `root_turn_id` if present, otherwise `turn_id` (groups the prompts).
- `is_sidechain`: true if the thread has a `parent_thread_id` (sub-agent).
- `timestamp`: ISO timestamp of the line.
- `model`: current `turn_context.model`; events before the first turn_context take the
  first model of the file, "codex-unknown" if the file has none.
- `project`: name of the `cwd` folder.
- `input_tokens`: non-cached input = input_tokens - cached_input_tokens.
- `cache_read_tokens`: cached_input_tokens.
- `cache_write_tokens`: cache_write_input_tokens (currently always 0).
- `output_tokens`: output_tokens (reasoning included).
- `reasoning_tokens`: reasoning_output_tokens, informational only (already inside output).
- `service_tier`: null, current rollouts do not report it.

Usage formats:

- A (all versions): `event_msg` with `payload.type=token_count`. Uses
  `info.last_token_usage`; discards null `info`, events with only total_tokens and
  re-emissions (total_token_usage equal to the last accepted one).
- B (cli >= 0.153.1): `token_usage_record` line with per-request `usage` and `response_id`.

Per-file A/B rule (equivalent to "B if present, otherwise A, never both" even for a file
read in pieces): the file switches to mode B, persisted in the state, at the first B
line; from then on A events are ignored. If an A event arrives before any B line in a
file with `cli_version` >= 0.153.1, the reader looks ahead in the file: if there is a B
line the file is B and the event is ignored (parent copies at the start of a fork,
reversed order); if there is none, the decision is deferred until the file has been idle
for `STALE_SECONDS`, then it is counted as A. Checked on the 746 real rollouts: every
file with cli >= 0.153.1 and usage has B lines and no older file has any; in mixed files
every deduplicated A event is preceded by a B line with identical usage, and B only adds
the 11 compaction requests, which A does not record.

`codex-auto-review` (guardian sub-agents) is excluded: it is never sent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, BinaryIO, cast

logger = logging.getLogger(__name__)

PROVIDER = "codex"
UNKNOWN_MODEL = "codex-unknown"
EXCLUDED_MODELS = frozenset({"codex-auto-review"})
GUARDIAN_SOURCE = "guardian"
B_FORMAT_MIN_CLI = (0, 153, 1)
LINE_CHUNK_BYTES = 1 << 20
MAX_RELEVANT_LINE_BYTES = 32 << 20
STALE_SECONDS = 3600
MODE_A = "A"
MODE_B = "B"
MARKERS = (b'"session_meta"', b'"turn_context"', b'"token_usage_record"', b'"token_count"')
USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CLI_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(-.+)?$")
JsonObject = dict[str, Any]


@dataclass(frozen=True)
class CodexRecord:
    """Normalized usage of one Codex request (see the module docstring)."""

    message_id: str
    session_id: str
    thread_id: str | None
    turn_id: str | None
    is_sidechain: bool
    timestamp: str
    model: str
    project: str | None
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    service_tier: str | None = None
    provider: str = PROVIDER

    def to_payload(self) -> dict[str, object]:
        """Return the JSON payload sent to `POST /ingest`."""
        return asdict(self)


@dataclass
class CodexFileState:
    """Persistent read state of a rollout (stored in the agent's state.json)."""

    offset: int = 0
    thread_id: str | None = None
    session_id: str | None = None
    parent_thread_id: str | None = None
    project: str | None = None
    cli_version: str | None = None
    excluded: bool = False
    meta_seen: bool = False
    mode: str | None = None
    model: str | None = None
    turn_id: str | None = None
    last_total: list[int] | None = field(default=None)

    def to_dict(self) -> dict[str, object]:
        """Serialize for state.json."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: object) -> CodexFileState:
        """Rebuild from state.json, tolerating missing or unknown keys."""
        if not isinstance(raw, dict):
            return cls()
        data = cast(JsonObject, raw)
        last_total = data.get("last_total")
        return cls(
            offset=_int(data.get("offset")),
            thread_id=_str(data.get("thread_id")),
            session_id=_str(data.get("session_id")),
            parent_thread_id=_str(data.get("parent_thread_id")),
            project=_str(data.get("project")),
            cli_version=_str(data.get("cli_version")),
            excluded=bool(data.get("excluded", False)),
            meta_seen=bool(data.get("meta_seen", False)),
            mode=_str(data.get("mode")),
            model=_str(data.get("model")),
            turn_id=_str(data.get("turn_id")),
            last_total=(
                [_int(value) for value in cast(list[object], last_total)]
                if isinstance(last_total, list)
                else None
            ),
        )


@dataclass(frozen=True)
class CodexReadResult:
    """Result of a read: records produced and state after the last consumed line."""

    records: list[CodexRecord]
    state: CodexFileState
    exhausted: bool


class _Defer(Exception):
    """The current line cannot be decided yet: it must be read again on the next pass."""


def read_codex_file(
    path: Path,
    state: CodexFileState,
    *,
    max_records: int,
    now: float | None = None,
) -> CodexReadResult:
    """Read complete new lines of a rollout from `state.offset`, in streaming.

    Args:
        path: Rollout JSONL file.
        state: Current state of the file; it is not modified.
        max_records: Reading stops after this number of records.
        now: Current time (for tests); used to tell whether the file is idle.

    Returns:
        Normalized records, the new state (offset right after the last consumed line) and
        `exhausted` true if no complete lines are left to read. An incomplete trailing
        line is never consumed.
    """
    current = replace(state, last_total=list(state.last_total) if state.last_total else None)
    records: list[CodexRecord] = []
    reader = _FileReader(path, now=time.time() if now is None else now)
    with path.open("rb") as handle:
        handle.seek(current.offset)
        while len(records) < max_records:
            line = _read_line(handle, path)
            if line is None:
                return CodexReadResult(records=records, state=current, exhausted=True)
            content, next_offset = line
            if content is not None:
                try:
                    record = reader.process(content, current, next_offset)
                except _Defer:
                    return CodexReadResult(records=records, state=current, exhausted=True)
                if record is not None:
                    records.append(record)
            current.offset = next_offset
    return CodexReadResult(records=records, state=current, exhausted=False)


def find_rollout_files(roots: tuple[Path, ...], warned: set[str]) -> list[Path]:
    """Return the `.jsonl` rollouts under `roots`; `.zst` ones are skipped with a warning."""
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl*"):
            name = path.name
            if name.endswith(".jsonl"):
                files.append(path)
            elif name.endswith(".jsonl.zst"):
                key = str(path)
                if key not in warned:
                    warned.add(key)
                    logger.warning("compressed Codex rollout skipped (no zstd): %s", path)
    return sorted(files)


class _FileReader:
    """Interprets the relevant lines of a rollout, updating the file state."""

    def __init__(self, path: Path, *, now: float) -> None:
        self.path = path
        self.now = now
        self._first_model: str | None = None
        self._first_model_known = False
        self._b_ahead: bool | None = None

    def process(
        self, content: bytes, state: CodexFileState, next_offset: int
    ) -> CodexRecord | None:
        obj = _loads(content)
        if obj is None:
            return None
        line_type = obj.get("type")
        payload_object = obj.get("payload")
        payload = cast(JsonObject, payload_object) if isinstance(payload_object, dict) else {}
        timestamp = _str(obj.get("timestamp")) or ""
        if line_type == "session_meta":
            _apply_meta(state, payload, self.path)
            return None
        if line_type == "turn_context":
            model = _str(payload.get("model"))
            if model:
                state.model = model
            state.turn_id = _str(payload.get("root_turn_id")) or _str(payload.get("turn_id"))
            return None
        if line_type == "token_usage_record":
            return self._record_b(payload, timestamp, state, next_offset)
        if line_type == "event_msg" and payload.get("type") == "token_count":
            return self._record_a(payload, timestamp, state, next_offset)
        return None

    def _record_b(
        self,
        payload: JsonObject,
        timestamp: str,
        state: CodexFileState,
        next_offset: int,
    ) -> CodexRecord | None:
        usage = _usage(payload.get("usage"))
        if usage is None:
            return None
        model = self._model(state, next_offset)
        state.mode = MODE_B
        message_id = _str(payload.get("response_id"))
        if not message_id:
            message_id = _content_id(_session_id(state, self.path), usage, usage)
        turn_id = _str(payload.get("root_turn_id")) or _str(payload.get("turn_id"))
        return self._build(
            state,
            message_id=message_id,
            timestamp=timestamp,
            model=model,
            usage=usage,
            thread_id=_str(payload.get("thread_id")),
            session_id=_str(payload.get("session_id")),
            turn_id=turn_id or state.turn_id,
        )

    def _record_a(
        self,
        payload: JsonObject,
        timestamp: str,
        state: CodexFileState,
        next_offset: int,
    ) -> CodexRecord | None:
        if state.mode == MODE_B:
            return None
        info_object = payload.get("info")
        if not isinstance(info_object, dict):
            return None
        info = cast(JsonObject, info_object)
        last = _usage(info.get("last_token_usage"))
        total = _usage(info.get("total_token_usage"))
        if last is None or total is None:
            return None
        if last[0] == 0 and last[1] == 0 and last[3] == 0:
            # event with only total_tokens: no real usage
            return None
        if state.last_total is not None and list(total) == state.last_total:
            # identical re-emission of the last event
            return None
        if (
            state.mode is None
            and _cli_expects_b(state.cli_version)
            and self._has_b_ahead(next_offset)
        ):
            # file "expected to be B" with no B lines before this event but B lines after
            # (parent copies at the start of a fork, or reversed order): it counts as B
            state.mode = MODE_B
            return None
        model = self._model(state, next_offset)
        state.last_total = list(total)
        if state.mode is None:
            state.mode = MODE_A
        return self._build(
            state,
            message_id=_content_id(_session_id(state, self.path), last, total),
            timestamp=timestamp,
            model=model,
            usage=last,
            thread_id=None,
            session_id=None,
            turn_id=state.turn_id,
        )

    def _build(
        self,
        state: CodexFileState,
        *,
        message_id: str,
        timestamp: str,
        model: str,
        usage: tuple[int, ...],
        thread_id: str | None,
        session_id: str | None,
        turn_id: str | None,
    ) -> CodexRecord | None:
        if state.excluded or model.lower() in EXCLUDED_MODELS:
            return None
        input_tokens, cached, cache_write, output, reasoning, _total = usage
        return CodexRecord(
            message_id=message_id,
            session_id=session_id or _session_id(state, self.path),
            thread_id=thread_id or state.thread_id or _thread_from_name(self.path),
            turn_id=turn_id,
            is_sidechain=state.parent_thread_id is not None,
            timestamp=timestamp,
            model=model,
            project=state.project,
            input_tokens=max(input_tokens - cached, 0),
            cache_read_tokens=cached,
            cache_write_tokens=cache_write,
            output_tokens=output,
            reasoning_tokens=reasoning,
        )

    def _model(self, state: CodexFileState, next_offset: int) -> str:
        if state.model:
            return state.model
        if not self._first_model_known:
            self._first_model = _lookahead_model(self.path, next_offset)
            self._first_model_known = True
        if self._first_model:
            return self._first_model
        if self._is_stale():
            return UNKNOWN_MODEL
        raise _Defer

    def _has_b_ahead(self, next_offset: int) -> bool:
        """A event in a file that should be B, before any B line."""
        if self._b_ahead is None:
            self._b_ahead = _lookahead_has_b(self.path, next_offset)
        if self._b_ahead:
            return True
        if self._is_stale():
            logger.warning(
                "Codex rollout has no B lines despite its version, using A: %s", self.path
            )
            return False
        raise _Defer

    def _is_stale(self) -> bool:
        try:
            return self.now - self.path.stat().st_mtime >= STALE_SECONDS
        except OSError:
            return True


def _apply_meta(state: CodexFileState, payload: JsonObject, path: Path) -> None:
    if state.meta_seen:
        # forks also copy the parent's session_meta: only the first one counts
        return
    state.meta_seen = True
    state.thread_id = _str(payload.get("id")) or _thread_from_name(path)
    state.session_id = _str(payload.get("session_id")) or state.thread_id
    state.parent_thread_id = _str(payload.get("parent_thread_id"))
    state.project = _project_from_cwd(_str(payload.get("cwd")))
    state.cli_version = _str(payload.get("cli_version"))
    state.excluded = _is_guardian(payload.get("source"))


def _read_line(handle: BinaryIO, path: Path) -> tuple[bytes | None, int] | None:
    """Read the next complete line.

    Returns None when no complete line is left (EOF or partial line at the end).
    Otherwise returns (content, next offset); content is None for long irrelevant lines,
    skipped in chunks without keeping them in memory. The relevance of a line longer than
    LINE_CHUNK_BYTES is decided from the first chunk: the type is in the first ~100 bytes
    and relevant lines are small (long lines are text and tool output).
    """
    first = handle.readline(LINE_CHUNK_BYTES)
    if not first:
        return None
    if first.endswith(b"\n"):
        return first, handle.tell()
    if len(first) < LINE_CHUNK_BYTES:
        return None
    keep = _is_candidate(first)
    parts: list[bytes] = [first] if keep else []
    size = len(first)
    while True:
        chunk = handle.readline(LINE_CHUNK_BYTES)
        if not chunk:
            return None
        size += len(chunk)
        if keep and size > MAX_RELEVANT_LINE_BYTES:
            logger.warning("relevant Codex line too long, skipped: %s", path)
            keep = False
            parts = []
        elif keep:
            parts.append(chunk)
        if chunk.endswith(b"\n"):
            return (b"".join(parts) if keep else None), handle.tell()


def _is_candidate(content: bytes) -> bool:
    return any(marker in content for marker in MARKERS)


def _lookahead_model(path: Path, offset: int) -> str | None:
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            line = _read_line(handle, path)
            if line is None:
                return None
            content = line[0]
            if content is None or b'"turn_context"' not in content:
                continue
            obj = _loads(content)
            if obj is None or obj.get("type") != "turn_context":
                continue
            payload = obj.get("payload")
            if isinstance(payload, dict):
                model = _str(cast(JsonObject, payload).get("model"))
                if model:
                    return model


def _lookahead_has_b(path: Path, offset: int) -> bool:
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            line = _read_line(handle, path)
            if line is None:
                return False
            content = line[0]
            if content is None or b'"token_usage_record"' not in content:
                continue
            obj = _loads(content)
            if obj is not None and obj.get("type") == "token_usage_record":
                return True


def _loads(content: bytes) -> JsonObject | None:
    if not _is_candidate(content):
        return None
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError, UnicodeDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return cast(JsonObject, decoded)


def _usage(raw: object) -> tuple[int, ...] | None:
    if not isinstance(raw, dict):
        return None
    data = cast(JsonObject, raw)
    return tuple(_int(data.get(name)) for name in USAGE_FIELDS)


def _content_id(session_id: str, last: tuple[int, ...], total: tuple[int, ...]) -> str:
    digest = hashlib.sha256(",".join(str(value) for value in last + total).encode("ascii"))
    return f"codex-a:{session_id}:{digest.hexdigest()[:24]}"


def _session_id(state: CodexFileState, path: Path) -> str:
    return state.session_id or state.thread_id or _thread_from_name(path)


def _thread_from_name(path: Path) -> str:
    match = UUID_RE.search(path.name)
    return match.group(0) if match else path.stem


def _project_from_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    parts = [part for part in cwd.replace("\\", "/").split("/") if part]
    if not parts:
        return None
    return parts[-1]


def _is_guardian(source: object) -> bool:
    if not isinstance(source, dict):
        return False
    subagent = cast(JsonObject, source).get("subagent")
    if not isinstance(subagent, dict):
        return False
    return cast(JsonObject, subagent).get("other") == GUARDIAN_SOURCE


def _cli_expects_b(cli_version: str | None) -> bool:
    if not cli_version:
        return False
    match = CLI_RE.match(cli_version)
    if match is None:
        return False
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    prerelease = match.group(4) is not None
    return core > B_FORMAT_MIN_CLI or (core == B_FORMAT_MIN_CLI and not prerelease)


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
