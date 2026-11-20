"""Agent-side Codex extraction on synthetic rollouts (format A, B, mixed, forks, tails)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from compression import zstd
from pathlib import Path
from typing import cast

import pytest
from tests.codex_fixtures import (
    CHILD,
    NEW_CLI,
    SESSION,
    Cumulative,
    Rollout,
    usage,
)

from ccmeter.client import codex_source as codex_module
from ccmeter.client.agent import (
    AgentConfig,
    OffsetState,
    load_agent_config,
    load_offset_state,
    save_offset_state,
    ship_codex,
)
from ccmeter.client.codex_source import (
    STALE_SECONDS,
    UNKNOWN_MODEL,
    CodexFileState,
    CodexReadResult,
    CodexRecord,
    find_rollout_files,
    read_codex_file,
)

NOW = time.time()
LATER = NOW + STALE_SECONDS + 10


def _read_all(
    path: Path, state: CodexFileState | None = None, now: float = LATER
) -> CodexReadResult:
    current = state or CodexFileState()
    records: list[CodexRecord] = []
    while True:
        result = read_codex_file(path, current, max_records=1000, now=now)
        records.extend(result.records)
        current = result.state
        if result.exhausted:
            return CodexReadResult(records=records, state=current, exhausted=True)


def _format_a_rollout() -> Rollout:
    cumulative = Cumulative()
    rollout = Rollout().meta().turn("gpt-5.6-sol", "turn-1").text()
    rollout.total_only(61_849)
    rollout.no_info()
    rollout.token_count(*cumulative.step(usage(25_405, 11_008, 420, 264)))
    rollout.token_count(*cumulative.step(usage(26_033, 24_320, 115, 23)))
    # identical re-emission of the previous cumulative total
    rollout.token_count(usage(26_033, 24_320, 115, 23), dict(cumulative.total))
    rollout.turn("gpt-6-astra", "turn-2").text(500)
    rollout.token_count(*cumulative.step(usage(300_000, 250_000, 1_000, 10)))
    return rollout


def test_format_a_extracts_last_usage_and_skips_noise(tmp_path: Path) -> None:
    path = _format_a_rollout().write(tmp_path / "rollout-a.jsonl")

    result = _read_all(path)

    records = result.records
    assert [record.model for record in records] == ["gpt-5.6-sol", "gpt-5.6-sol", "gpt-6-astra"]
    first = records[0]
    assert first.provider == "codex"
    assert first.session_id == SESSION
    assert first.thread_id == SESSION
    assert first.project == "demo-project"
    assert first.is_sidechain is False
    assert first.turn_id == "turn-1"
    assert first.input_tokens == 25_405 - 11_008
    assert first.cache_read_tokens == 11_008
    assert first.output_tokens == 420
    assert first.reasoning_tokens == 264
    assert first.timestamp.startswith("2026-07-20T10:")
    assert first.message_id.startswith(f"codex-a:{SESSION}:")
    assert len({record.message_id for record in records}) == 3
    assert records[2].turn_id == "turn-2"
    assert result.state.mode == "A"
    assert result.state.offset == path.stat().st_size


def test_records_never_contain_conversation_text(tmp_path: Path) -> None:
    path = _format_a_rollout().write(tmp_path / "rollout-a.jsonl")

    payloads = [record.to_payload() for record in _read_all(path).records]

    dumped = json.dumps(payloads)
    assert "xxxx" not in dumped
    assert "istruzioni" not in dumped
    assert set(payloads[0]) == {
        "provider",
        "message_id",
        "session_id",
        "thread_id",
        "turn_id",
        "is_sidechain",
        "timestamp",
        "model",
        "project",
        "input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "output_tokens",
        "reasoning_tokens",
        "service_tier",
    }


def _format_b_rollout() -> Rollout:
    cumulative = Cumulative()
    rollout = Rollout().meta(cli=NEW_CLI).turn("gpt-6-astra", "turn-1", "root-turn-1")
    for index, used in enumerate((usage(20_174, 0, 74, 0), usage(20_437, 19_968, 70, 0))):
        rollout.record(f"resp_{index}", used)
        rollout.text()
        rollout.token_count(*cumulative.step(used))
    # compaction request: B line without an A event
    rollout.record("resp_compact", usage(150_000, 0, 6_000, 0))
    return rollout


def test_format_b_uses_response_id_and_ignores_format_a(tmp_path: Path) -> None:
    path = _format_b_rollout().write(tmp_path / "rollout-b.jsonl")

    result = _read_all(path)

    assert [record.message_id for record in result.records] == ["resp_0", "resp_1", "resp_compact"]
    assert result.records[1].input_tokens == 20_437 - 19_968
    assert result.records[1].cache_read_tokens == 19_968
    assert result.records[0].turn_id == "root-turn-1"
    assert result.state.mode == "B"


def test_old_cli_file_switches_to_b_when_first_record_appears(tmp_path: Path) -> None:
    cumulative = Cumulative()
    rollout = Rollout().meta().turn("gpt-5.6-sol")
    rollout.token_count(*cumulative.step(usage(1_000, 0, 10, 0)))
    used = usage(2_000, 1_000, 20, 0)
    rollout.record("resp_new", used)
    rollout.token_count(*cumulative.step(used))
    path = rollout.write(tmp_path / "rollout-mixed.jsonl")

    records = _read_all(path).records

    assert [record.output_tokens for record in records] == [10, 20]
    assert records[0].message_id.startswith("codex-a:")
    assert records[1].message_id == "resp_new"


def test_new_cli_fork_ignores_copied_format_a_events_before_first_record(
    tmp_path: Path,
) -> None:
    cumulative = Cumulative()
    rollout = Rollout(CHILD).meta(cli=NEW_CLI, parent=SESSION, forked_from=SESSION)
    rollout.turn("gpt-6-astra")
    # parent's history copied at the start: A events without B lines
    rollout.token_count(*cumulative.step(usage(5_000, 0, 50, 0)))
    rollout.token_count(*cumulative.step(usage(6_000, 5_000, 60, 0)))
    used = usage(7_000, 6_000, 70, 0)
    rollout.record("resp_child", used)
    rollout.token_count(*cumulative.step(used))
    path = rollout.write(tmp_path / "rollout-fork.jsonl")

    records = _read_all(path, now=NOW).records

    assert [record.message_id for record in records] == ["resp_child"]
    assert records[0].is_sidechain is True
    assert records[0].thread_id == CHILD


def test_new_cli_file_without_records_waits_then_counts_format_a(tmp_path: Path) -> None:
    cumulative = Cumulative()
    rollout = Rollout().meta(cli=NEW_CLI).turn("gpt-6-sol")
    rollout.token_count(*cumulative.step(usage(1_000, 0, 10, 0)))
    path = rollout.write(tmp_path / "rollout-new-a.jsonl")

    fresh = _read_all(path, now=NOW)
    stale = _read_all(path, fresh.state, now=LATER)

    assert fresh.records == []
    assert fresh.state.offset < path.stat().st_size
    assert [record.output_tokens for record in stale.records] == [10]
    assert stale.state.offset == path.stat().st_size


def test_reemission_after_resume_from_offset_is_skipped(tmp_path: Path) -> None:
    cumulative = Cumulative()
    rollout = Rollout().meta().turn("gpt-5.6-sol")
    last, total = cumulative.step(usage(1_000, 0, 10, 0))
    rollout.token_count(last, total)
    path = rollout.write(tmp_path / "rollout-resume.jsonl")
    first = _read_all(path)
    with path.open("ab") as handle:
        again = Rollout()
        again.ordinal = 99
        again.token_count(last, total)
        again.token_count(*cumulative.step(usage(2_000, 1_000, 20, 0)))
        handle.write(again.text_payload())

    second = _read_all(path, first.state)

    assert [record.output_tokens for record in first.records] == [10]
    assert [record.output_tokens for record in second.records] == [20]


def test_events_before_first_turn_context_take_first_model(tmp_path: Path) -> None:
    cumulative = Cumulative()
    rollout = Rollout().meta()
    rollout.token_count(*cumulative.step(usage(1_000, 0, 10, 0)))
    rollout.turn("gpt-5.6-terra").token_count(*cumulative.step(usage(2_000, 0, 20, 0)))
    rollout.turn("gpt-6-astra").token_count(*cumulative.step(usage(3_000, 0, 30, 0)))
    path = rollout.write(tmp_path / "rollout-pre.jsonl")

    records = _read_all(path, now=NOW).records

    assert [record.model for record in records] == ["gpt-5.6-terra", "gpt-5.6-terra", "gpt-6-astra"]


def test_file_without_turn_context_uses_unknown_model_once_stale(tmp_path: Path) -> None:
    cumulative = Cumulative()
    rollout = Rollout().meta()
    rollout.token_count(*cumulative.step(usage(1_000, 0, 10, 0)))
    path = rollout.write(tmp_path / "rollout-nomodel.jsonl")

    fresh = _read_all(path, now=NOW)
    stale = _read_all(path, fresh.state, now=LATER)

    assert fresh.records == []
    assert [record.model for record in stale.records] == [UNKNOWN_MODEL]


def test_guardian_threads_and_auto_review_model_are_excluded(tmp_path: Path) -> None:
    cumulative = Cumulative()
    guardian = Rollout(CHILD).meta(parent=SESSION, source={"subagent": {"other": "guardian"}})
    guardian.turn("gpt-5.6-sol").token_count(*cumulative.step(usage(1_000, 0, 10, 0)))
    other = Rollout().meta().turn("codex-auto-review")
    other.token_count(*Cumulative().step(usage(1_000, 0, 10, 0)))
    other.turn("gpt-5.6-sol").token_count(*Cumulative().step(usage(5, 0, 5, 0)))
    guardian_path = guardian.write(tmp_path / "rollout-guardian.jsonl")
    other_path = other.write(tmp_path / "rollout-review.jsonl")

    guardian_result = _read_all(guardian_path)
    other_result = _read_all(other_path)

    assert guardian_result.records == []
    assert guardian_result.state.offset == guardian_path.stat().st_size
    assert [record.model for record in other_result.records] == ["gpt-5.6-sol"]


def test_incomplete_trailing_line_is_not_consumed(tmp_path: Path) -> None:
    cumulative = Cumulative()
    rollout = Rollout().meta().turn("gpt-5.6-sol")
    rollout.token_count(*cumulative.step(usage(1_000, 0, 10, 0)))
    rollout.token_count(*cumulative.step(usage(2_000, 0, 20, 0)))
    payload = rollout.text_payload()
    cut = len(payload) - 30
    path = tmp_path / "rollout-partial.jsonl"
    path.write_bytes(payload[:cut])

    partial = _read_all(path)
    with path.open("ab") as handle:
        handle.write(payload[cut:])
    complete = _read_all(path, partial.state)

    assert [record.output_tokens for record in partial.records] == [10]
    assert partial.state.offset == payload.rindex(b"\n", 0, cut) + 1
    assert [record.output_tokens for record in complete.records] == [20]
    assert complete.state.offset == len(payload)


def test_resume_from_saved_state_matches_single_read(tmp_path: Path) -> None:
    path = _format_a_rollout().write(tmp_path / "rollout-a.jsonl")
    single = _read_all(path).records

    first = read_codex_file(path, CodexFileState(), max_records=2, now=LATER)
    restored = CodexFileState.from_dict(json.loads(json.dumps(first.state.to_dict())))
    rest = _read_all(path, restored)

    assert first.exhausted is False
    assert first.records + rest.records == single


def test_fork_copies_share_message_ids_with_parent(tmp_path: Path) -> None:
    parent_cumulative = Cumulative()
    parent = Rollout().meta().turn("gpt-5.6-sol")
    copied: list[tuple[dict[str, int], dict[str, int]]] = []
    for step in range(3):
        pair = parent_cumulative.step(usage(1_000 * (step + 1), 0, 10 + step, 0))
        copied.append(pair)
        parent.token_count(*pair)
    child = Rollout(CHILD).meta(parent=SESSION, forked_from=SESSION).turn("gpt-5.6-sol")
    for pair in copied:
        child.token_count(*pair)
    child_cumulative = Cumulative(dict(parent_cumulative.total))
    child.token_count(*child_cumulative.step(usage(9_000, 0, 99, 0)))
    parent_path = parent.write(tmp_path / "rollout-parent.jsonl")
    child_path = child.write(tmp_path / "rollout-child.jsonl")

    parent_ids = [record.message_id for record in _read_all(parent_path).records]
    child_records = _read_all(child_path).records

    assert [record.message_id for record in child_records[:3]] == parent_ids
    assert child_records[3].message_id not in parent_ids
    assert all(record.is_sidechain for record in child_records)
    assert {record.session_id for record in child_records} == {SESSION}


def test_long_lines_are_streamed_in_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_module, "LINE_CHUNK_BYTES", 128)
    path = _format_a_rollout().write(tmp_path / "rollout-a.jsonl")
    expected = [record.output_tokens for record in _read_all(path).records]

    assert expected == [420, 115, 1_000]
    monkeypatch.setattr(codex_module, "MAX_RELEVANT_LINE_BYTES", 300)
    # session_meta and the token_count events exceed the limit: they are skipped without errors
    assert _read_all(path).state.offset == path.stat().st_size


def test_find_rollout_files_lists_plain_and_compressed_rollouts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "sessions" / "2026" / "07" / "20"
    Rollout().meta().write(root / "rollout-1.jsonl")
    Rollout().meta().write_zst(root / "rollout-2.jsonl.zst")
    (root / "rollout-3.jsonl.tmp").write_bytes(b"")

    with caplog.at_level(logging.WARNING):
        files = find_rollout_files((tmp_path / "sessions", tmp_path / "missing"), set())

    assert [path.name for path in files] == ["rollout-1.jsonl", "rollout-2.jsonl.zst"]
    assert caplog.messages == []


def test_find_rollout_files_without_zstd_skips_compressed_with_one_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a Python built without compression.zstd keeps the 1.0.0 behaviour
    monkeypatch.setattr("ccmeter.client.codex_source._zstd", None)
    root = tmp_path / "sessions" / "2026" / "07" / "20"
    Rollout().meta().write(root / "rollout-1.jsonl")
    Rollout().meta().write_zst(root / "rollout-2.jsonl.zst")
    Rollout().meta().write_zst(root / "rollout-3.jsonl.zst")
    warned: set[str] = set()

    with caplog.at_level(logging.WARNING):
        first = find_rollout_files((tmp_path / "sessions",), warned)
        second = find_rollout_files((tmp_path / "sessions",), warned)

    assert [path.name for path in first] == ["rollout-1.jsonl"]
    assert second == first
    assert len(caplog.messages) == 1
    assert "2 compressed" in caplog.messages[0]


@pytest.mark.parametrize("build", [_format_a_rollout, _format_b_rollout])
def test_compressed_rollout_reads_like_plain_jsonl(
    tmp_path: Path,
    build: Callable[[], Rollout],
) -> None:
    rollout = build()
    plain = rollout.write(tmp_path / "plain" / "rollout-x.jsonl")
    packed = rollout.write_zst(tmp_path / "packed" / "rollout-x.jsonl.zst")

    expected = _read_all(plain)
    result = _read_all(packed)

    assert result.records == expected.records
    assert result.state.offset == len(rollout.text_payload())
    assert result.state.compressed_done is True
    assert expected.state.compressed_done is False


def test_compressed_rollout_resumes_from_decompressed_offset(tmp_path: Path) -> None:
    path = _format_a_rollout().write_zst(tmp_path / "rollout-a.jsonl.zst")
    single = _read_all(path).records

    first = read_codex_file(path, CodexFileState(), max_records=2, now=LATER)
    restored = CodexFileState.from_dict(json.loads(json.dumps(first.state.to_dict())))
    rest = _read_all(path, restored)

    assert first.exhausted is False
    assert first.state.compressed_done is False
    assert first.records + rest.records == single
    assert rest.state.compressed_done is True


def test_compressed_rollout_is_final_and_never_waits(tmp_path: Path) -> None:
    # a recent .jsonl would wait STALE_SECONDS here; a compressed rollout no longer grows
    new_a = Rollout().meta(cli=NEW_CLI).turn("gpt-6-sol")
    new_a.token_count(*Cumulative().step(usage(1_000, 0, 10, 0)))
    no_model = Rollout(CHILD).meta()
    no_model.token_count(*Cumulative().step(usage(2_000, 0, 20, 0)))
    new_a_path = new_a.write_zst(tmp_path / "rollout-new-a.jsonl.zst")
    no_model_path = no_model.write_zst(tmp_path / "rollout-nomodel.jsonl.zst")

    new_a_result = _read_all(new_a_path, now=NOW)
    no_model_result = _read_all(no_model_path, now=NOW)

    assert [record.output_tokens for record in new_a_result.records] == [10]
    assert [record.model for record in no_model_result.records] == [UNKNOWN_MODEL]
    assert new_a_result.state.compressed_done is True
    assert no_model_result.state.compressed_done is True


def test_compressed_copy_without_session_meta_keeps_the_same_ids(tmp_path: Path) -> None:
    rollout = Rollout().turn("gpt-5.6-sol")
    rollout.token_count(*Cumulative().step(usage(1_000, 0, 10, 0)))
    plain = rollout.write(tmp_path / "rollout-nometa.jsonl")
    packed = rollout.write_zst(tmp_path / "rollout-nometa.jsonl.zst")

    plain_records = _read_all(plain).records
    packed_records = _read_all(packed).records

    assert packed_records == plain_records
    assert plain_records[0].session_id == "rollout-nometa"
    assert plain_records[0].thread_id == "rollout-nometa"


def test_damaged_compressed_rollouts_keep_their_readable_part(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rollout = _format_a_rollout()
    payload = rollout.text_payload()
    split = payload.index(b"\n", len(payload) // 2) + 1
    full = _read_all(rollout.write(tmp_path / "plain" / "rollout-cut.jsonl")).records
    head_path = tmp_path / "head" / "rollout-cut.jsonl"
    head_path.parent.mkdir()
    head_path.write_bytes(payload[:split])
    head = _read_all(head_path).records
    truncated = tmp_path / "rollout-cut.jsonl.zst"
    # two frames, the second cut short: the first one stays readable
    truncated.write_bytes(zstd.compress(payload[:split]) + zstd.compress(payload[split:])[:-8])
    garbage = tmp_path / "rollout-garbage.jsonl.zst"
    garbage.write_bytes(b"\x28\xb5\x2f\xfd" + b"not zstd" * 8)
    empty = tmp_path / "rollout-empty.jsonl.zst"
    empty.write_bytes(b"")

    with caplog.at_level(logging.WARNING):
        truncated_result = _read_all(truncated)
        garbage_result = _read_all(garbage)
        empty_result = _read_all(empty)

    assert head
    assert truncated_result.records[: len(head)] == head
    assert truncated_result.records == full[: len(truncated_result.records)]
    assert len(truncated_result.records) < len(full)
    assert garbage_result.records == []
    assert empty_result.records == []
    for result in (truncated_result, garbage_result, empty_result):
        assert result.state.compressed_done is True
    assert sum("damaged" in message for message in caplog.messages) == 3


def _records_of(payload: dict[str, object]) -> list[dict[str, object]]:
    records = payload["records"]
    assert isinstance(records, list)
    return cast(list[dict[str, object]], records)


class _Response:
    def __init__(self, status: int, body: object) -> None:
        self.status_code = status
        self.body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self) -> object:
        return self.body


class _Server:
    """Fake server: responds like the new /ingest, like an old one, or with an error."""

    def __init__(self, behaviour: str = "ok") -> None:
        self.behaviour = behaviour
        self.payloads: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> object:
        self.payloads.append(json)
        records = _records_of(json)
        if self.behaviour == "error":
            return _Response(500, {})
        if self.behaviour == "old":
            return _Response(200, {"accepted": 0, "parsed": 0, "duplicates": 0, "errors": 0})
        return _Response(200, {"records_received": len(records)})


def _codex_cfg(root: Path) -> AgentConfig:
    return AgentConfig(
        server_url="http://server:8787",
        api_token="token",
        host_label="desktop",
        claude_projects_dir=root / "claude",
        codex_dirs=(root / "sessions", root / "archived_sessions"),
    )


def test_ship_codex_posts_records_and_commits_state(tmp_path: Path) -> None:
    path = _format_a_rollout().write(tmp_path / "sessions" / "rollout-a.jsonl")
    _format_b_rollout().write(tmp_path / "archived_sessions" / "rollout-b.jsonl")
    state = OffsetState(offsets={})
    server = _Server()

    sent, failed = ship_codex(_codex_cfg(tmp_path), state, server)
    again = ship_codex(_codex_cfg(tmp_path), state, server)

    assert (sent, failed) == (6, 0)
    assert again == (0, 0)
    assert len(server.payloads) == 1
    assert server.payloads[0]["host"] == "desktop"
    assert server.payloads[0]["batch"] == []
    assert state.codex[str(path)]["offset"] == path.stat().st_size


@pytest.mark.parametrize("behaviour", ["error", "old"])
def test_ship_codex_keeps_state_when_server_fails_or_is_old(
    tmp_path: Path,
    behaviour: str,
) -> None:
    _format_a_rollout().write(tmp_path / "sessions" / "rollout-a.jsonl")
    state = OffsetState(offsets={})

    sent, failed = ship_codex(_codex_cfg(tmp_path), state, _Server(behaviour))

    assert (sent, failed) == (0, 3)
    assert state.codex == {}


def test_ship_codex_splits_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ccmeter.client.agent.MAX_BATCH_RECORDS", 2)
    _format_a_rollout().write(tmp_path / "sessions" / "rollout-a.jsonl")
    _format_b_rollout().write(tmp_path / "sessions" / "rollout-b.jsonl")
    state = OffsetState(offsets={})
    server = _Server()

    sent, failed = ship_codex(_codex_cfg(tmp_path), state, server)

    assert (sent, failed) == (6, 0)
    sizes = [len(_records_of(payload)) for payload in server.payloads]
    assert sizes == [2, 2, 2]


def test_moved_rollout_is_reread_and_old_state_pruned(tmp_path: Path) -> None:
    source = _format_a_rollout().write(tmp_path / "sessions" / "rollout-a.jsonl")
    state = OffsetState(offsets={})
    server = _Server()
    ship_codex(_codex_cfg(tmp_path), state, server)
    target = tmp_path / "archived_sessions" / "rollout-a.jsonl"
    target.parent.mkdir(parents=True)
    source.replace(target)

    ship_codex(_codex_cfg(tmp_path), state, server)

    assert list(state.codex) == [str(target)]
    first_ids = [record["message_id"] for record in _records_of(server.payloads[0])]
    second_ids = [record["message_id"] for record in _records_of(server.payloads[1])]
    assert first_ids == second_ids


def test_ship_codex_reads_compressed_rollouts_once_until_rewritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rollout = _format_a_rollout()
    packed = rollout.write_zst(tmp_path / "sessions" / "rollout-a.jsonl.zst")
    damaged = tmp_path / "sessions" / "rollout-empty.jsonl.zst"
    damaged.write_bytes(b"")
    state = OffsetState(offsets={})
    server = _Server()
    reads: list[Path] = []

    def counting_read(
        path: Path,
        file_state: CodexFileState,
        *,
        max_records: int,
        now: float | None = None,
    ) -> CodexReadResult:
        reads.append(path)
        return read_codex_file(path, file_state, max_records=max_records, now=now)

    monkeypatch.setattr("ccmeter.client.agent.read_codex_file", counting_read)

    with caplog.at_level(logging.WARNING):
        first = ship_codex(_codex_cfg(tmp_path), state, server)
        reads_after_first = len(reads)
        second = ship_codex(_codex_cfg(tmp_path), state, server)
    reads_after_second = len(reads)
    rollout.token_count(usage(1, 0, 7, 0), usage(1_000_000, 0, 7_777, 0))
    rollout.write_zst(packed)
    third = ship_codex(_codex_cfg(tmp_path), state, server)

    assert first == (3, 0)
    assert reads_after_first == 2
    assert second == (0, 0)
    assert reads_after_second == reads_after_first
    assert sum("damaged" in message for message in caplog.messages) == 1
    # rewritten: read again from the start, the server drops the three it already has
    assert third == (4, 0)
    assert [record["output_tokens"] for record in _records_of(server.payloads[-1])][-1] == 7
    assert state.codex[str(packed)]["compressed_done"] is True
    assert state.codex[str(damaged)]["compressed_done"] is True


def test_agent_config_codex_dirs_default_and_override(tmp_path: Path) -> None:
    base = {"server_url": "http://s", "api_token": "t", "host_label": "desktop"}
    default_path = tmp_path / "default.json"
    default_path.write_text(json.dumps(base), encoding="utf-8")
    custom_path = tmp_path / "custom.json"
    custom_path.write_text(json.dumps({**base, "codex_dirs": ["D:/codex"]}), encoding="utf-8")
    off_path = tmp_path / "off.json"
    off_path.write_text(json.dumps({**base, "codex_dirs": []}), encoding="utf-8")

    default = load_agent_config(default_path)
    custom = load_agent_config(custom_path)
    off = load_agent_config(off_path)

    assert [path.name for path in default.codex_dirs] == ["sessions", "archived_sessions"]
    assert custom.codex_dirs == (Path("D:/codex"),)
    assert off.codex_dirs == ()


def test_state_file_roundtrip_keeps_codex_and_old_format(tmp_path: Path) -> None:
    old_state = tmp_path / "old.json"
    old_state.write_text(json.dumps({"offsets": {"a.jsonl": 10}}), encoding="utf-8")
    new_state = tmp_path / "new.json"
    state = OffsetState(offsets={"a.jsonl": 10}, codex={"r.jsonl": {"offset": 5, "mode": "B"}})

    save_offset_state(new_state, state)

    assert load_offset_state(old_state).codex == {}
    assert load_offset_state(new_state) == state
    assert json.loads(new_state.read_text(encoding="utf-8"))["offsets"] == {"a.jsonl": 10}
