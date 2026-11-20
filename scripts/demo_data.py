"""Fill a database with invented usage, to try the dashboard without any real data.

    uv run python scripts/demo_data.py --db demo.db
    CCMETER_API_TOKEN=demo uv run ccmeter server run --db demo.db

Three machines, a handful of projects, Claude Code sessions with subagents and a few Codex
sessions over the last 45 days. Deterministic: the same seed gives the same data.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ccmeter.server.db import (
    connect_db,
    ingest_batch,
    ingest_usage_records,
    initialize_schema,
    touch_host,
)
from ccmeter.shared.models import ProviderUsage

HOSTS = ("workstation", "laptop", "build-box")
PROJECTS = ("web-shop", "data-pipeline", "docs-site", "cli-tool", "mobile-app")
CLAUDE_MODELS = (
    ("claude-opus-5-5", 0.30),
    ("claude-sonnet-5-5", 0.45),
    ("claude-haiku-4-5-20251001", 0.25),
)
CODEX_MODEL = "gpt-5.5"


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill a database with invented ccmeter usage.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    if args.db.exists():
        raise SystemExit(f"{args.db} exists: choose a new file")
    rng = random.Random(args.seed)
    conn = connect_db(args.db)
    initialize_schema(conn)
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sessions = written = 0
    for day_offset in range(args.days, -1, -1):
        day = today - timedelta(days=day_offset)
        weekday = day.weekday()
        for _ in range(rng.randint(0, 2) if weekday >= 5 else rng.randint(2, 6)):
            sessions += 1
            host = rng.choices(HOSTS, weights=(5, 3, 1))[0]
            if rng.random() < 0.15:
                written += _codex_session(conn, rng, host, day, now)
            else:
                written += _claude_session(conn, rng, host, day, sessions, now)
    for host in HOSTS:
        touch_host(conn, host, has_data=True)
    conn.commit()
    conn.close()
    print(f"wrote {written} invented sessions to {args.db}")
    return 0


def _claude_session(
    conn: sqlite3.Connection,
    rng: random.Random,
    host: str,
    day: datetime,
    number: int,
    now: datetime,
) -> int:
    project = rng.choice(PROJECTS)
    session = str(uuid.UUID(int=rng.getrandbits(128), version=4))
    file = f"{project}/{session}.jsonl"
    start = day + timedelta(hours=rng.randint(8, 20), minutes=rng.randint(0, 59))
    lines: list[dict[str, str]] = []
    for prompt in range(1, rng.randint(2, 14)):
        moment = start + timedelta(minutes=prompt * rng.randint(2, 9))
        lines.append(_line(file, _user(session, f"{session}-p{prompt}", moment)))
        model = rng.choices([m for m, _ in CLAUDE_MODELS], weights=[w for _, w in CLAUDE_MODELS])[0]
        for step in range(rng.randint(1, 6)):
            stamp = moment + timedelta(seconds=20 * (step + 1))
            lines.append(
                _line(file, _assistant(session, f"{session}-{prompt}-{step}", model, stamp, rng))
            )
        if rng.random() < 0.25:
            agent = f"a{number:04d}{prompt:02d}"
            agent_file = f"{project}/{session}/subagents/agent-{agent}.jsonl"
            for step in range(rng.randint(2, 8)):
                stamp = moment + timedelta(seconds=30 * (step + 1))
                line = _assistant(
                    session,
                    f"{session}-sub-{prompt}-{step}",
                    "claude-haiku-4-5-20251001",
                    stamp,
                    rng,
                )
                line["isSidechain"] = True
                line["agentId"] = agent
                lines.append(_line(agent_file, line))
    # today's sessions stop at the current time: nothing is dated in the future
    lines = [item for item in lines if json.loads(item["line"])["timestamp"] <= _stamp(now)]
    if not lines:
        return 0
    ingest_batch(conn, host=host, batch=lines)
    return 1


def _codex_session(
    conn: sqlite3.Connection, rng: random.Random, host: str, day: datetime, now: datetime
) -> int:
    project = rng.choice(PROJECTS)
    session = str(uuid.UUID(int=rng.getrandbits(128), version=4))
    start = day + timedelta(hours=rng.randint(8, 20))
    records: list[ProviderUsage] = []
    for turn in range(rng.randint(2, 10)):
        stamp = start + timedelta(minutes=turn * 3)
        if stamp > now:
            break
        cached = rng.randint(20_000, 120_000)
        records.append(
            ProviderUsage(
                provider="codex",
                message_id=f"{session}-{turn}",
                session_id=session,
                thread_id=session,
                turn_id=f"{session}-turn-{turn}",
                is_sidechain=False,
                timestamp=_stamp(stamp),
                model=CODEX_MODEL,
                project=project,
                input_tokens=rng.randint(500, 8_000),
                cache_read_tokens=cached,
                cache_write_tokens=0,
                output_tokens=rng.randint(200, 4_000),
                reasoning_tokens=rng.randint(0, 1_500),
                service_tier=None,
            )
        )
    if not records:
        return 0
    ingest_usage_records(conn, host=host, records=records)
    return 1


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _line(file: str, payload: dict[str, object]) -> dict[str, str]:
    return {"file": file, "line": json.dumps(payload)}


def _user(session: str, prompt_id: str, moment: datetime) -> dict[str, object]:
    return {
        "type": "user",
        "sessionId": session,
        "uuid": f"user-{prompt_id}",
        "promptId": prompt_id,
        "timestamp": _stamp(moment),
        "message": {"role": "user", "content": ""},
    }


def _assistant(
    session: str, message_id: str, model: str, moment: datetime, rng: random.Random
) -> dict[str, object]:
    return {
        "type": "assistant",
        "sessionId": session,
        "uuid": f"uuid-{message_id}",
        "timestamp": _stamp(moment),
        "message": {
            "id": f"msg-{message_id}",
            "model": model,
            "usage": {
                "input_tokens": rng.randint(5, 400),
                "output_tokens": rng.randint(50, 3_000),
                "cache_read_input_tokens": rng.randint(5_000, 150_000),
                "cache_creation": {
                    "ephemeral_5m_input_tokens": rng.randint(0, 8_000),
                    "ephemeral_1h_input_tokens": rng.randint(0, 2_000),
                },
            },
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
