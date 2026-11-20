"""Server, DB, API and dashboard with normalized Codex records."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from tests.codex_fixtures import CHILD, NEW_CLI, SESSION, Cumulative, Rollout, compress, usage

from ccmeter.client.agent import AgentConfig, OffsetState, ship_codex
from ccmeter.server.app import create_app
from ccmeter.server.dashboard_data import (
    MESSAGE_COLUMNS,
    build_overview,
    build_session_list,
    load_messages,
)
from ccmeter.server.db import connect_db, ingest_batch, initialize_schema, reprocess_raw_lines
from ccmeter.server.pricing import BUNDLED_PRICES_PATH, UsageCounts, cost_usd, load_price_book

PRICES = BUNDLED_PRICES_PATH
FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
AUTH = {"Authorization": "Bearer secret-token"}


def _record(message_id: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "provider": "codex",
        "message_id": message_id,
        "session_id": SESSION,
        "thread_id": SESSION,
        "turn_id": "turn-1",
        "is_sidechain": False,
        "timestamp": "2026-07-20T10:00:00.000Z",
        "model": "gpt-6-astra",
        "project": "demo-project",
        "input_tokens": 1_000,
        "cache_read_tokens": 9_000,
        "cache_write_tokens": 0,
        "output_tokens": 100,
        "reasoning_tokens": 40,
        "service_tier": None,
    }
    record.update(overrides)
    return record


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(db_path=tmp_path / "ccmeter.db", api_token="secret-token", prices_path=PRICES)
    )


def _seed_claude(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    lines = (FIXTURES_DIR / "main_session_extract.jsonl").read_text(encoding="utf-8").splitlines()
    ingest_batch(
        conn,
        host="desktop",
        batch=[{"file": "C--work-demo-app/session.jsonl", "line": line} for line in lines],
    )
    conn.close()


def _post(client: TestClient, records: list[dict[str, Any]], host: str = "desktop") -> Any:
    return client.post("/ingest", headers=AUTH, json={"host": host, "records": records})


def test_ingest_stores_records_and_deduplicates_by_message_id(tmp_path: Path) -> None:
    client = _client(tmp_path)
    records = [
        _record("resp_1"),
        _record("resp_2", turn_id="turn-2", timestamp="2026-07-20T11:00:00.000Z"),
        _record("resp_3", is_sidechain=True, thread_id="child-thread", project="altro"),
    ]

    first = _post(client, records)
    second = _post(client, records)

    assert first.status_code == 200
    assert first.json() == {
        "accepted": 0,
        "parsed": 0,
        "duplicates": 0,
        "errors": 0,
        "records_received": 3,
        "records_inserted": 3,
        "records_duplicates": 0,
        "records_excluded": 0,
    }
    assert second.json()["records_duplicates"] == 3
    conn = connect_db(tmp_path / "ccmeter.db")
    rows = conn.execute("SELECT * FROM messages ORDER BY message_id").fetchall()
    session = conn.execute("SELECT * FROM sessions").fetchone()
    assert [row["provider"] for row in rows] == ["codex"] * 3
    assert [row["prompt_index"] for row in rows] == [1, 2, 1]
    assert rows[0]["reasoning_tokens"] == 40
    assert rows[0]["raw_line_id"] is None
    assert rows[2]["sidechain_key"] == "child-thread"
    assert rows[2]["is_sidechain"] == 1
    assert session["provider"] == "codex"
    assert session["project"] == "demo-project"
    assert session["first_ts"] == "2026-07-20T10:00:00.000Z"
    assert session["last_ts"] == "2026-07-20T11:00:00.000Z"
    assert conn.execute("SELECT COUNT(*) FROM raw_lines").fetchone()[0] == 0


def test_auto_review_records_are_rejected_server_side(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = _post(client, [_record("resp_review", model="codex-auto-review")])

    assert response.json()["records_excluded"] == 1
    assert client.get("/api/summary").json()["by_model"] == []


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "claude"},
        {"input_tokens": -1},
        {"message_id": ""},
        {"timestamp": "ieri"},
    ],
)
def test_invalid_records_are_rejected(tmp_path: Path, override: dict[str, Any]) -> None:
    record = _record("resp_bad")
    record.update(override)

    response = _post(_client(tmp_path), [record])

    assert response.status_code == 422


def _codex_cost(model: str, non_cached: int, cached: int, output: int) -> float:
    prices = load_price_book(PRICES)
    usage_counts = UsageCounts(
        input_tokens=non_cached,
        output_tokens=output,
        cache_read_tokens=cached,
        cache_write_5m=0,
        cache_write_1h=0,
        cache_write_total=0,
    )
    cost = cost_usd(model, usage_counts, prices)
    assert cost is not None
    return cost.total_usd


def test_summary_prices_codex_per_request_and_splits_by_provider(tmp_path: Path) -> None:
    _seed_claude(tmp_path)
    client = _client(tmp_path)
    claude_only = client.get("/api/summary").json()
    _post(
        client,
        [
            # two small requests that together exceed the 272K threshold
            _record("resp_a", input_tokens=50_000, cache_read_tokens=100_000, output_tokens=1_000),
            _record("resp_b", input_tokens=50_000, cache_read_tokens=100_000, output_tokens=1_000),
            _record("resp_long", input_tokens=10_000, cache_read_tokens=300_000, output_tokens=500),
            _record("resp_unknown", model="codex-unknown"),
        ],
    )

    summary = client.get("/api/summary").json()
    codex_only = client.get("/api/summary?provider=codex").json()

    expected_astra = 2 * _codex_cost("gpt-6-astra", 50_000, 100_000, 1_000) + _codex_cost(
        "gpt-6-astra", 10_000, 300_000, 500
    )
    astra = next(item for item in summary["by_model"] if item["model"] == "gpt-6-astra")
    assert astra["cost_usd"] == pytest.approx(expected_astra)
    assert astra["input"] == 110_000
    unknown = next(item for item in summary["by_model"] if item["model"] == "codex-unknown")
    assert unknown["cost_usd"] is None
    assert "codex-unknown" in summary["unpriced_models"]
    providers = {item["provider"]: item for item in summary["by_provider"]}
    assert set(providers) == {"claude", "codex"}
    assert providers["claude"]["cost_usd"] == pytest.approx(claude_only["total_cost_usd"])
    assert providers["codex"]["cost_usd"] == pytest.approx(expected_astra)
    assert providers["codex"]["reasoning"] == 160
    assert summary["total_cost_usd"] == pytest.approx(
        claude_only["total_cost_usd"] + expected_astra
    )
    assert {item["model"] for item in codex_only["by_model"]} == {
        "gpt-6-astra",
        "codex-unknown",
    }
    assert [item["provider"] for item in codex_only["by_provider"]] == ["codex"]


def test_daily_sessions_and_subagents_include_codex(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _post(
        client,
        [
            _record("resp_main"),
            _record("resp_sub", is_sidechain=True, thread_id="child-thread", model="gpt-5.6-sol"),
        ],
        host="laptop",
    )

    daily = client.get("/api/spend/daily").json()
    sessions = client.get("/api/sessions").json()["sessions"]
    detail = client.get(f"/api/sessions/{SESSION}").json()
    subagents = client.get("/api/subagents").json()
    health = client.get("/api/health").json()

    assert set(daily["days"][0]["by_model"]) == {"gpt-6-astra", "gpt-5.6-sol"}
    assert sessions[0]["provider"] == "codex"
    assert sessions[0]["project"] == "demo-project"
    assert sessions[0]["subagent_messages"] == 1
    assert detail["prompts"][0]["messages"][0]["provider"] == "codex"
    assert subagents["subagents"][0]["sidechain_key"] == "child-thread"
    assert health["hosts_seen"] == ["laptop"]
    assert health["last_ingest_ts"] is not None


def test_reprocess_keeps_codex_records(tmp_path: Path) -> None:
    _seed_claude(tmp_path)
    _post(_client(tmp_path), [_record("resp_keep")])
    conn = connect_db(tmp_path / "ccmeter.db")
    query = "SELECT provider, COUNT(*) FROM messages GROUP BY provider ORDER BY provider"
    before = [tuple(row) for row in conn.execute(query)]

    result = reprocess_raw_lines(conn)

    after = [tuple(row) for row in conn.execute(query)]
    assert after == before
    assert ("codex", 1) in after
    assert result.messages == sum(count for _provider, count in after)
    session_provider = conn.execute(
        "SELECT provider FROM sessions WHERE session_id = ?", (SESSION,)
    ).fetchone()[0]
    assert session_provider == "codex"


def test_dashboard_shows_codex_models_and_raw_tokens(tmp_path: Path) -> None:
    _seed_claude(tmp_path)
    _post(_client(tmp_path), [_record("resp_dash", timestamp="2026-07-20T10:00:00.000Z")])
    conn = connect_db(tmp_path / "ccmeter.db")

    book = load_price_book(PRICES)
    data = build_overview(conn, book)
    messages = load_messages(
        conn.execute(f"SELECT {MESSAGE_COLUMNS} FROM messages ORDER BY ts"), book
    )
    sessions = build_session_list(messages, list(conn.execute("SELECT * FROM sessions")))

    models = {item["model"]: item for item in data["models"]}
    assert models["gpt-6-astra"]["cost"] > 0
    day = next(item for item in data["days"] if item["date"] == "2026-07-20")
    assert day["byModel"]["gpt-6-astra"] == models["gpt-6-astra"]["cost"]
    assert day["byModelTok"]["gpt-6-astra"] == [1_000, 100, 9_000, 0]
    assert day["rawTok"] == {"input": 1_000, "output": 100, "cache_read": 9_000, "cache_write": 0}
    raw_models = {item["model"]: item for item in data["rawUsage"]["models"]}
    assert raw_models["gpt-6-astra"]["messages"] == 1
    session = next(item for item in sessions if item["session_id"] == SESSION)
    assert session["project"] == "demo-project"
    assert session["promptCount"] == 1
    assert session["provider"] == "codex"
    assert models["gpt-6-astra"]["provider"] == "codex"
    codex_only = build_overview(conn, book, provider="codex")
    assert {item["model"] for item in codex_only["models"]} == {"gpt-6-astra"}
    assert codex_only["providers"] == ["claude", "codex"]


def _old_schema_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE raw_lines (
          id INTEGER PRIMARY KEY, host TEXT NOT NULL, file TEXT NOT NULL,
          byte_offset INTEGER, content TEXT NOT NULL, received_ts TEXT NOT NULL,
          parsed INTEGER NOT NULL DEFAULT 0, error TEXT, content_hash TEXT NOT NULL
        );
        CREATE TABLE sessions (
          session_id TEXT PRIMARY KEY, host TEXT NOT NULL, project TEXT,
          first_ts TEXT, last_ts TEXT
        );
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY, message_id TEXT NOT NULL,
          session_id TEXT NOT NULL REFERENCES sessions(session_id), host TEXT NOT NULL,
          ts TEXT NOT NULL, model TEXT NOT NULL, prompt_id TEXT, prompt_index INTEGER,
          is_sidechain INTEGER NOT NULL DEFAULT 0, sidechain_key TEXT, parent_uuid TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
          cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_5m INTEGER NOT NULL DEFAULT 0,
          cache_write_1h INTEGER NOT NULL DEFAULT 0, cache_write_total INTEGER NOT NULL DEFAULT 0,
          raw_line_id INTEGER REFERENCES raw_lines(id)
        );
        INSERT INTO sessions VALUES ('old-s', 'desktop', 'p', '2026-07-01', '2026-07-01');
        INSERT INTO messages(message_id, session_id, host, ts, model, output_tokens)
        VALUES ('msg_old', 'old-s', 'desktop', '2026-07-01T10:00:00.000Z', 'claude-opus-4-8', 10);
        """
    )
    conn.commit()
    conn.close()


