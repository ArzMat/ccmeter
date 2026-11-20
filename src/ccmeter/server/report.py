"""Plain-text spending report for the terminal."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ccmeter.server.pricing import UsageCounts, cost_usd, is_synthetic_model, load_price_book

MONTH_DAYS = 30


def build_report(
    conn: sqlite3.Connection,
    *,
    prices_path: Path,
    days: int,
    today: date | None = None,
) -> str:
    """Cost of the last `days` calendar days, ending today (UTC, like the stored timestamps).

    Args:
        conn: Open database connection.
        prices_path: Price book to use.
        days: Number of calendar days in the period, today included.
        today: Last day of the period (tests pass a fixed date).

    Returns:
        The report text.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    last_day = today or datetime.now(UTC).date()
    first_day = last_day - timedelta(days=days - 1)
    prices = load_price_book(prices_path)
    rows = conn.execute(
        "SELECT * FROM messages WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (first_day.isoformat(), last_day.isoformat() + "T99:99"),
    ).fetchall()
    by_day_model: dict[tuple[str, str], float] = defaultdict(float)
    by_host: dict[str, float] = defaultdict(float)
    unpriced: set[str] = set()
    total_input = 0
    total_cache_read = 0

    for row in rows:
        usage = _usage_from_row(row)
        model = str(row["model"])
        total_input += usage.input_tokens
        total_cache_read += usage.cache_read_tokens
        cost = cost_usd(model, usage, prices)
        if cost is None:
            if not is_synthetic_model(model):
                unpriced.add(model)
            continue
        by_day_model[(str(row["ts"])[:10], model)] += cost.total_usd
        by_host[str(row["host"])] += cost.total_usd

    total = sum(by_host.values())
    lines = [
        f"ccmeter report: last {days} days ({first_day} to {last_day}, UTC)",
        "",
        "Cost per day and model:",
    ]
    lines.extend(
        f"- {day} {model}: ${value:.6f}" for (day, model), value in sorted(by_day_model.items())
    )
    lines.extend(["", "Cost per host:"])
    lines.extend(f"- {host}: ${value:.6f}" for host, value in sorted(by_host.items()))
    lines.extend(
        [
            "",
            f"Total: ${total:.2f}",
            f"Monthly projection: ${total / days * MONTH_DAYS:.2f}",
            f"Cache hit rate: {_cache_hit_rate(total_cache_read, total_input):.2%}",
            "Models without a price: " + (", ".join(sorted(unpriced)) if unpriced else "none"),
        ]
    )
    return "\n".join(lines)


def _usage_from_row(row: sqlite3.Row) -> UsageCounts:
    return UsageCounts(
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        cache_read_tokens=int(row["cache_read_tokens"]),
        cache_write_5m=int(row["cache_write_5m"]),
        cache_write_1h=int(row["cache_write_1h"]),
        cache_write_total=int(row["cache_write_total"]),
    )


def _cache_hit_rate(cache_read: int, input_tokens: int) -> float:
    denominator = cache_read + input_tokens
    if denominator == 0:
        return 0.0
    return cache_read / denominator
