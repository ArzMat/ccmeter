from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from ccmeter.server.db import connect_db, ingest_batch, initialize_schema

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8").splitlines()


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    return conn


def test_deduplicates_assistant_messages_by_message_id_and_keeps_max_output(
    tmp_path: Path,
) -> None:
    lines = _fixture_lines("duplicate_message_id_extract.jsonl")
    higher_output = cast(dict[str, Any], json.loads(lines[2]))
    message = cast(dict[str, Any], higher_output["message"])
    usage = cast(dict[str, Any], message["usage"])
    usage["output_tokens"] = 7000
    batch = [
        {"file": "C--work-demo-app/session.jsonl", "line": lines[0]},
        {"file": "C--work-demo-app/session.jsonl", "line": lines[1]},
        {"file": "C--work-demo-app/session.jsonl", "line": json.dumps(higher_output)},
    ]
    conn = _db(tmp_path)

    result = ingest_batch(conn, host="desktop", batch=batch)

    row = conn.execute("SELECT message_id, output_tokens, raw_line_id FROM messages").fetchone()
    assert result.accepted == 3
    assert result.parsed == 3
    assert result.duplicates == 2
    assert result.errors == 0
    assert row["message_id"] == "msg_gPu7cuJN9vuFN2yz0ipSpXfu"
    assert row["output_tokens"] == 7000
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM raw_lines").fetchone()[0] == 3


def test_reingesting_same_raw_batch_is_idempotent(tmp_path: Path) -> None:
    line = _fixture_lines("main_session_extract.jsonl")[1]
    batch = [{"file": "C--work-demo-app/session.jsonl", "line": line}]
    conn = _db(tmp_path)

    first = ingest_batch(conn, host="desktop", batch=batch)
    second = ingest_batch(conn, host="desktop", batch=batch)

    assert first.accepted == 1
    assert first.parsed == 1
    assert first.duplicates == 0
    assert second.accepted == 0
    assert second.parsed == 0
    assert second.duplicates == 1
    assert second.errors == 0
    assert conn.execute("SELECT COUNT(*) FROM raw_lines").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_prompt_id_is_propagated_to_assistant_messages_per_file(tmp_path: Path) -> None:
    batch = [
        {"file": "C--work-demo-app/session.jsonl", "line": line}
        for line in _fixture_lines("prompt_sequence_extract.jsonl")
    ]
    conn = _db(tmp_path)

    result = ingest_batch(conn, host="desktop", batch=batch)

    rows = conn.execute(
        """
        SELECT message_id, prompt_id, prompt_index
        FROM messages
        ORDER BY ts
        """
    ).fetchall()
    assert result.errors == 0
    assert {row["prompt_id"] for row in rows} == {"7321d145-3376-4db0-8cc4-9b00be261255"}
    assert {row["prompt_index"] for row in rows} == {1}


def test_unparseable_line_is_saved_raw_first_without_failing_batch(tmp_path: Path) -> None:
    conn = _db(tmp_path)

    result = ingest_batch(
        conn,
        host="desktop",
        batch=[{"file": "bad.jsonl", "line": '{"type":"assistant"'}],
    )

    row = conn.execute("SELECT parsed, error FROM raw_lines").fetchone()
    assert result.accepted == 1
    assert result.parsed == 0
    assert result.duplicates == 0
    assert result.errors == 1
    assert row["parsed"] == 0
    assert "json" in row["error"].lower()
