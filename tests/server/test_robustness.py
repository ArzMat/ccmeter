"""Regression tests for the issues found in the review of the server rework."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccmeter.cli import main as cli_main
from ccmeter.server import db as db_module
from ccmeter.server.app import create_app
from ccmeter.server.db import (
    backup_database,
    connect_db,
    ingest_batch,
    initialize_schema,
    minimize_raw_lines,
    reprocess_raw_lines,
    update_raw_usage_daily,
)

FILE = "Project/session.jsonl"
AUTH = {"Authorization": "Bearer secret-token"}


def _assistant(message_id: str, *, text: str = "the answer", session: str = "s1") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "sessionId": session,
            "uuid": f"u-{message_id}",
            "timestamp": "2026-07-04T10:00:00.000Z",
            "message": {
                "id": message_id,
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        }
    )


def _store_as_received(line: str) -> str:
    return line


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(connection)
    yield connection
    connection.close()


def _old_server_ingest(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, batch: list[dict[str, str]]
) -> None:
    """Ingest like a server before minimization: full lines stored as received."""
    with monkeypatch.context() as patch:
        patch.setattr(db_module, "minimize_line", _store_as_received)
        ingest_batch(conn, host="pc", batch=batch)


def _raw_input_tokens(conn: sqlite3.Connection) -> int:
    update_raw_usage_daily(conn)
    row = conn.execute("SELECT COALESCE(SUM(input_tokens), 0) FROM raw_usage_daily").fetchone()
    return int(row[0])


def _contents(conn: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in conn.execute("SELECT content FROM raw_lines ORDER BY id")]


def test_server_started_through_the_uvicorn_factory_holds_the_lock(tmp_path: Path) -> None:
    """`uvicorn ccmeter.server.app:create_default_app --factory` must block maintenance too."""
    db_path = tmp_path / "ccmeter.db"
    script = (
        "import time\n"
        "from ccmeter.server.app import create_default_app\n"
        "app = create_default_app()\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    env = {**os.environ, "CCMETER_API_TOKEN": "a-real-secret", "CCMETER_DB_PATH": str(db_path)}
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        assert cli_main(["server", "minimize", "--dry-run", "--db", str(db_path)]) == 1
    finally:
        process.kill()
        process.wait(10)


def test_backup_refuses_a_missing_or_foreign_source(tmp_path: Path) -> None:
    missing = tmp_path / "typo.db"
    foreign = tmp_path / "foreign.db"
    other = sqlite3.connect(foreign)
    other.execute("CREATE TABLE something (x)")
    other.close()

    with pytest.raises(FileNotFoundError):
        backup_database(missing, tmp_path / "out1.db")
    with pytest.raises(ValueError, match="not a ccmeter database"):
        backup_database(foreign, tmp_path / "out2.db")

    assert not missing.exists()
    assert not (tmp_path / "out1.db").exists()
    assert not (tmp_path / "out2.db").exists()


def test_reprocess_keeps_prompt_links_that_only_batch_boundaries_resolved(
    conn: sqlite3.Connection,
) -> None:
    """An agent's line read in a later batch than its prompt gets the prompt at ingest; the
    raw lines alone cannot rebuild that link, so reprocess keeps it."""
    main = "P/s.jsonl"
    sub = "P/s/subagents/agent-a.jsonl"
    user = json.dumps(
        {
            "type": "user",
            "sessionId": "s1",
            "uuid": "u-p1",
            "timestamp": "2026-07-04T09:00:00.000Z",
            "promptId": "p1",
            "message": {"content": "x"},
        }
    )
    ingest_batch(
        conn,
        host="pc",
        batch=[
            {"file": main, "line": user, "offset": "0"},
            {"file": main, "line": _assistant("a1"), "offset": "100"},
        ],
    )
    ingest_batch(conn, host="pc", batch=[{"file": sub, "line": _assistant("s1"), "offset": "0"}])
    before = {
        str(row[0]): int(row[1])
        for row in conn.execute("SELECT message_id, prompt_index FROM messages")
    }

    reprocess_raw_lines(conn)

    after = {
        str(row[0]): int(row[1])
        for row in conn.execute("SELECT message_id, prompt_index FROM messages")
    }
    assert before == {"a1": 1, "s1": 1}
    assert after == before


def test_interrupted_minimize_keeps_finished_chunks_and_resumes(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = [_assistant(f"m{index}", text="SECRET") for index in range(3)]
    _old_server_ingest(
        conn,
        monkeypatch,
        [
            {"file": FILE, "line": line, "offset": str(index * 500)}
            for index, line in enumerate(lines)
        ],
    )
    expected = _raw_input_tokens(conn)
    calls = {"n": 0}
    real_minimize = db_module.minimize_line

    def failing_minimize(line: str) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_minimize(line)

    monkeypatch.setattr(db_module, "MAINTENANCE_BATCH_ROWS", 1)
    with monkeypatch.context() as patch:
        patch.setattr(db_module, "minimize_line", failing_minimize)
        with pytest.raises(KeyboardInterrupt):
            minimize_raw_lines(conn, dry_run=False)
    conn.rollback()

    assert ["SECRET" in content for content in _contents(conn)] == [False, True, True]
    assert conn.execute("SELECT COUNT(*) FROM raw_usage_daily").fetchone()[0] == 0
    assert _raw_input_tokens(conn) == expected
    minimize_raw_lines(conn, dry_run=False)
    assert all("SECRET" not in content for content in _contents(conn))
    assert _raw_input_tokens(conn) == expected


def test_dry_run_counts_conflicts_between_rows_rewritten_in_the_same_pass(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two full lines without offset that differ only in dropped text become one row."""
    first = _assistant("m1")
    second = json.dumps({**json.loads(first), "cwd": "/somewhere/else"})
    _old_server_ingest(conn, monkeypatch, [{"file": FILE, "line": first}])
    _old_server_ingest(conn, monkeypatch, [{"file": FILE, "line": second}])

    preview = minimize_raw_lines(conn, dry_run=True)
    result = minimize_raw_lines(conn, dry_run=False)

    assert preview.conflicts == result.conflicts == 1
    assert len(_contents(conn)) == 1


