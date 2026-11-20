from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from ccmeter.server.app import MAX_LINE_BYTES, create_app
from ccmeter.server.db import (
    connect_db,
    host_contacts,
    ingest_batch,
    ingest_usage_records,
    initialize_schema,
    reprocess_raw_lines,
)
from ccmeter.shared.models import ProviderUsage

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


def _client(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection]:
    db_path = tmp_path / "ccmeter.db"
    conn = connect_db(db_path)
    initialize_schema(conn)
    client = TestClient(create_app(db_path=db_path, api_token="secret-token"))
    return client, conn


def test_ingest_requires_bearer_token(tmp_path: Path) -> None:
    client, _conn = _client(tmp_path)

    response = client.post("/ingest", json={"host": "desktop", "batch": []})

    assert response.status_code == 401


def test_ingest_accepts_real_fixture_and_stores_raw_before_parsing(tmp_path: Path) -> None:
    client, conn = _client(tmp_path)
    lines = (FIXTURES_DIR / "main_session_extract.jsonl").read_text(encoding="utf-8").splitlines()

    response = client.post(
        "/ingest",
        headers={"Authorization": "Bearer secret-token"},
        json={
            "host": "desktop",
            "batch": [
                {"file": "C--work-demo-app/session.jsonl", "line": lines[0]},
                {"file": "C--work-demo-app/session.jsonl", "line": lines[1]},
                {"file": "C--work-demo-app/session.jsonl", "line": '{"type":"assistant"'},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": 3, "parsed": 2, "duplicates": 0, "errors": 1}
    assert conn.execute("SELECT COUNT(*) FROM raw_lines").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM raw_lines WHERE parsed = 0").fetchone()[0] == 1


def test_ingest_rejects_path_traversal(tmp_path: Path) -> None:
    client, _conn = _client(tmp_path)
    line = (FIXTURES_DIR / "main_session_extract.jsonl").read_text(encoding="utf-8").splitlines()[1]

    response = client.post(
        "/ingest",
        headers={"Authorization": "Bearer secret-token"},
        json={"host": "desktop", "batch": [{"file": "../secret.jsonl", "line": line}]},
    )

    assert response.status_code == 400


def test_ingest_rejects_windows_absolute_path(tmp_path: Path) -> None:
    client, _conn = _client(tmp_path)
    line = (FIXTURES_DIR / "main_session_extract.jsonl").read_text(encoding="utf-8").splitlines()[1]

    response = client.post(
        "/ingest",
        headers={"Authorization": "Bearer secret-token"},
        json={"host": "desktop", "batch": [{"file": "C:\\secret\\session.jsonl", "line": line}]},
    )

    assert response.status_code == 400


def test_ingest_rejects_oversized_line(tmp_path: Path) -> None:
    client, _conn = _client(tmp_path)

    response = client.post(
        "/ingest",
        headers={"Authorization": "Bearer secret-token"},
        json={
            "host": "desktop",
            "batch": [{"file": "big.jsonl", "line": "x" * (MAX_LINE_BYTES + 1)}],
        },
    )

    assert response.status_code == 413


def test_ingest_deduplicates_raw_lines_by_file_offset_when_present(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    line = (FIXTURES_DIR / "main_session_extract.jsonl").read_text(encoding="utf-8").splitlines()[1]

    result = ingest_batch(
        conn,
        host="desktop",
        batch=[
            {"file": "Project/session.jsonl", "line": line, "offset": "0"},
            {"file": "Project/session.jsonl", "line": line, "offset": "200"},
            {"file": "Project/session.jsonl", "line": line, "offset": "200"},
        ],
    )

    rows = conn.execute("SELECT byte_offset FROM raw_lines ORDER BY byte_offset").fetchall()
    assert result.accepted == 2
    assert result.duplicates == 2
    assert [row["byte_offset"] for row in rows] == [0, 200]
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_schema_indexes_raw_line_health_fields(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)

    indexes = {row["name"] for row in conn.execute("PRAGMA index_list(raw_lines)").fetchall()}

    assert "idx_raw_host" in indexes
    assert "idx_raw_received_ts" in indexes
    assert "idx_raw_parsed" in indexes


def _assistant_line(session: str, message_id: str, *, prompt: str | None = None) -> str:
    payload: dict[str, object] = {
        "type": "assistant",
        "sessionId": session,
        "uuid": f"u-{message_id}",
        "timestamp": "2026-07-04T10:00:00.000Z",
        "message": {"id": message_id, "model": "claude-opus-4-8", "usage": {"input_tokens": 1}},
    }
    if prompt is not None:
        payload["promptId"] = prompt
    return json.dumps(payload)


def _user_line(session: str, prompt: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "sessionId": session,
            "uuid": f"user-{prompt}",
            "timestamp": "2026-07-04T09:59:00.000Z",
            "promptId": prompt,
            "message": {"role": "user", "content": "text"},
        }
    )


def _prompt_index_of(conn: sqlite3.Connection, message_id: str) -> int:
    row = conn.execute(
        "SELECT prompt_index FROM messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    return int(row[0])


def test_prompt_of_a_batch_continues_from_earlier_batches_but_not_from_its_own_rows(
    tmp_path: Path,
) -> None:
    """Assistant lines without a user line in the same file and batch take the session's
    last prompt as it was BEFORE the batch, like the eager loader this replaced."""
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    main = "P/s.jsonl"
    sub = "P/s/subagents/agent-a.jsonl"

    ingest_batch(
        conn,
        host="pc",
        batch=[
            {"file": main, "line": _user_line("s", "p1")},
            {"file": main, "line": _assistant_line("s", "a1")},
        ],
    )
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _assistant_line("s", "a2")}])
    ingest_batch(
        conn,
        host="pc",
        batch=[
            {"file": main, "line": _user_line("s", "p2")},
            {"file": main, "line": _assistant_line("s", "a3")},
            {"file": sub, "line": _assistant_line("s", "s1")},
        ],
    )

    assert _prompt_index_of(conn, "a1") == 1
    assert _prompt_index_of(conn, "a2") == 1
    assert _prompt_index_of(conn, "a3") == 2
    assert _prompt_index_of(conn, "s1") == 1


def _codex_record(message_id: str) -> ProviderUsage:
    return ProviderUsage(
        provider="codex",
        message_id=message_id,
        session_id="codex-session",
        thread_id="codex-session",
        turn_id=f"turn-{message_id}",
        is_sidechain=False,
        timestamp="2026-07-04T10:00:00.000Z",
        model="gpt-5.5",
        project="demo",
        input_tokens=10,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=5,
        reasoning_tokens=0,
        service_tier=None,
    )


def _prompt_of(conn: sqlite3.Connection, message_id: str) -> tuple[str | None, int]:
    row = conn.execute(
        "SELECT prompt_id, prompt_index FROM messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row[0], int(row[1])


def test_a_prompt_shipped_alone_owns_the_reply_of_the_next_batch(tmp_path: Path) -> None:
    """The agent can ship a prompt while Claude is still answering: the reply arrives in a
    later batch and must still belong to that prompt, the first one or a later one."""
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    main = "P/s.jsonl"

    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _user_line("s", "p1")}])
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _assistant_line("s", "a1")}])
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _user_line("s", "p2")}])
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _assistant_line("s", "a2")}])
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _assistant_line("s", "a3")}])

    assert _prompt_of(conn, "a1") == ("p1", 1)
    assert _prompt_of(conn, "a2") == ("p2", 2)
    assert _prompt_of(conn, "a3") == ("p2", 2)


