from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ccmeter.client.agent import (
    MAX_LINE_BYTES,
    AgentConfig,
    OffsetState,
    load_agent_config,
    load_offset_state,
    run_agent_loop,
    save_offset_state,
    scan_and_ship,
)
from ccmeter.client.config import DEFAULT_CODEX_DIRS, write_agent_config
from ccmeter.shared.claude_lines import minimize_line


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _line(index: int) -> str:
    return (
        f'{{"type":"user","sessionId":"s","uuid":"u-{index}",'
        '"message":{"content":"p","role":"user"}}'
    )


class FakeHttpClient:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.requests: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> object:
        self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse(self.statuses.pop(0))


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


def _cfg(root: Path) -> AgentConfig:
    return AgentConfig(
        server_url="http://server:8787",
        api_token="token",
        host_label="desktop",
        claude_projects_dir=root,
    )


def test_scan_and_ship_sends_new_lines_and_advances_offsets(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    _write(transcript, _line(1) + "\n" + _line(2) + "\n")
    state = OffsetState(offsets={})
    client = FakeHttpClient([200])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    assert result.sent == 2
    assert result.failed == 0
    assert len(client.requests) == 1
    payload = client.requests[0]["json"]
    second_offset = transcript.read_bytes().index(b"\n") + 1
    assert payload == {
        "host": "desktop",
        "batch": [
            {"file": "Project/session.jsonl", "line": minimize_line(_line(1)), "offset": 0},
            {
                "file": "Project/session.jsonl",
                "line": minimize_line(_line(2)),
                "offset": second_offset,
            },
        ],
    }
    assert state.offsets[str(transcript)] == transcript.stat().st_size


def test_scan_and_ship_reads_only_appended_lines(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    _write(transcript, _line(1) + "\n")
    initial_offset = transcript.stat().st_size
    state = OffsetState(offsets={str(transcript): initial_offset})
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_line(2) + "\n")
    client = FakeHttpClient([200])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    payload = client.requests[0]["json"]
    assert result.sent == 1
    assert payload == {
        "host": "desktop",
        "batch": [
            {
                "file": "Project/session.jsonl",
                "line": minimize_line(_line(2)),
                "offset": initial_offset,
            }
        ],
    }
    assert state.offsets[str(transcript)] == transcript.stat().st_size


def test_scan_and_ship_resets_offset_when_file_is_truncated(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    _write(transcript, _line(1) + "\n")
    state = OffsetState(offsets={str(transcript): 10_000})
    client = FakeHttpClient([200])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    assert result.sent == 1
    assert state.offsets[str(transcript)] == transcript.stat().st_size


def test_scan_and_ship_does_not_send_final_partial_line(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    _write(transcript, _line(1) + "\n" + '{"type":"assistant"')
    state = OffsetState(offsets={})
    client = FakeHttpClient([200])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    payload = client.requests[0]["json"]
    file_bytes = transcript.read_bytes()
    expected_offset = file_bytes.index(b"\n") + 1
    assert result.sent == 1
    assert payload == {
        "host": "desktop",
        "batch": [{"file": "Project/session.jsonl", "line": minimize_line(_line(1)), "offset": 0}],
    }
    assert state.offsets[str(transcript)] == expected_offset


def test_scan_and_ship_replaces_invalid_utf8_without_crashing(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_bytes(_line(1).encode("utf-8") + b"\n\xff\n")
    state = OffsetState(offsets={})
    client = FakeHttpClient([200])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    payload = client.requests[0]["json"]
    second_offset = transcript.read_bytes().index(b"\n") + 1
    assert result.sent == 2
    assert payload == {
        "host": "desktop",
        "batch": [
            {"file": "Project/session.jsonl", "line": minimize_line(_line(1)), "offset": 0},
            # not JSON once decoded: minimized to an empty line, recorded as unparseable
            {"file": "Project/session.jsonl", "line": "", "offset": second_offset},
        ],
    }
    assert state.offsets[str(transcript)] == transcript.stat().st_size


def _huge_kept_field_line() -> str:
    """A line still too large AFTER minimization: its size sits in a field the parser keeps."""
    return json.dumps({"type": "user", "sessionId": "s", "uuid": "u" * (MAX_LINE_BYTES + 1)})


def test_scan_and_ship_ships_huge_text_lines_minimized_instead_of_dropping_them(
    tmp_path: Path,
) -> None:
    """Before minimization a line over the server limit was dropped with its usage.

    Its text is what made it large, so the minimized line is small and goes through.
    """
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    huge = json.dumps(
        {
            "type": "assistant",
            "sessionId": "s",
            "message": {"id": "m1", "model": "claude-x", "content": "x" * (MAX_LINE_BYTES + 1)},
        }
    )
    _write(transcript, huge + "\n" + _line(2) + "\n")
    state = OffsetState(offsets={})
    client = FakeHttpClient([200])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    batch = client.requests[0]["json"]["batch"]  # type: ignore[index]
    assert result.sent == 2
    assert batch[0]["line"] == minimize_line(huge)  # type: ignore[index]
    assert len(batch[0]["line"]) < 200  # type: ignore[index, arg-type]
    assert state.offsets[str(transcript)] == transcript.stat().st_size


def test_scan_and_ship_skips_lines_too_large_for_server(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    _write(transcript, _huge_kept_field_line() + "\n" + _line(2) + "\n")
    state = OffsetState(offsets={})
    client = FakeHttpClient([200])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    payload = client.requests[0]["json"]
    second_offset = transcript.read_bytes().index(b"\n") + 1
    assert result.sent == 1
    assert result.failed == 0
    assert payload == {
        "host": "desktop",
        "batch": [
            {
                "file": "Project/session.jsonl",
                "line": minimize_line(_line(2)),
                "offset": second_offset,
            }
        ],
    }
    assert state.offsets[str(transcript)] == transcript.stat().st_size


def test_scan_and_ship_advances_offset_when_only_line_is_too_large(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    _write(transcript, _huge_kept_field_line() + "\n")
    state = OffsetState(offsets={})
    client = FakeHttpClient([])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    assert result.sent == 0
    assert result.failed == 0
    assert client.requests == []
    assert state.offsets[str(transcript)] == transcript.stat().st_size


def test_scan_and_ship_batches_at_500_lines(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    _write(transcript, "".join(_line(index) + "\n" for index in range(501)))
    state = OffsetState(offsets={})
    client = FakeHttpClient([200, 200])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    first_payload = client.requests[0]["json"]
    second_payload = client.requests[1]["json"]
    assert result.sent == 501
    assert len(first_payload["batch"]) == 500  # type: ignore[arg-type]
    assert len(second_payload["batch"]) == 1  # type: ignore[arg-type]
    assert state.offsets[str(transcript)] == transcript.stat().st_size


def test_scan_and_ship_does_not_advance_offset_when_server_fails(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    _write(transcript, _line(1) + "\n")
    state = OffsetState(offsets={})
    client = FakeHttpClient([500])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    assert result.sent == 0
    assert result.failed == 1
    assert state.offsets == {}


def test_scan_and_ship_skips_transcript_deleted_after_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listed file can vanish before it is read: Claude Code deletes old sessions.

    An unhandled FileNotFoundError on `stat()` used to kill the agent silently.
    """
    root = tmp_path / "projects"
    kept = root / "Project" / "kept.jsonl"
    gone = root / "Project" / "gone.jsonl"
    _write(kept, _line(1) + "\n")
    real_rglob = Path.rglob

    def rglob_with_ghost(self: Path, pattern: str) -> list[Path]:
        return [*real_rglob(self, pattern), gone]

    monkeypatch.setattr(Path, "rglob", rglob_with_ghost)
    state = OffsetState(offsets={})
    client = FakeHttpClient([200])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    assert result.sent == 1
    assert result.failed == 0
    assert state.offsets == {str(kept): kept.stat().st_size}


def test_scan_and_ship_skips_transcript_deleted_between_stat_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "projects"
    kept = root / "Project" / "kept.jsonl"
    gone = root / "Project" / "gone.jsonl"
    _write(kept, _line(1) + "\n")
    _write(gone, _line(2) + "\n")
    real_open = Path.open

    def open_after_delete(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == gone:
            self.unlink()
        return real_open(self, *args, **kwargs)  # pyright: ignore[reportUnknownVariableType]

    monkeypatch.setattr(Path, "open", open_after_delete)
    state = OffsetState(offsets={})
    client = FakeHttpClient([200])

    result = scan_and_ship(_cfg(root), state, http_client=client)

    assert result.sent == 1
    assert result.failed == 0
    assert state.offsets == {str(kept): kept.stat().st_size}


def test_offset_state_round_trips_to_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = OffsetState(offsets={"a.jsonl": 12})

    save_offset_state(path, state)
    loaded = load_offset_state(path)

    assert loaded.offsets == {"a.jsonl": 12}


def test_load_agent_config_from_json(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.json"
    _write(
        config_path,
        """
{
  "server_url": "http://server:8787",
  "api_token": "token",
  "host_label": "desktop",
  "claude_projects_dir": "C:/tmp/projects",
  "poll_seconds": 7
}
""".strip(),
    )

    cfg = load_agent_config(config_path)

    assert cfg.server_url == "http://server:8787"
    assert cfg.api_token == "token"
    assert cfg.host_label == "desktop"
    assert cfg.claude_projects_dir == Path("C:/tmp/projects")
    assert cfg.poll_seconds == 7


def test_run_agent_loop_persists_offsets_and_uses_backoff_on_failure(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    state_path = tmp_path / "state.json"
    _write(transcript, _line(1) + "\n")
    client = FakeHttpClient([500, 200])
    sleeps: list[float] = []

    run_agent_loop(
        _cfg(root),
        state_path=state_path,
        http_client=client,
        sleep=sleeps.append,
        max_iterations=2,
    )

    assert sleeps == [5, 5]
    assert load_offset_state(state_path).offsets[str(transcript)] == transcript.stat().st_size


def test_shipped_lines_carry_no_conversation_text(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    transcript = root / "Project" / "session.jsonl"
    secret = "SECRET-PROMPT-TEXT"
    lines = [
        {
            "type": "user",
            "sessionId": "s",
            "uuid": "u1",
            "cwd": f"C:/Users/someone/{secret}",
            "message": {"role": "user", "content": secret},
        },
        {
            "type": "user",
            "sessionId": "s",
            "uuid": "u2",
            "toolUseResult": {"stdout": secret},
            "message": {"content": [{"type": "tool_result", "content": secret}]},
        },
        {
            "type": "assistant",
            "sessionId": "s",
            "uuid": "u3",
            "message": {
                "id": "msg_1",
                "model": "claude-x",
                "content": [{"type": "text", "text": secret}],
                "usage": {"input_tokens": 3, "output_tokens": 5},
            },
        },
    ]
    _write(transcript, "".join(json.dumps(line) + "\n" for line in lines))
    client = FakeHttpClient([200])

    scan_and_ship(_cfg(root), OffsetState(offsets={}), http_client=client)

    sent = json.dumps(client.requests[0]["json"])
    shipped = [json.loads(item["line"]) for item in client.requests[0]["json"]["batch"]]  # type: ignore[index, union-attr]
    assert secret not in sent
    assert "someone" not in sent
    assert shipped[2]["message"]["usage"] == {"input_tokens": 3, "output_tokens": 5}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_idle_agent_sends_a_heartbeat_after_five_minutes_and_not_before(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    clock = FakeClock()
    client = FakeHttpClient([200])

    def sleep(seconds: float) -> None:
        clock.now += 100

    run_agent_loop(
        _cfg(root),
        state_path=tmp_path / "state.json",
        http_client=client,
        sleep=sleep,
        clock=clock,
        max_iterations=3,
    )
    assert client.requests == []

    run_agent_loop(
        _cfg(root),
        state_path=tmp_path / "state.json",
        http_client=client,
        sleep=sleep,
        clock=clock,
        max_iterations=4,
    )
    assert [request["json"] for request in client.requests] == [{"host": "desktop", "batch": []}]


def _requests_with_data_at_200s(tmp_path: Path, iterations: int) -> list[dict[str, object]]:
    """Run passes at t=0, 100, 200, ... with one transcript line appearing at t=200."""
    root = tmp_path / f"projects-{iterations}"
    transcript = root / "Project" / "session.jsonl"
    root.mkdir()
    clock = FakeClock()
    client = FakeHttpClient([200, 200])

    def sleep(seconds: float) -> None:
        clock.now += 100
        if clock.now == 200:
            _write(transcript, _line(1) + "\n")

    run_agent_loop(
        _cfg(root),
        state_path=tmp_path / f"state-{iterations}.json",
        http_client=client,
        sleep=sleep,
        clock=clock,
        max_iterations=iterations,
    )
    return client.requests


def test_shipping_data_postpones_the_heartbeat(tmp_path: Path) -> None:
    """Data shipped at t=200 restarts the 5 minute count: heartbeat at t=500, not at t=300."""
    until_400 = _requests_with_data_at_200s(tmp_path, 5)
    until_500 = _requests_with_data_at_200s(tmp_path, 6)

    assert len(until_400) == 1
    assert until_400[0]["json"]["batch"]  # type: ignore[index]
    assert len(until_500) == 2
    assert until_500[1]["json"] == {"host": "desktop", "batch": []}


def test_load_agent_config_accepts_a_byte_order_mark(tmp_path: Path) -> None:
    """Windows PowerShell 5 writes UTF-8 with a BOM, which plain json.loads rejects."""
    config_path = tmp_path / "agent.json"
    config_path.write_text(
        '{"server_url": "http://s:8787", "api_token": "t", "host_label": "pc"}',
        encoding="utf-8-sig",
    )

    cfg = load_agent_config(config_path)

    assert cfg.host_label == "pc"
    assert cfg.codex_dirs == DEFAULT_CODEX_DIRS


def test_write_agent_config_round_trips_and_refuses_to_overwrite(tmp_path: Path) -> None:
    config_path = tmp_path / "nested" / "agent.json"
    cfg = AgentConfig(
        server_url="http://s:8787",
        api_token="t",
        host_label="pc",
        claude_projects_dir=tmp_path / "projects",
        codex_dirs=(),
    )

    write_agent_config(config_path, cfg)
    with pytest.raises(FileExistsError):
        write_agent_config(config_path, cfg)
    write_agent_config(config_path, cfg, force=True)

    assert load_agent_config(config_path) == cfg


def test_existing_state_file_format_is_still_read(tmp_path: Path) -> None:
    """State written by earlier versions: an upgraded agent resumes from the same offsets."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "codex": {"C:/x/rollout-1.jsonl": {"offset": 120, "format": "B"}},
                "offsets": {"C:/x/projects/p/s.jsonl": 4096},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    state = load_offset_state(path)

    assert state.offsets == {"C:/x/projects/p/s.jsonl": 4096}
    assert state.codex == {"C:/x/rollout-1.jsonl": {"offset": 120, "format": "B"}}