def test_existing_database_is_migrated_in_place(tmp_path: Path) -> None:
    _old_schema_db(tmp_path / "ccmeter.db")
    client = _client(tmp_path)

    response = _post(client, [_record("resp_new")])

    assert response.json()["records_inserted"] == 1
    conn = connect_db(tmp_path / "ccmeter.db")
    rows = conn.execute("SELECT message_id, provider FROM messages ORDER BY message_id").fetchall()
    assert [tuple(row) for row in rows] == [("msg_old", "claude"), ("resp_new", "codex")]
    session = conn.execute("SELECT provider FROM sessions WHERE session_id = 'old-s'").fetchone()
    assert session[0] == "claude"
    indexes = {row["name"] for row in conn.execute("PRAGMA index_list(messages)")}
    assert "idx_msg_provider" in indexes


class _TestClientAdapter:
    """Connects the agent to the test server as if it were httpx."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> object:
        assert url.endswith("/ingest")
        return self.client.post("/ingest", headers=headers, json=json)


def test_agent_to_server_end_to_end(tmp_path: Path) -> None:
    cumulative = Cumulative()
    rollout = Rollout().meta(cli=NEW_CLI).turn("gpt-5.6-sol")
    for index in range(3):
        used = usage(10_000 * (index + 1), 5_000, 100, 10)
        rollout.record(f"resp_e2e_{index}", used)
        rollout.token_count(*cumulative.step(used))
    rollout.write(tmp_path / "codex" / "2026" / "rollout-e2e.jsonl")
    cfg = AgentConfig(
        server_url="http://testserver",
        api_token="secret-token",
        host_label="desktop",
        claude_projects_dir=tmp_path / "claude",
        codex_dirs=(tmp_path / "codex",),
    )
    client = _client(tmp_path)
    state = OffsetState(offsets={})

    sent, failed = ship_codex(cfg, state, _TestClientAdapter(client))

    assert (sent, failed) == (3, 0)
    summary = client.get("/api/summary").json()
    model = cast(dict[str, Any], summary["by_model"][0])
    assert model["model"] == "gpt-5.6-sol"
    assert model["input"] == (10_000 + 20_000 + 30_000) - 3 * 5_000
    assert model["cache_read"] == 15_000
    assert model["output"] == 300


def _agent_cfg(codex_dir: Path) -> AgentConfig:
    return AgentConfig(
        server_url="http://testserver",
        api_token="secret-token",
        host_label="desktop",
        claude_projects_dir=codex_dir.parent / "claude",
        codex_dirs=(codex_dir,),
    )


def test_rollout_compressed_after_a_partial_read_is_counted_once(tmp_path: Path) -> None:
    cumulative = Cumulative()
    format_a = Rollout().meta().turn("gpt-5.6-sol")
    for index in range(4):
        format_a.token_count(*cumulative.step(usage(1_000 * (index + 1), 500, 10 + index, 0)))
    format_b = Rollout(CHILD).meta(cli=NEW_CLI).turn("gpt-6-astra")
    for index in range(4):
        format_b.record(f"resp_zst_{index}", usage(2_000 * (index + 1), 1_000, 20 + index, 0))
    rollouts = {"rollout-a.jsonl": format_a, "rollout-b.jsonl": format_b}
    live = tmp_path / "live" / "codex"
    live.mkdir(parents=True)
    for name, rollout in rollouts.items():
        payload = rollout.text_payload()
        # the agent reads the first half while Codex is still writing the rollout
        (live / name).write_bytes(payload[: payload.index(b"\n", len(payload) // 2) + 1])
    reference_dir = tmp_path / "reference"
    for name, rollout in rollouts.items():
        rollout.write(reference_dir / "codex" / name)
    client = _client(tmp_path / "live")
    reference = _client(reference_dir)
    state = OffsetState(offsets={})

    first = ship_codex(_agent_cfg(live), state, _TestClientAdapter(client))
    # Codex finishes both sessions and later compresses the idle rollouts
    for name, rollout in rollouts.items():
        compress(rollout.write(live / name))
    second = ship_codex(_agent_cfg(live), state, _TestClientAdapter(client))
    ship_codex(
        _agent_cfg(reference_dir / "codex"), OffsetState(offsets={}), _TestClientAdapter(reference)
    )

    assert first[1] == 0
    assert 0 < first[0] < 8
    # every record of the compressed copies is sent again and the server keeps one of each
    assert second == (8, 0)
    summary = client.get("/api/summary").json()
    assert summary["by_model"] == reference.get("/api/summary").json()["by_model"]
    assert summary["by_provider"] == reference.get("/api/summary").json()["by_provider"]
    conn = sqlite3.connect(tmp_path / "live" / "ccmeter.db")
    try:
        count = conn.execute("SELECT COUNT(*) FROM messages WHERE provider = 'codex'").fetchone()
    finally:
        conn.close()
    assert count[0] == 8
    assert sorted(Path(key).name for key in state.codex) == [
        "rollout-a.jsonl.zst",
        "rollout-b.jsonl.zst",
    ]
