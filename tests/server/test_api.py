from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccmeter.server.app import create_app, create_default_app
from ccmeter.server.db import connect_db, ingest_batch, initialize_schema
from ccmeter.server.pricing import BUNDLED_PRICES_PATH

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8").splitlines()


def _seed_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "ccmeter.db"
    conn = connect_db(db_path)
    initialize_schema(conn)
    main_batch = [
        {"file": "C--work-demo-app/session.jsonl", "line": line}
        for line in _fixture_lines("main_session_extract.jsonl")
    ]
    subagent_batch = [
        {"file": "C--work-voice-lab/session/subagents/agent-a1097c2b70a1dff39.jsonl", "line": line}
        for line in _fixture_lines("subagent_extract.jsonl")
    ]
    synthetic_batch = [
        {"file": "C--work-demo-app/session.jsonl", "line": line}
        for line in _fixture_lines("synthetic_extract.jsonl")
    ]
    ingest_batch(conn, host="desktop", batch=main_batch + subagent_batch + synthetic_batch)
    return conn


def _client(tmp_path: Path) -> TestClient:
    _seed_db(tmp_path).close()
    return TestClient(
        create_app(
            db_path=tmp_path / "ccmeter.db",
            api_token="secret-token",
            prices_path=BUNDLED_PRICES_PATH,
        )
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/health",
        "/api/summary",
        "/api/spend/daily",
        "/api/sessions",
        "/api/sessions/missing-session",
        "/api/subagents",
    ],
)
def test_read_endpoints_initialize_empty_database(tmp_path: Path, path: str) -> None:
    client = TestClient(
        create_app(
            db_path=tmp_path / "empty.db",
            api_token="secret-token",
            prices_path=BUNDLED_PRICES_PATH,
        )
    )

    response = client.get(path)

    assert response.status_code == 200


def test_default_app_requires_explicit_non_placeholder_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CCMETER_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="CCMETER_API_TOKEN"):
        create_default_app()

    monkeypatch.setenv("CCMETER_API_TOKEN", "change-me")

    with pytest.raises(RuntimeError, match="CCMETER_API_TOKEN"):
        create_default_app()


def test_summary_endpoint_returns_model_host_costs_and_excludes_synthetic_unpriced(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    response = client.get("/api/summary")

    assert response.status_code == 200
    body = response.json()
    by_model = {row["model"]: row for row in body["by_model"]}
    assert by_model["claude-fable-5"]["input"] == 12_864
    assert by_model["claude-fable-5"]["cost_usd"] == pytest.approx(0.603628)
    assert by_model["<synthetic>"]["cost_usd"] is None
    assert body["by_host"][0]["host"] == "desktop"
    assert body["total_cost_usd"] > 0
    assert "<synthetic>" not in body["unpriced_models"]


def test_summary_reloads_prices_without_restarting_app(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    conn.close()
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(
        json.dumps(
            {
                "models": {
                    "fable-5": {
                        "match": ["fable-5"],
                        "input": 0,
                        "output": 0,
                        "cache_read": 0,
                        "cache_write_5m": 0,
                        "cache_write_1h": 0,
                        "verified": "test",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            db_path=tmp_path / "ccmeter.db",
            api_token="secret-token",
            prices_path=prices_path,
        )
    )
    first = client.get("/api/summary").json()

    prices_path.write_text(
        json.dumps(
            {
                "models": {
                    "fable-5": {
                        "match": ["fable-5"],
                        "input": 10,
                        "output": 50,
                        "cache_read": 1,
                        "cache_write_5m": 12.5,
                        "cache_write_1h": 20,
                        "verified": "test",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    second = client.get("/api/summary").json()

    assert first["total_cost_usd"] == 0
    assert second["total_cost_usd"] > 0


def test_spend_daily_endpoint_returns_projection_method(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/spend/daily")

    assert response.status_code == 200
    body = response.json()
    assert body["projection_method"] == "period_average_daily_total_usd_x_30"
    assert body["projection_month_usd"] > 0
    assert body["days"][0]["date"] == "2025-05-08"
    assert "claude-fable-5" in body["days"][0]["by_model"]


def test_sessions_endpoint_returns_session_rollups(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/sessions")

    assert response.status_code == 200
    sessions = response.json()["sessions"]
    by_id = {session["session_id"]: session for session in sessions}
    session = by_id["60ce41f3-dfbc-472e-8297-629a073f21dd"]
    assert session["host"] == "desktop"
    assert session["messages"] >= 1
    assert session["prompts"] >= 1
    assert session["total_cost_usd"] > 0


def test_session_detail_endpoint_groups_messages_by_prompt(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/sessions/60ce41f3-dfbc-472e-8297-629a073f21dd")

    assert response.status_code == 200
    body = response.json()
    assert body["session"]["session_id"] == "60ce41f3-dfbc-472e-8297-629a073f21dd"
    assert body["prompts"][0]["prompt_index"] >= 0
    assert body["prompts"][0]["messages"][0]["message_id"]
    assert "main" in body["prompts"][0]
    assert "subagents" in body["prompts"][0]


def test_subagents_endpoint_returns_sidechain_rollups(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/subagents")

    assert response.status_code == 200
    body = response.json()
    assert body["subagents"][0]["sidechain_key"] == "a1097c2b70a1dff39"
    assert body["subagents"][0]["messages"] == 1
    assert body["grand_total_usd"] > 0
