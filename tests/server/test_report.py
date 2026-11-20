from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from ccmeter.server.db import connect_db, ingest_batch, initialize_schema
from ccmeter.server.pricing import BUNDLED_PRICES_PATH
from ccmeter.server.report import build_report

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8").splitlines()


def _seed_db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)
    batch = [
        {"file": "C--work-demo-app/session.jsonl", "line": line}
        for line in _fixture_lines("main_session_extract.jsonl")
    ]
    ingest_batch(conn, host="desktop", batch=batch)
    return conn


def test_build_report_contains_costs_hosts_projection_and_cache_hit_rate(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    last_day = _last_usage_day(conn)

    report = build_report(conn, prices_path=BUNDLED_PRICES_PATH, days=7, today=last_day)

    assert report.startswith("ccmeter report: last 7 days (")
    assert "Cost per day and model" in report
    assert "Cost per host" in report
    assert "Monthly projection" in report
    assert "Cache hit rate" in report
    assert "claude-fable-5" in report
    assert "desktop" in report


def test_build_report_only_counts_the_requested_days(tmp_path: Path) -> None:
    """The period used to be ignored: the report summed the whole history."""
    conn = _seed_db(tmp_path)
    last_day = _last_usage_day(conn)
    after = date.fromordinal(last_day.toordinal() + 30)

    inside = build_report(conn, prices_path=BUNDLED_PRICES_PATH, days=7, today=last_day)
    outside = build_report(conn, prices_path=BUNDLED_PRICES_PATH, days=7, today=after)

    assert "- desktop: $" in inside
    assert "- desktop: $" not in outside
    assert "Total: $0.00" in outside


def _last_usage_day(conn: sqlite3.Connection) -> date:
    return date.fromisoformat(str(conn.execute("SELECT MAX(ts) FROM messages").fetchone()[0])[:10])