def test_prompt_cursors_are_kept_per_host(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    main = "P/s.jsonl"

    ingest_batch(conn, host="desk", batch=[{"file": main, "line": _user_line("s", "p1")}])
    ingest_batch(conn, host="laptop", batch=[{"file": main, "line": _assistant_line("s", "x")}])

    assert _prompt_of(conn, "x") == (None, 0)


def test_a_cursor_older_than_the_messages_of_its_file_does_not_win(tmp_path: Path) -> None:
    """A server without cursors (a rollback) may have ingested newer prompts of the file."""
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    main = "P/s.jsonl"
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _user_line("s", "p1")}])
    stale = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'prompt_cursor:%'").fetchone()
    ingest_batch(
        conn,
        host="pc",
        batch=[
            {"file": main, "line": _assistant_line("s", "a1")},
            {"file": main, "line": _user_line("s", "p2")},
            {"file": main, "line": _assistant_line("s", "a2")},
        ],
    )
    # as if an older server had ingested that batch: the cursor still says p1
    conn.execute("UPDATE meta SET value = ? WHERE key = ?", (stale[1], stale[0]))
    conn.commit()

    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _assistant_line("s", "a3")}])

    assert _prompt_of(conn, "a3") == ("p2", 2)


def test_an_older_server_reusing_the_index_of_a_waiting_prompt_is_noticed(
    tmp_path: Path,
) -> None:
    """An older server gives the next prompt the index of the one still waiting (it only
    counts prompts with messages): the cursor must not keep the waiting prompt."""
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    main = "P/s.jsonl"
    ingest_batch(
        conn,
        host="pc",
        batch=[
            {"file": main, "line": _user_line("s", "p1")},
            {"file": main, "line": _assistant_line("s", "a1")},
        ],
    )
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _user_line("s", "p2")}])
    stale = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'prompt_cursor:%'").fetchone()
    ingest_batch(
        conn,
        host="pc",
        batch=[
            {"file": main, "line": _user_line("s", "p3")},
            {"file": main, "line": _assistant_line("s", "a3")},
        ],
    )
    assert _prompt_of(conn, "a3") == ("p3", 2)
    # as if an older server had ingested that batch: the cursor still says p2, index 2
    conn.execute("UPDATE meta SET value = ? WHERE key = ?", (stale[1], stale[0]))
    conn.commit()

    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _assistant_line("s", "a4")}])

    assert _prompt_of(conn, "a4") == ("p3", 2)


