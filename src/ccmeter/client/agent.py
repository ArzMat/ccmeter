"""The ccmeter agent: ships local Claude Code and Codex usage to a ccmeter server.

Each pass reads what was appended since the saved offsets, sends it to `POST /ingest` in
batches and advances the offsets only after the server accepted a batch (at-least-once
delivery; the server deduplicates). Claude lines are minimized first and Codex rollouts are
reduced to numeric records, so conversation text never leaves the machine.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

import httpx

from ccmeter.client.claude_source import MAX_LINE_BYTES, PendingLine, scan_pending_lines
from ccmeter.client.codex_source import (
    CodexFileState,
    CodexRecord,
    find_rollout_files,
    read_codex_file,
)
from ccmeter.client.config import AgentConfig, load_agent_config
from ccmeter.client.state import (
    OffsetState,
    load_offset_state,
    save_offset_state,
    state_text,
    write_state_text,
)

__all__ = [
    "MAX_LINE_BYTES",
    "AgentConfig",
    "OffsetState",
    "ShipResult",
    "load_agent_config",
    "load_offset_state",
    "run_agent_loop",
    "save_offset_state",
    "scan_and_ship",
    "send_heartbeat",
    "ship_codex",
]

logger = logging.getLogger(__name__)

MAX_BATCH_LINES = 500
MAX_BATCH_RECORDS = 1000
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_BACKOFF_SECONDS = 60
# An idle agent sends nothing; past this long without a successful request it sends an empty
# ingest so the server can tell "agent alive, no new usage" from "agent stopped".
HEARTBEAT_SECONDS = 300.0


class HttpClient(Protocol):
    """Subset of the httpx sync client used by the agent."""

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> object: ...


@dataclass(frozen=True)
class ShipResult:
    """Result counters for one scan and ship pass."""

    sent: int
    failed: int
    records_sent: int = 0
    records_failed: int = 0

    @property
    def ok(self) -> bool:
        """True when nothing failed in this pass."""
        return self.failed == 0 and self.records_failed == 0

    @property
    def shipped_anything(self) -> bool:
        """True when the server accepted at least one line or record."""
        return self.sent > 0 or self.records_sent > 0


def scan_and_ship(
    cfg: AgentConfig,
    state: OffsetState,
    *,
    http_client: HttpClient,
) -> ShipResult:
    """Scan transcript files and ship new complete lines (minimized) to the server.

    Offsets advance only after the server accepted the batch that contains the line. Codex
    is read only when every Claude batch went through, so a down server stops the pass early.
    """
    sent = 0
    failed = 0
    for batch in _batches(scan_pending_lines(cfg.claude_projects_dir, state), MAX_BATCH_LINES):
        shippable = [pending for pending in batch if not pending.skip]
        try:
            if shippable:
                _post_lines(cfg, http_client, shippable)
        except Exception as exc:
            failed += len(shippable)
            logger.warning("failed to ship batch: %s", exc)
            break
        for pending in batch:
            state.offsets[str(pending.file_path)] = pending.next_offset
        sent += len(shippable)

    records_sent = 0
    records_failed = 0
    if failed == 0 and cfg.codex_dirs:
        records_sent, records_failed = ship_codex(cfg, state, http_client)
    return ShipResult(
        sent=sent,
        failed=failed,
        records_sent=records_sent,
        records_failed=records_failed,
    )


def ship_codex(cfg: AgentConfig, state: OffsetState, client: HttpClient) -> tuple[int, int]:
    """Read Codex rollouts and ship normalized usage records.

    Each file is streamed from its offset; records go out in batches of at most
    MAX_BATCH_RECORDS and a file's state advances only after the server accepted them.

    Returns:
        (records sent, records failed).
    """
    files = find_rollout_files(cfg.codex_dirs, _WARNED_ZST)
    present = {str(path) for path in files}
    for key in [key for key in state.codex if key not in present]:
        # archived (moved) or deleted: the new path starts from zero and the server drops
        # the duplicates by message_id
        del state.codex[key]
    batch = _CodexBatch()
    sent = 0
    for path in files:
        key = str(path)
        file_state = CodexFileState.from_dict(state.codex.get(key))
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < file_state.offset:
            file_state = CodexFileState()
        if size == file_state.offset:
            continue
        while True:
            try:
                result = read_codex_file(
                    path,
                    file_state,
                    max_records=MAX_BATCH_RECORDS - len(batch.records),
                )
            except OSError as exc:
                logger.warning("cannot read codex rollout %s: %s", path, exc)
                break
            batch.records.extend(result.records)
            batch.states[key] = result.state
            file_state = result.state
            if len(batch.records) >= MAX_BATCH_RECORDS:
                shipped = _flush_codex(cfg, client, state, batch)
                if shipped is None:
                    return sent, MAX_BATCH_RECORDS
                sent += shipped
            if result.exhausted:
                break
    pending = len(batch.records)
    shipped = _flush_codex(cfg, client, state, batch)
    if shipped is None:
        return sent, pending
    return sent + shipped, 0


def send_heartbeat(cfg: AgentConfig, client: HttpClient) -> None:
    """Empty ingest: tells the server this agent is alive even without new usage."""
    _post(cfg, client, {"host": cfg.host_label, "batch": []})


def run_agent_loop(
    cfg: AgentConfig,
    *,
    state_path: Path,
    http_client: HttpClient | None = None,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_iterations: int | None = None,
) -> None:
    """Run the polling loop with persisted offsets, exponential backoff and a heartbeat.

    Args:
        cfg: Agent configuration.
        state_path: Where offsets are persisted.
        http_client: HTTP client to use; by default one httpx client for the whole run.
        sleep: Sleep function (tests pass a recorder).
        clock: Monotonic clock (tests pass a fake one).
        max_iterations: Stop after this many passes (None = run forever).
    """
    if http_client is None:
        with httpx.Client() as client:
            _loop(cfg, state_path, client, sleep, clock, max_iterations)
    else:
        _loop(cfg, state_path, http_client, sleep, clock, max_iterations)


def _loop(
    cfg: AgentConfig,
    state_path: Path,
    client: HttpClient,
    sleep: Callable[[float], object],
    clock: Callable[[], float],
    max_iterations: int | None,
) -> None:
    state = load_offset_state(state_path)
    backoff_seconds = cfg.poll_seconds
    iterations = 0
    last_saved: str | None = None
    last_contact = clock()

    while max_iterations is None or iterations < max_iterations:
        result = scan_and_ship(cfg, state, http_client=client)
        if result.shipped_anything:
            last_contact = clock()
        if result.ok:
            if clock() - last_contact >= HEARTBEAT_SECONDS:
                try:
                    send_heartbeat(cfg, client)
                    last_contact = clock()
                except Exception as exc:
                    logger.warning("heartbeat failed: %s", exc)
            # with Codex the state weighs hundreds of KB: rewrite it only when it changed
            text = state_text(state)
            if text != last_saved:
                write_state_text(state_path, text)
                last_saved = text
            sleep(cfg.poll_seconds)
            backoff_seconds = cfg.poll_seconds
        else:
            sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, MAX_BACKOFF_SECONDS)
        iterations += 1


def _batches(lines: list[PendingLine], batch_size: int) -> list[list[PendingLine]]:
    return [lines[index : index + batch_size] for index in range(0, len(lines), batch_size)]


_WARNED_ZST: set[str] = set()


@dataclass
class _CodexBatch:
    """Codex records waiting to be sent and the file states to commit once accepted."""

    records: list[CodexRecord] = field(default_factory=list[CodexRecord])
    states: dict[str, CodexFileState] = field(default_factory=dict[str, CodexFileState])


def _flush_codex(
    cfg: AgentConfig,
    client: HttpClient,
    state: OffsetState,
    batch: _CodexBatch,
) -> int | None:
    """Send pending records; commit file states only on success. None = failure."""
    count = len(batch.records)
    try:
        if count:
            _post_records(cfg, client, batch.records)
    except Exception as exc:
        logger.warning("failed to ship codex records: %s", exc)
        batch.records.clear()
        batch.states.clear()
        return None
    for key, file_state in batch.states.items():
        state.codex[key] = file_state.to_dict()
    batch.records.clear()
    batch.states.clear()
    return count


def _post_records(cfg: AgentConfig, client: HttpClient, records: list[CodexRecord]) -> None:
    response = _post(
        cfg,
        client,
        {
            "host": cfg.host_label,
            "batch": [],
            "records": [record.to_payload() for record in records],
        },
    )
    # a server without Codex support answers 200 and ignores "records": do not advance
    body_reader = getattr(response, "json", None)
    body: object = body_reader() if callable(body_reader) else None
    received = (
        cast(dict[str, object], body).get("records_received") if isinstance(body, dict) else None
    )
    if received != len(records):
        raise RuntimeError("server without codex record support (records_received missing)")


def _post_lines(cfg: AgentConfig, client: HttpClient, batch: list[PendingLine]) -> None:
    _post(
        cfg,
        client,
        {
            "host": cfg.host_label,
            "batch": [
                {
                    "file": pending.relative_file,
                    "line": pending.line,
                    "offset": pending.byte_offset,
                }
                for pending in batch
            ],
        },
    )


def _post(cfg: AgentConfig, client: HttpClient, payload: dict[str, object]) -> object:
    response = client.post(
        f"{cfg.server_url.rstrip('/')}/ingest",
        headers={"Authorization": f"Bearer {cfg.api_token}"},
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()
    return response
