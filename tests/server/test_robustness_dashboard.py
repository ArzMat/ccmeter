"""Regression tests for the review of the dashboard rework (server side)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ccmeter.server.app import create_app
from ccmeter.server.db import connect_db, ingest_batch, initialize_schema, reprocess_raw_lines

AUTH = {"Authorization": "Bearer secret-token"}
MAIN = "P/s.jsonl"
SUB = "P/s/subagents/agent-a.jsonl"


def _user(prompt: str, *, sidechain: bool = False) -> str:
    return json.dumps(
        {
            "type": "user",
            "sessionId": "s",
            "uuid": f"u-{prompt}-{sidechain}",
            "timestamp": "2026-07-04T09:00:00.000Z",
            "promptId": prompt,
            "isSidechain": sidechain,
            "message": {"content": "x"},
        }
    )


def _assistant(
    message_id: str,
    *,
    model: str = "claude-opus-4-8",
    sidechain: bool = False,
    stamp: str = "2026-07-04T10:00:00.000Z",
) -> str:
    payload: dict[str, object] = {
        "type": "assistant",
        "sessionId": "s",
        "uuid": f"u-{message_id}",
        "timestamp": stamp,
        "message": {"id": message_id, "model": model, "usage": {"input_tokens": 10}},
    }
    if sidechain:
        payload["isSidechain"] = True
        payload["agentId"] = "a"
    return json.dumps(payload)


def _links(conn: sqlite3.Connection) -> dict[str, tuple[str | None, int]]:
    return {
        str(row[0]): (row[1], int(row[2]))
        for row in conn.execute("SELECT message_id, prompt_id, prompt_index FROM messages")
    }


def test_reprocess_never_moves_a_message_to_another_prompt(tmp_path: Path) -> None:
    """The subagent's own prompt line and the main session's newer prompt lead the stored
    lines to a different prompt than ingest chose across batches: reprocess keeps ingest's."""
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    for batch in (
        [{"file": MAIN, "line": _user("p1")}, {"file": MAIN, "line": _assistant("a1")}],
        [
            {"file": SUB, "line": _user("p1", sidechain=True)},
            {"file": SUB, "line": _assistant("s1", sidechain=True)},
        ],
        [{"file": MAIN, "line": _user("p2")}, {"file": MAIN, "line": _assistant("a2")}],
        [{"file": SUB, "line": _assistant("s2", sidechain=True)}],
    ):
        ingest_batch(conn, host="pc", batch=batch)
    before = _links(conn)

    reprocess_raw_lines(conn)

    assert _links(conn) == before


def test_sessions_on_unpriced_models_are_listed_and_flagged(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "ccmeter.db", api_token="secret-token"))
    client.post(
        "/ingest",
        headers=AUTH,
        json={"host": "pc", "batch": [{"file": MAIN, "line": _assistant("m", model="mystery-9")}]},
    )

    sessions = client.get("/api/dashboard/session-list").json()["sessions"]

    assert [(item["session_id"], item["total"], item["unpriced"]) for item in sessions] == [
        ("s", 0, True)
    ]


def test_subagents_report_their_last_message_for_recency(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "ccmeter.db", api_token="secret-token"))
    batch = [
        {"file": SUB, "line": _assistant("x1", sidechain=True, stamp="2026-07-04T08:00:00.000Z")},
        {"file": SUB, "line": _assistant("x2", sidechain=True, stamp="2026-07-04T18:30:00.000Z")},
    ]
    client.post("/ingest", headers=AUTH, json={"host": "pc", "batch": batch})

    detail = client.get("/api/dashboard/session-detail/s").json()

    assert detail["subagents"][0]["last"] == "2026-07-04 18:30"


def test_recency_keeps_the_seconds_for_sessions_and_subagents(tmp_path: Path) -> None:
    """Two subagents (or sessions) ending in the same minute are still ordered by time: the
    cheaper one that ended later comes first under "recent"."""
    client = TestClient(create_app(db_path=tmp_path / "ccmeter.db", api_token="secret-token"))

    def line(session: str, message_id: str, agent: str, stamp: str, tokens: int) -> str:
        payload = json.loads(_assistant(message_id, sidechain=True, stamp=stamp))
        payload["sessionId"] = session
        payload["agentId"] = agent
        payload["message"]["usage"] = {"input_tokens": tokens}
        return json.dumps(payload)

    batch = [
        {
            "file": "P/s/subagents/agent-a.jsonl",
            "line": line("s", "x1", "a", "2026-07-04T18:30:05.000Z", 9000),
        },
        {
            "file": "P/s/subagents/agent-b.jsonl",
            "line": line("s", "x2", "b", "2026-07-04T18:30:40.000Z", 10),
        },
        {
            "file": "P/t/subagents/agent-c.jsonl",
            "line": line("t", "x3", "c", "2026-07-04T18:30:02.000Z", 10),
        },
    ]
    client.post("/ingest", headers=AUTH, json={"host": "pc", "batch": batch})

    subagents = client.get("/api/dashboard/session-detail/s").json()["subagents"]
    sessions = client.get("/api/dashboard/session-list").json()["sessions"]

    by_key = {item["key"]: item for item in subagents}
    assert by_key["a"]["last"] == by_key["b"]["last"] == "2026-07-04 18:30"
    assert by_key["b"]["lastTs"] > by_key["a"]["lastTs"]
    assert [item["session_id"] for item in sessions] == ["s", "t"]
    assert sessions[0]["lastTs"] == "2026-07-04T18:30:40.000Z"


def test_reads_do_not_wait_for_a_writer(tmp_path: Path) -> None:
    """Request handlers no longer touch the schema, so a long ingest does not block them."""
    db_path = tmp_path / "ccmeter.db"
    client = TestClient(create_app(db_path=db_path, api_token="secret-token"))
    client.post(
        "/ingest",
        headers=AUTH,
        json={"host": "pc", "batch": [{"file": MAIN, "line": _assistant("m")}]},
    )
    client.get("/api/dashboard/overview?provider=claude")
    writer = connect_db(db_path)
    writer.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        for path in ("/api/health", "/api/summary", "/api/dashboard/overview?provider=claude"):
            assert client.get(path).status_code == 200, path
        assert time.monotonic() - started < 2
    finally:
        writer.rollback()
        writer.close()


def test_session_list_does_not_wait_for_an_overview_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ccmeter.server.app as app_module

    real_build = app_module.build_overview
    building = threading.Event()

    def slow_build(*args: Any, **kwargs: Any) -> Any:
        building.set()
        time.sleep(3)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(app_module, "build_overview", slow_build)
    client = TestClient(
        create_app(db_path=tmp_path / "ccmeter.db", api_token="secret-token", warm_dashboard=True)
    )
    assert building.wait(5)

    started = time.monotonic()
    response = client.get("/api/dashboard/session-list")

    assert response.status_code == 200
    assert time.monotonic() - started < 1.5
