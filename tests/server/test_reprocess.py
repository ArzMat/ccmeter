from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from ccmeter.cli import main
from ccmeter.server.db import connect_db, ingest_batch, initialize_schema, reprocess_raw_lines

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8").splitlines()


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    return conn


def test_reprocess_rebuilds_normalized_tables_from_raw_lines(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    lines = _fixture_lines("main_session_extract.jsonl")
    ingest_batch(
        conn,
        host="desktop",
        batch=[
            {"file": "C--work-demo-app/session.jsonl", "line": lines[0]},
            {"file": "C--work-demo-app/session.jsonl", "line": lines[1]},
            {"file": "C--work-demo-app/session.jsonl", "line": '{"type":"assistant"'},
        ],
    )
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM sessions")
    conn.execute("UPDATE raw_lines SET parsed = 0, error = 'stale'")
    conn.commit()

    result = reprocess_raw_lines(conn)

    assert result.raw_lines == 3
    assert result.parsed == 2
    assert result.errors == 1
    assert result.messages == 1
    assert result.sessions == 1
    assert conn.execute("SELECT COUNT(*) FROM raw_lines").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    row = conn.execute("SELECT parsed, error FROM raw_lines WHERE parsed = 0").fetchone()
    assert row["parsed"] == 0
    assert "json" in row["error"].lower()


def test_reprocess_preserves_message_dedup_max_counters(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    lines = _fixture_lines("duplicate_message_id_extract.jsonl")
    higher_output = cast(dict[str, Any], json.loads(lines[2]))
    message = cast(dict[str, Any], higher_output["message"])
    usage = cast(dict[str, Any], message["usage"])
    usage["output_tokens"] = 7000
    ingest_batch(
        conn,
        host="desktop",
        batch=[
            {"file": "C--work-demo-app/session.jsonl", "line": lines[0]},
            {"file": "C--work-demo-app/session.jsonl", "line": lines[1]},
            {"file": "C--work-demo-app/session.jsonl", "line": json.dumps(higher_output)},
        ],
    )
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM sessions")
    conn.commit()

    result = reprocess_raw_lines(conn)

    row = conn.execute("SELECT output_tokens FROM messages").fetchone()
    assert result.raw_lines == 3
    assert result.messages == 1
    assert result.duplicates == 2
    assert row["output_tokens"] == 7000


def test_reprocess_cli_rebuilds_database(tmp_path: Path, capsys: Any) -> None:
    db_path = tmp_path / "ccmeter.db"
    conn = connect_db(db_path)
    initialize_schema(conn)
    lines = _fixture_lines("main_session_extract.jsonl")
    ingest_batch(
        conn,
        host="desktop",
        batch=[
            {"file": "C--work-demo-app/session.jsonl", "line": lines[0]},
            {"file": "C--work-demo-app/session.jsonl", "line": lines[1]},
        ],
    )
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM sessions")
    conn.commit()
    conn.close()

    exit_code = main(["server", "reprocess", "--db", str(db_path)])

    output = capsys.readouterr().out
    check_conn = connect_db(db_path)
    assert exit_code == 0
    assert "reprocessed raw=2 parsed=2 errors=0 messages=1 sessions=1 duplicates=0" in output
    assert check_conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
