from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ccmeter.server.app import STATIC_DIR, create_app
from ccmeter.server.cache import CachedAsset, StaleWhileRevalidate
from ccmeter.server.dashboard_data import (
    MESSAGE_COLUMNS,
    build_overview,
    build_session_detail,
    build_session_list,
    load_messages,
)
from ccmeter.server.db import connect_db, ingest_batch, initialize_schema
from ccmeter.server.pricing import BUNDLED_PRICES_PATH, load_price_book

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
MAIN_SESSION = "60ce41f3-dfbc-472e-8297-629a073f21dd"


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8").splitlines()


def _seed_db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    batch = [
        {"file": "C--work-demo-app/session.jsonl", "line": line}
        for line in _fixture_lines("main_session_extract.jsonl")
    ]
    batch.extend(
        {
            "file": "C--work-demo-app/session/subagents/agent-a1097c2b70a1dff39.jsonl",
            "line": line,
        }
        for line in _fixture_lines("subagent_extract.jsonl")
    )
    ingest_batch(conn, host="desktop", batch=batch)
    return conn


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            db_path=tmp_path / "ccmeter.db",
            api_token="secret-token",
            prices_path=BUNDLED_PRICES_PATH,
        )
    )


def _overview(conn: sqlite3.Connection, provider: str | None = None) -> dict[str, Any]:
    return build_overview(conn, load_price_book(BUNDLED_PRICES_PATH), provider=provider)


def _messages(conn: sqlite3.Connection) -> list[Any]:
    rows = conn.execute(f"SELECT {MESSAGE_COLUMNS} FROM messages ORDER BY ts").fetchall()
    return load_messages(rows, load_price_book(BUNDLED_PRICES_PATH))


def test_overview_matches_dashboard_contract(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)

    data = _overview(conn)

    assert data["days"]
    day = data["days"][0]
    assert set(day) == {"date", "byModel", "byHost", "byModelTok", "rawTok", "total"}
    assert data["models"][0]["cost"] > 0
    assert data["models"][0]["provider"] == "claude"
    assert data["hosts"][0]["host"] == "desktop"
    assert data["providers"] == ["claude"]
    assert data["health"]["hosts_seen"] == ["desktop"]
    assert data["unpricedModels"] == []
    assert "sessions" not in data