def test_a_waiting_prompt_survives_a_reprocess(tmp_path: Path) -> None:
    """Reprocess gives every Claude message a new id, above the Codex rows it keeps."""
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    main = "P/s.jsonl"
    ingest_batch(
        conn,
        host="pc",
        batch=[
            {"file": main, "line": _user_line("s", "p1")},
            {"file": main, "line": _assistant_line("s", "a1")},
        ],
    )
    ingest_usage_records(conn, host="pc", records=[_codex_record("r1")])
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _user_line("s", "p2")}])

    reprocess_raw_lines(conn)
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _assistant_line("s", "a2")}])

    assert _prompt_of(conn, "a1") == ("p1", 1)
    assert _prompt_of(conn, "a2") == ("p2", 2)


def test_another_file_of_the_session_does_not_take_over_a_waiting_prompt(
    tmp_path: Path,
) -> None:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    first, second = "P/s.jsonl", "P/s-resumed.jsonl"
    ingest_batch(
        conn,
        host="pc",
        batch=[
            {"file": first, "line": _user_line("s", "p1")},
            {"file": first, "line": _assistant_line("s", "a1")},
        ],
    )
    ingest_batch(conn, host="pc", batch=[{"file": first, "line": _user_line("s", "p2")}])
    ingest_batch(
        conn,
        host="pc",
        batch=[
            {"file": second, "line": _user_line("s", "p3")},
            {"file": second, "line": _assistant_line("s", "b1")},
        ],
    )

    ingest_batch(conn, host="pc", batch=[{"file": first, "line": _assistant_line("s", "a2")}])

    assert _prompt_of(conn, "a2") == ("p2", 2)
    assert _prompt_of(conn, "b1") == ("p3", 3)