def test_last_data_moves_only_when_something_new_is_accepted(tmp_path: Path) -> None:
    db_path = tmp_path / "ccmeter.db"
    client = TestClient(create_app(db_path=db_path, api_token="secret-token"))
    batch = [{"file": FILE, "line": _assistant("m1"), "offset": 0}]
    excluded = {
        "provider": "codex",
        "message_id": "guardian-1",
        "session_id": "cs",
        "timestamp": "2026-07-20T10:00:00.000Z",
        "model": "codex-auto-review",
        "input_tokens": 1,
        "output_tokens": 1,
    }
    client.post("/ingest", headers=AUTH, json={"host": "pc", "batch": batch})
    conn = connect_db(db_path)
    conn.execute("UPDATE meta SET value = '2000-01-01 00:00:00' WHERE key LIKE 'host_last_%'")
    conn.commit()
    conn.close()

    client.post("/ingest", headers=AUTH, json={"host": "pc", "batch": batch})
    client.post("/ingest", headers=AUTH, json={"host": "pc", "records": [excluded]})
    hosts = client.get("/api/health").json()["hosts"]

    assert hosts["pc"]["last_data"] == "2000-01-01 00:00:00"
    assert hosts["pc"]["last_seen"] > "2000-01-01 00:00:00"


def test_failed_ingest_commits_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "ccmeter.db"
    client = TestClient(
        create_app(db_path=db_path, api_token="secret-token"), raise_server_exceptions=False
    )
    record = {
        "provider": "codex",
        "message_id": "r1",
        "session_id": "cs",
        "timestamp": "2026-07-20T10:00:00.000Z",
        "model": "gpt-5.5",
        "input_tokens": 1,
        "output_tokens": 1,
    }

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("disk full")

    monkeypatch.setattr("ccmeter.server.app.ingest_usage_records", broken)
    response = client.post(
        "/ingest",
        headers=AUTH,
        json={
            "host": "pc",
            "batch": [{"file": FILE, "line": _assistant("m1")}],
            "records": [record],
        },
    )

    check = connect_db(db_path)
    assert response.status_code == 500
    assert check.execute("SELECT COUNT(*) FROM raw_lines").fetchone()[0] == 0
    assert (
        check.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'host_last_%'").fetchone()[0] == 0
    )
    check.close()


def test_unknown_providers_never_take_a_cache_slot(tmp_path: Path) -> None:
    db_path = tmp_path / "ccmeter.db"
    client = TestClient(create_app(db_path=db_path, api_token="secret-token"))
    client.post(
        "/ingest",
        headers=AUTH,
        json={"host": "pc", "batch": [{"file": FILE, "line": _assistant("m")}]},
    )

    for index in range(20):
        assert client.get(f"/api/dashboard/overview?provider=nope{index}").status_code == 404
    assert client.get("/api/dashboard/overview?provider=claude").status_code == 200