def test_session_list_and_detail_split_the_old_session_payload(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    messages = _messages(conn)
    session_rows = list(conn.execute("SELECT * FROM sessions"))

    sessions = build_session_list(messages, session_rows)
    main = next(item for item in sessions if item["session_id"] == MAIN_SESSION)
    detail = build_session_detail(
        [row for row in messages if row.session_id == MAIN_SESSION],
        next(row for row in session_rows if row["session_id"] == MAIN_SESSION),
    )

    assert "prompts" not in main
    assert main["promptCount"] >= 1
    assert main["models"]
    assert detail["session"] == main
    assert detail["prompts"][0]["text"].startswith("Prompt")
    assert detail["prompts"][0]["models"]
    sub_session = next(row.session_id for row in messages if row.is_sidechain == 1)
    sub_detail = build_session_detail(
        [row for row in messages if row.session_id == sub_session], None
    )
    assert any(item["key"] == "a1097c2b70a1dff39" for item in sub_detail["subagents"])


def test_dashboard_global_models_exclude_synthetic_usage(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    ingest_batch(
        conn,
        host="desktop",
        batch=[
            {"file": "C--work-demo-app/session.jsonl", "line": line}
            for line in _fixture_lines("synthetic_extract.jsonl")
        ],
    )

    data = _overview(conn)

    assert all(model["model"] != "<synthetic>" for model in data["models"])


def test_dashboard_raw_usage_counts_physical_rows_separately_from_models(
    tmp_path: Path,
) -> None:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    line = (
        '{"type":"assistant","sessionId":"s","uuid":"u-1","timestamp":"2026-07-04T10:00:00.000Z",'
        '"message":{"id":"msg-1","model":"claude-opus-4-8","usage":{'
        '"input_tokens":100,"output_tokens":10,"cache_read_input_tokens":7,'
        '"cache_creation":{"ephemeral_5m_input_tokens":3,"ephemeral_1h_input_tokens":5}}}}'
    )
    ingest_batch(
        conn,
        host="desktop",
        batch=[
            {"file": "Project/session.jsonl", "line": line, "offset": "0"},
            {"file": "Project/session.jsonl", "line": line, "offset": "200"},
        ],
    )

    data = _overview(conn)

    assert data["models"][0]["input"] == 100
    assert data["models"][0]["output"] == 10
    assert data["rawUsage"]["total"]["input"] == 200
    assert data["rawUsage"]["total"]["output"] == 20
    assert data["rawUsage"]["total"]["cache_read"] == 14
    assert data["rawUsage"]["total"]["cache_write"] == 16
    assert data["rawUsage"]["models"][0]["model"] == "claude-opus-4-8"
    assert data["days"][0]["rawTok"] == {
        "input": 200,
        "output": 20,
        "cache_read": 14,
        "cache_write": 16,
    }


def test_prompt_models_exclude_subagent_costs(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    conn.execute(
        """
        INSERT INTO sessions(session_id, host, project, first_ts, last_ts)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "session-with-subagent",
            "desktop",
            "project",
            "2026-07-04T10:00:00.000Z",
            "2026-07-04T10:01:00.000Z",
        ),
    )
    _insert_dashboard_message(
        conn,
        message_id="main",
        session_id="session-with-subagent",
        model="claude-fable-5",
        prompt_index=1,
        is_sidechain=0,
        sidechain_key=None,
        output_tokens=1_000_000,
    )
    _insert_dashboard_message(
        conn,
        message_id="subagent",
        session_id="session-with-subagent",
        model="claude-opus-4-8",
        prompt_index=1,
        is_sidechain=1,
        sidechain_key="agent-a",
        output_tokens=1_000_000,
    )

    detail = build_session_detail(_messages(conn), None)

    prompt = detail["prompts"][0]
    prompt_model_cost = sum(float(item["cost"]) for item in prompt["models"])
    prompt_sub_model_cost = sum(float(item["cost"]) for item in prompt["subModels"])
    assert prompt["sub"]["cost"] > 0
    assert prompt_model_cost == pytest.approx(prompt["main"]["cost"])
    assert prompt_sub_model_cost == pytest.approx(prompt["sub"]["cost"])


def _insert_dashboard_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    session_id: str,
    model: str,
    prompt_index: int,
    is_sidechain: int,
    sidechain_key: str | None,
    output_tokens: int,
) -> None:
    conn.execute(
        """
        INSERT INTO messages(
          message_id, session_id, host, ts, model, prompt_id, prompt_index,
          is_sidechain, sidechain_key, parent_uuid, input_tokens, output_tokens,
          cache_read_tokens, cache_write_5m, cache_write_1h, cache_write_total,
          raw_line_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            session_id,
            "desktop",
            "2026-07-04T10:00:00.000Z",
            model,
            "prompt-1",
            prompt_index,
            is_sidechain,
            sidechain_key,
            None,
            0,
            output_tokens,
            0,
            0,
            0,
            0,
            None,
        ),
    )


def test_dashboard_routes_serve_the_page_and_its_data(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    conn.close()
    client = _client(tmp_path)

    page = client.get("/dashboard/")
    overview = client.get("/api/dashboard/overview")
    sessions = client.get("/api/dashboard/session-list?from=2025-04-28&to=2025-05-27")
    detail = client.get(f"/api/dashboard/session-detail/{MAIN_SESSION}")
    missing = client.get("/api/dashboard/session-detail/nope")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert overview.status_code == 200
    assert overview.json()["days"]
    assert sessions.status_code == 200
    assert all("prompts" not in item for item in sessions.json()["sessions"])
    assert detail.status_code == 200
    assert detail.json()["session"]["session_id"] == MAIN_SESSION
    assert missing.status_code == 404


def test_dashboard_pages_forbid_third_party_content(tmp_path: Path) -> None:
    client = _client(tmp_path)

    page = client.get("/dashboard/")

    script = client.get("/dashboard/app.js")

    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    # static files are revalidated, so an upgrade never leaves an old script in a browser
    assert script.headers["cache-control"] == "no-cache"
    assert "etag" in script.headers


def test_root_and_bare_dashboard_redirect_to_the_page(tmp_path: Path) -> None:
    client = _client(tmp_path)

    for path in ("/", "/dashboard"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code in {307, 308}
        assert response.headers["location"] == "/dashboard/"


def test_dashboard_is_shipped_inside_the_package() -> None:
    assert (
        Path(__import__("ccmeter.server.app").server.app.__file__).with_name("static") == STATIC_DIR
    )
    assert (STATIC_DIR / "index.html").exists()


def test_overview_is_gzipped_once_and_revalidated_with_etag(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    conn.close()
    client = _client(tmp_path)

    plain = client.get("/api/dashboard/overview", headers={"Accept-Encoding": "identity"})
    zipped = client.get("/api/dashboard/overview", headers={"Accept-Encoding": "gzip"})
    etag = plain.headers["etag"]
    revalidated = client.get("/api/dashboard/overview", headers={"If-None-Match": etag})

    assert "content-encoding" not in plain.headers
    assert zipped.headers["content-encoding"] == "gzip"
    assert zipped.json() == plain.json()
    assert zipped.headers["etag"] == etag
    assert plain.headers["cache-control"] == "no-cache"
    assert revalidated.status_code == 304
    assert revalidated.content == b""


def test_session_list_is_gzipped_when_accepted(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    conn.close()
    client = _client(tmp_path)

    plain = client.get("/api/dashboard/session-list", headers={"Accept-Encoding": "identity"})
    zipped = client.get("/api/dashboard/session-list", headers={"Accept-Encoding": "gzip"})

    assert plain.status_code == 200
    assert zipped.headers["content-encoding"] == "gzip"
    assert zipped.json() == plain.json()
    assert plain.json()["sessions"]


def test_overview_rejects_malformed_provider_filters(tmp_path: Path) -> None:
    client = _client(tmp_path)

    # a well-formed name without data is a 404: tests/server/test_robustness.py
    assert client.get("/api/dashboard/overview?provider=../x").status_code == 400


class _Renders:
    """Fake renderer: counts builds, can block one build and can fail."""

    def __init__(self) -> None:
        self.count = 0
        self.gate = threading.Event()
        self.gate.set()
        self.fail = False

    def __call__(self) -> CachedAsset:
        number = self.count
        self.count += 1
        assert self.gate.wait(5)
        if self.fail:
            raise RuntimeError("boom")
        return CachedAsset.of(f"v{number}".encode())


def _eventually(predicate: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_stale_dashboard_data_is_served_at_once_and_rebuilt_in_background() -> None:
    renders = _Renders()
    now = [0.0]
    cache = StaleWhileRevalidate(renders, ttl_seconds=60, clock=lambda: now[0])

    assert cache.get().body == b"v0"
    now[0] = 30
    assert cache.get().body == b"v0"
    assert renders.count == 1

    renders.gate.clear()
    now[0] = 61
    assert cache.get().body == b"v0"
    assert cache.get().body == b"v0"
    assert renders.count == 2
    renders.gate.set()

    assert _eventually(lambda: cache.get().body == b"v1")
    assert renders.count == 2


def test_failed_background_refresh_keeps_previous_copy_and_retries() -> None:
    renders = _Renders()
    now = [0.0]
    cache = StaleWhileRevalidate(renders, ttl_seconds=60, clock=lambda: now[0])
    assert cache.get().body == b"v0"

    renders.fail = True
    now[0] = 61
    # every stale read returns the old copy at once and failed rebuilds are retried
    assert _eventually(lambda: cache.get().body == b"v0" and renders.count >= 3)

    renders.fail = False
    assert _eventually(lambda: cache.get().body != b"v0")


def test_first_request_waits_for_the_startup_build_instead_of_building_twice() -> None:
    renders = _Renders()
    renders.gate.clear()
    cache = StaleWhileRevalidate(renders, ttl_seconds=60)
    cache.refresh_in_background()
    assert _eventually(lambda: renders.count == 1)

    result: list[bytes] = []
    reader = threading.Thread(target=lambda: result.append(cache.get().body))
    reader.start()
    time.sleep(0.05)
    renders.gate.set()
    reader.join(5)

    assert result == [b"v0"]
    assert renders.count == 1