def test_prompts_waiting_in_different_files_get_different_indexes(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    first, second = "P/s.jsonl", "P/s-resumed.jsonl"

    ingest_batch(conn, host="pc", batch=[{"file": first, "line": _user_line("s", "p1")}])
    ingest_batch(conn, host="pc", batch=[{"file": second, "line": _user_line("s", "p2")}])
    ingest_batch(conn, host="pc", batch=[{"file": second, "line": _assistant_line("s", "b1")}])
    ingest_batch(conn, host="pc", batch=[{"file": first, "line": _assistant_line("s", "a1")}])

    assert _prompt_of(conn, "a1") == ("p1", 1)
    assert _prompt_of(conn, "b1") == ("p2", 2)


def test_the_same_prompt_in_two_files_shares_its_index(tmp_path: Path) -> None:
    """A subagent's first line carries the prompt that started it."""
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    main, sub = "P/s.jsonl", "P/s/subagents/agent-a.jsonl"

    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _user_line("s", "p1")}])
    ingest_batch(conn, host="pc", batch=[{"file": sub, "line": _user_line("s", "p1")}])
    ingest_batch(conn, host="pc", batch=[{"file": sub, "line": _assistant_line("s", "s1")}])
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _assistant_line("s", "a1")}])

    assert _prompt_of(conn, "s1") == ("p1", 1)
    assert _prompt_of(conn, "a1") == ("p1", 1)


def test_an_interrupted_prompt_leaves_no_gap(tmp_path: Path) -> None:
    """A prompt stopped before any reply is replaced by the next one of the same file."""
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    main = "P/s.jsonl"

    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _user_line("s", "p1")}])
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _user_line("s", "p2")}])
    ingest_batch(conn, host="pc", batch=[{"file": main, "line": _assistant_line("s", "a2")}])

    assert _prompt_of(conn, "a2") == ("p2", 1)


def test_prompt_cursors_live_in_meta_and_the_schema_does_not_change(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    ingest_batch(conn, host="pc", batch=[{"file": "P/s.jsonl", "line": _user_line("s", "p1")}])

    after = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    keys = [row[0] for row in conn.execute("SELECT key FROM meta WHERE key LIKE 'prompt_cursor:%'")]
    assert after == tables
    assert keys == ['prompt_cursor:["s", "pc", "P/s.jsonl"]']


def test_ingest_records_when_each_host_was_seen_and_sent_data(tmp_path: Path) -> None:
    db_path = tmp_path / "ccmeter.db"
    client = TestClient(create_app(db_path=db_path, api_token="secret-token"))
    auth = {"Authorization": "Bearer secret-token"}
    old_event = _assistant_line("s", "late")

    client.post(
        "/ingest",
        headers=auth,
        json={"host": "laptop", "batch": [{"file": "P/s.jsonl", "line": old_event}]},
    )
    client.post("/ingest", headers=auth, json={"host": "desktop", "batch": []})
    hosts = client.get("/api/health").json()["hosts"]

    assert hosts["laptop"]["last_data"] is not None
    assert hosts["laptop"]["last_seen"] == hosts["laptop"]["last_data"]
    received = datetime.fromisoformat(hosts["laptop"]["last_data"]).replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - received).total_seconds()) < 120
    assert hosts["desktop"]["last_seen"] is not None
    assert hosts["desktop"]["last_data"] is None


def test_hosts_known_only_from_history_have_no_contact_times(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    ingest_batch(
        conn, host="old-pc", batch=[{"file": "P/s.jsonl", "line": _assistant_line("s", "m")}]
    )
    conn.execute("DELETE FROM meta WHERE key LIKE 'host_last_%'")
    conn.commit()

    contact = host_contacts(conn)["old-pc"]

    assert contact.last_seen is None
    assert contact.last_data is None


def test_wrong_token_is_rejected(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "ccmeter.db", api_token="secret-token"))

    for header in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "secret-token"}):
        response = client.post("/ingest", headers=header, json={"host": "pc", "batch": []})
        assert response.status_code == 401
