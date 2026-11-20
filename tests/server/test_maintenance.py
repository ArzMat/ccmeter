"""Maintenance commands: minimize, process lock, backup."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccmeter.server import db as db_module
from ccmeter.server.app import create_app
from ccmeter.server.db import (
    backup_database,
    connect_db,
    ingest_batch,
    initialize_schema,
    minimize_raw_lines,
    update_raw_usage_daily,
)
from ccmeter.server.lock import DatabaseLocked, ProcessLock, lock_path_for
from ccmeter.server.pricing import BUNDLED_PRICES_PATH
from ccmeter.shared.claude_lines import minimize_line

FILE = "Project/session.jsonl"


def _assistant(message_id: str, *, text: str = "the answer", input_tokens: int = 100) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "sessionId": "s1",
            "uuid": f"u-{message_id}",
            "timestamp": "2026-07-04T10:00:00.000Z",
            "cwd": "/home/someone/project",
            "message": {
                "id": message_id,
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": input_tokens, "output_tokens": 10},
            },
        }
    )


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(connection)
    yield connection
    connection.close()


def _old_server_ingest(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    batch: list[dict[str, str]],
) -> None:
    """Ingest like a server before minimization: full lines stored as received."""
    with monkeypatch.context() as patch:
        patch.setattr(db_module, "minimize_line", _store_as_received)
        ingest_batch(conn, host="pc", batch=batch)


def _store_as_received(line: str) -> str:
    return line


def _raw_input_tokens(conn: sqlite3.Connection) -> int:
    update_raw_usage_daily(conn)
    row = conn.execute("SELECT COALESCE(SUM(input_tokens), 0) FROM raw_usage_daily").fetchone()
    return int(row[0])


def _raw_rows(conn: sqlite3.Connection) -> list[tuple[int | None, str]]:
    return [
        (row["byte_offset"], str(row["content"]))
        for row in conn.execute("SELECT byte_offset, content FROM raw_lines ORDER BY id")
    ]


def test_server_minimizes_lines_from_old_agents(conn: sqlite3.Connection) -> None:
    line = _assistant("m1", text="SECRET")

    ingest_batch(conn, host="pc", batch=[{"file": FILE, "line": line, "offset": "0"}])

    stored = _raw_rows(conn)
    assert stored == [(0, minimize_line(line))]
    assert "SECRET" not in stored[0][1]
    assert "someone" not in stored[0][1]


def test_minimize_rewrites_full_lines_and_is_idempotent(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = [_assistant("m1", text="SECRET one"), _assistant("m2", text="SECRET two")]
    _old_server_ingest(
        conn,
        monkeypatch,
        [
            {"file": FILE, "line": line, "offset": str(index * 500)}
            for index, line in enumerate(lines)
        ],
    )
    before = _raw_input_tokens(conn)

    dry = minimize_raw_lines(conn, dry_run=True)
    assert dry.rewritten == 2
    assert all("SECRET" in content for _offset, content in _raw_rows(conn))

    first = minimize_raw_lines(conn, dry_run=False)
    second = minimize_raw_lines(conn, dry_run=False)

    assert first.rewritten == 2
    assert first.bytes_after < first.bytes_before
    assert second.rewritten == 0
    assert all("SECRET" not in content for _offset, content in _raw_rows(conn))
    assert _raw_input_tokens(conn) == before


def test_minimize_drops_the_reshipped_copy_and_its_raw_tokens(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-1: a full line and its minimized re-send were both counted in the raw aggregate."""
    line = _assistant("m1")
    _old_server_ingest(conn, monkeypatch, [{"file": FILE, "line": line, "offset": "0"}])
    assert _raw_input_tokens(conn) == 100
    ingest_batch(conn, host="pc", batch=[{"file": FILE, "line": line, "offset": "0"}])
    assert _raw_input_tokens(conn) == 200

    result = minimize_raw_lines(conn, dry_run=False)

    assert result.conflicts == 1
    assert len(_raw_rows(conn)) == 1
    assert _raw_input_tokens(conn) == 100
    minimize_raw_lines(conn, dry_run=False)
    assert _raw_input_tokens(conn) == 100
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_minimize_never_merges_a_line_without_offset_with_a_line_with_one(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-2: identical lines are legitimate, so only same-offset copies are deduplicated."""
    line = _assistant("m1")
    _old_server_ingest(conn, monkeypatch, [{"file": FILE, "line": line}])
    ingest_batch(conn, host="pc", batch=[{"file": FILE, "line": line, "offset": "0"}])

    dry = minimize_raw_lines(conn, dry_run=True)
    minimize_raw_lines(conn, dry_run=False)

    assert dry.null_offsets == 1
    assert [offset for offset, _content in _raw_rows(conn)] == [None, 0]


def test_identical_lines_at_different_offsets_stay_distinct(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = _assistant("m1")
    _old_server_ingest(
        conn,
        monkeypatch,
        [
            {"file": FILE, "line": line, "offset": "0"},
            {"file": FILE, "line": line, "offset": "900"},
        ],
    )

    result = minimize_raw_lines(conn, dry_run=False)

    assert result.conflicts == 0
    assert len(_raw_rows(conn)) == 2


def test_lock_excludes_a_second_holder(tmp_path: Path) -> None:
    db_path = tmp_path / "ccmeter.db"
    with ProcessLock(db_path), pytest.raises(DatabaseLocked):
        ProcessLock(db_path).acquire()
    with ProcessLock(db_path):
        pass


def _hold_lock_in_subprocess(db_path: Path) -> subprocess.Popen[str]:
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from ccmeter.server.lock import ProcessLock\n"
        "lock = ProcessLock(Path(sys.argv[1]))\n"
        "lock.acquire()\n"
        "print('locked', flush=True)\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(db_path)], stdout=subprocess.PIPE, text=True
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "locked"
    return process


def test_lock_held_by_another_process_and_released_when_it_is_killed(tmp_path: Path) -> None:
    db_path = tmp_path / "ccmeter.db"
    process = _hold_lock_in_subprocess(db_path)
    try:
        with pytest.raises(DatabaseLocked):
            ProcessLock(db_path).acquire()
    finally:
        process.kill()
        process.wait(10)

    deadline = time.monotonic() + 5
    while True:
        try:
            with ProcessLock(db_path):
                break
        except DatabaseLocked:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    assert lock_path_for(db_path).exists()


def test_backup_of_a_live_database_restores_and_serves_the_same_numbers(tmp_path: Path) -> None:
    live = tmp_path / "live" / "ccmeter.db"
    conn = connect_db(live)
    initialize_schema(conn)
    ingest_batch(conn, host="pc", batch=[{"file": FILE, "line": _assistant("m1"), "offset": "0"}])
    with ProcessLock(live):
        backup_database(live, tmp_path / "backup" / "ccmeter.db")
        conn.close()
        with pytest.raises(FileExistsError):
            backup_database(live, tmp_path / "backup" / "ccmeter.db")

        restored = tmp_path / "backup" / "ccmeter.db"
        check = connect_db(restored)
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        check.close()
        with ProcessLock(restored):
            numbers = [
                TestClient(create_app(db_path=path, api_token="t", prices_path=BUNDLED_PRICES_PATH))
                .get("/api/summary")
                .json()
                for path in (live, restored)
            ]
    assert numbers[0] == numbers[1]
    assert numbers[0]["total_cost_usd"] > 0
