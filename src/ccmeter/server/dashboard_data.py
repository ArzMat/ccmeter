"""Payloads of the dashboard API: overview, session list and session detail.

Every cost is computed once per message (`_Msg`) and the aggregates sum those values in
message order, so each view adds up exactly like the others.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ccmeter.server.db import (
    CLAUDE_PROVIDER,
    host_contacts,
    hosts_seen,
    last_ingest_ts,
    update_raw_usage_daily,
)
from ccmeter.server.pricing import PriceBook, UsageCounts, cost_usd, is_synthetic_model

MESSAGE_FIELDS = (
    "ts",
    "model",
    "host",
    "session_id",
    "prompt_index",
    "is_sidechain",
    "sidechain_key",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m",
    "cache_write_1h",
    "cache_write_total",
    "provider",
)
MESSAGE_COLUMNS = ", ".join(MESSAGE_FIELDS)


@dataclass(slots=True)
class _Msg:
    """One `messages` row with its cost computed once."""

    ts: str
    date: str
    model: str
    host: str
    session_id: str
    prompt_index: int
    is_sidechain: int
    sidechain_key: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_5m: int
    cache_write_1h: int
    cache_write_total: int
    provider: str
    cost: float | None


def load_messages(rows: Iterable[Sequence[Any]], price_book: PriceBook) -> list[_Msg]:
    """Turn rows with the `MESSAGE_FIELDS` columns, in that order, into priced messages."""
    output: list[_Msg] = []
    for row in rows:
        (
            ts,
            model,
            host,
            session_id,
            prompt_index,
            is_sidechain,
            sidechain_key,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_5m,
            cache_write_1h,
            cache_write_total,
            provider,
        ) = tuple(row)
        model_name = str(model)
        usage = UsageCounts(
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cache_read_tokens=int(cache_read_tokens),
            cache_write_5m=int(cache_write_5m),
            cache_write_1h=int(cache_write_1h),
            cache_write_total=int(cache_write_total),
        )
        cost = cost_usd(model_name, usage, price_book)
        output.append(
            _Msg(
                ts=ts,
                date=_date_part(ts),
                model=model_name,
                host=str(host),
                session_id=str(session_id),
                prompt_index=int(prompt_index),
                is_sidechain=int(is_sidechain),
                sidechain_key=sidechain_key,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_5m=usage.cache_write_5m,
                cache_write_1h=usage.cache_write_1h,
                cache_write_total=usage.cache_write_total,
                provider=str(provider),
                cost=None if cost is None else cost.total_usd,
            )
        )
    return output


def build_overview(
    conn: sqlite3.Connection,
    price_book: PriceBook,
    *,
    provider: str | None = None,
) -> dict[str, Any]:
    """Everything the dashboard needs except sessions, for one provider or all of them.

    `days` has one entry per day with usage: cost by model and by host, deduplicated tokens
    by model and raw tokens (every physical transcript line, as Claude Code counts them). The
    dashboard sums the days of the selected period, so any period is exact.
    """
    where = "WHERE provider = ?" if provider else ""
    params: tuple[str, ...] = (provider,) if provider else ()
    messages = load_messages(
        conn.execute(f"SELECT {MESSAGE_COLUMNS} FROM messages {where} ORDER BY ts, id", params),
        price_book,
    )
    unparsed = conn.execute("SELECT COUNT(*) FROM raw_lines WHERE parsed = 0").fetchone()[0]
    days = _daily_rows(messages)
    update_raw_usage_daily(conn)
    raw_daily = _raw_daily_map(conn, provider)
    zero_raw = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for day in days:
        day["rawTok"] = raw_daily.get(str(day["date"]), zero_raw)
    contacts = host_contacts(conn)
    hosts = _hosts_payload(messages)
    for host in hosts:
        contact = contacts.get(str(host["host"]))
        host["lastSeen"] = None if contact is None else contact.last_seen
        host["lastData"] = None if contact is None else contact.last_data
    return {
        "provider": provider,
        "providers": [
            str(row[0])
            for row in conn.execute("SELECT DISTINCT provider FROM messages ORDER BY provider")
        ],
        "days": days,
        "models": _models_payload(messages),
        "hosts": hosts,
        "rawUsage": _raw_usage_payload(conn, provider),
        "unpricedModels": sorted(
            {
                row.model
                for row in messages
                if row.cost is None and not is_synthetic_model(row.model)
            }
        ),
        "health": {
            "last_ingest": _format_datetime(last_ingest_ts(conn)),
            "hosts_seen": hosts_seen(conn),
            "unparsed_lines": int(unparsed),
            "hosts": {
                name: {"last_seen": contact.last_seen, "last_data": contact.last_data}
                for name, contact in contacts.items()
            },
        },
    }


def build_session_list(
    messages: list[_Msg],
    session_rows: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    """Sessions with usage in `messages` (already clipped to a period), newest first.

    No prompts or subagents here: the dashboard asks for them per session, so the list stays
    small even for the whole history.
    """
    metadata = {str(row["session_id"]): row for row in session_rows}
    output = [
        _session_summary(rows, metadata.get(session_id))
        for session_id, rows in _group_by_session(messages).items()
    ]
    return sorted(output, key=lambda item: str(item["lastTs"]), reverse=True)


def build_session_detail(
    messages: list[_Msg],
    session_row: sqlite3.Row | None,
) -> dict[str, Any]:
    """One session clipped to a period: its summary, prompts and subagents."""
    return {
        "session": _session_summary(messages, session_row),
        "prompts": _prompts_payload(messages),
        "subagents": _subagents_payload(messages),
    }


def _group_by_session(messages: list[_Msg]) -> dict[str, list[_Msg]]:
    grouped: dict[str, list[_Msg]] = defaultdict(list)
    for row in messages:
        grouped[row.session_id].append(row)
    return grouped


def _session_summary(rows: list[_Msg], session_row: sqlite3.Row | None) -> dict[str, Any]:
    by_model_rows = _group_by_model(rows)
    model_costs = {model: _mixed_cost(group) for model, group in by_model_rows.items()}
    session_daily: dict[str, float] = defaultdict(float)
    for row in rows:
        if row.cost is not None:
            session_daily[row.date] += row.cost
    last_ts = rows[-1].ts if session_row is None else session_row["last_ts"]
    return {
        "session_id": rows[0].session_id if rows else "",
        "project": None if session_row is None else session_row["project"],
        "host": rows[0].host if rows else "",
        "provider": rows[0].provider if rows else CLAUDE_PROVIDER,
        "started": _format_datetime(rows[0].ts if session_row is None else session_row["first_ts"]),
        "last": _format_datetime(last_ts),
        # full timestamp for ordering: two sessions can end in the same minute
        "lastTs": last_ts or "",
        "messages": len(rows),
        # usage on a model without a price: the session has a cost the totals cannot show
        "unpriced": any(row.cost is None and not is_synthetic_model(row.model) for row in rows),
        "promptCount": len({row.prompt_index for row in rows if row.prompt_index > 0}),
        "subagent_messages": sum(1 for row in rows if row.is_sidechain == 1),
        "byModel": {
            model: _round_money(model_costs[model])
            for model in sorted(by_model_rows)
            if model_costs[model] > 0
        },
        "total": _round_money(_mixed_cost(rows)),
        "daily": {
            date: _round_money(value) for date, value in sorted(session_daily.items()) if value > 0
        },
        "models": [
            _usage_payload(by_model_rows[model], model, cost=model_costs[model])
            for model in sorted(by_model_rows)
            if model_costs[model] >= 0.01
        ],
    }


def _daily_rows(messages: list[_Msg]) -> list[dict[str, Any]]:
    grouped: dict[str, list[_Msg]] = defaultdict(list)
    for row in messages:
        grouped[row.date].append(row)

    output: list[dict[str, Any]] = []
    for date, rows in sorted(grouped.items()):
        by_model: dict[str, float] = defaultdict(float)
        by_host: dict[str, float] = defaultdict(float)
        by_model_tok: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        for row in rows:
            if row.cost is not None:
                by_model[row.model] += row.cost
                by_host[row.host] += row.cost
            if not is_synthetic_model(row.model):
                tok = by_model_tok[row.model]
                tok[0] += row.input_tokens
                tok[1] += row.output_tokens
                tok[2] += row.cache_read_tokens
                tok[3] += row.cache_write_total
        rounded_by_model = {
            model: _round_money(value) for model, value in sorted(by_model.items()) if value > 0
        }
        rounded_by_host = {
            host: _round_money(value) for host, value in sorted(by_host.items()) if value > 0
        }
        by_model_tok_out = {model: tok for model, tok in sorted(by_model_tok.items()) if any(tok)}
        output.append(
            {
                "date": date,
                "byModel": rounded_by_model,
                "byHost": rounded_by_host,
                "byModelTok": by_model_tok_out,
                "total": _round_money(sum(rounded_by_model.values())),
            }
        )
    return output


def _models_payload(messages: list[_Msg]) -> list[dict[str, Any]]:
    grouped: dict[str, list[_Msg]] = defaultdict(list)
    for row in messages:
        if is_synthetic_model(row.model):
            continue
        grouped[row.model].append(row)
    payloads: list[dict[str, Any]] = []
    for model, rows in grouped.items():
        payload = _usage_payload(rows, model)
        payload["provider"] = rows[0].provider
        payloads.append(payload)
    return sorted(payloads, key=lambda item: float(item["cost"]), reverse=True)


def _hosts_payload(messages: list[_Msg]) -> list[dict[str, Any]]:
    grouped: dict[str, list[_Msg]] = defaultdict(list)
    for row in messages:
        grouped[row.host].append(row)
    return sorted(
        [_usage_payload(rows, None, host=host) for host, rows in grouped.items()],
        key=lambda item: float(item["cost"]),
        reverse=True,
    )


def _prompts_payload(messages: list[_Msg]) -> list[dict[str, Any]]:
    grouped: dict[int, list[_Msg]] = defaultdict(list)
    for row in messages:
        grouped[row.prompt_index].append(row)

    output: list[dict[str, Any]] = []
    for prompt_index, rows in sorted(grouped.items()):
        main_rows = [row for row in rows if row.is_sidechain == 0]
        sub_rows = [row for row in rows if row.is_sidechain == 1]
        label = "(pre-prompt)" if prompt_index == 0 else f"Prompt #{prompt_index}"
        first_ts = min(str(row.ts) for row in rows)
        output.append(
            {
                "index": prompt_index,
                "ts": _time_part(first_ts),
                "date": _date_part(first_ts),
                "text": label,
                "main": _usage_payload(main_rows, None),
                "sub": _usage_payload(sub_rows, None),
                "messages": len(rows),
                "models": _positive_model_payloads(main_rows),
                "subModels": _positive_model_payloads(sub_rows),
            }
        )
    return output


def _positive_model_payloads(rows: list[_Msg]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for model, model_rows in sorted(_group_by_model(rows).items()):
        cost = _mixed_cost(model_rows)
        if cost > 0:
            output.append(_usage_payload(model_rows, model, cost=cost))
    return output


def _subagents_payload(messages: list[_Msg]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[_Msg]] = defaultdict(list)
    for row in messages:
        if row.is_sidechain != 1:
            continue
        key = str(row.sidechain_key or "unknown")
        grouped[(key, row.session_id, row.model)].append(row)

    output: list[dict[str, Any]] = []
    for (key, session_id, model), rows in sorted(grouped.items()):
        sub_daily: dict[str, float] = defaultdict(float)
        for row in rows:
            if row.cost is not None:
                sub_daily[row.date] += row.cost
        output.append(
            {
                "key": key,
                "session": session_id,
                "model": model,
                "daily": {d: _round_money(v) for d, v in sorted(sub_daily.items()) if v > 0},
                "messages": len(rows),
                "last": _format_datetime(max(row.ts for row in rows)),
                "lastTs": max(row.ts for row in rows),
                "input": sum(row.input_tokens for row in rows),
                "output": sum(row.output_tokens for row in rows),
                "cache_read": sum(row.cache_read_tokens for row in rows),
                "cache_write": sum(row.cache_write_total for row in rows),
                "cost": _round_money(_mixed_cost(rows)),
            }
        )
    return sorted(output, key=lambda item: float(item["cost"]), reverse=True)


# Raw tokens: for Claude every physical line (raw_usage_daily); for agent-normalized records
# (Codex) the messages themselves, which have no raw lines and no duplicates.
_RAW_CLAUDE_SQL = """
    SELECT date, model, input_tokens, output_tokens, cache_read_tokens,
           cache_write_tokens, messages
    FROM raw_usage_daily
"""
_RAW_RECORDS_SQL = """
    SELECT substr(ts, 1, 10) AS date, model, input_tokens, output_tokens, cache_read_tokens,
           cache_write_total AS cache_write_tokens, 1 AS messages
    FROM messages
    WHERE provider != 'claude'
"""


def _raw_rows_sql(provider: str | None) -> tuple[str, tuple[str, ...]]:
    if provider == CLAUDE_PROVIDER:
        return _RAW_CLAUDE_SQL, ()
    if provider:
        return _RAW_RECORDS_SQL + " AND provider = ?", (provider,)
    return _RAW_CLAUDE_SQL + " UNION ALL " + _RAW_RECORDS_SQL, ()


def _raw_usage_payload(conn: sqlite3.Connection, provider: str | None) -> dict[str, Any]:
    rows_sql, params = _raw_rows_sql(provider)
    models: list[dict[str, Any]] = [
        {
            "model": str(row["model"]),
            "input": int(row["input_tokens"]),
            "output": int(row["output_tokens"]),
            "cache_read": int(row["cache_read_tokens"]),
            "cache_write": int(row["cache_write_tokens"]),
            "messages": int(row["messages"]),
        }
        for row in conn.execute(
            f"""
            SELECT model,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(messages) AS messages
            FROM ({rows_sql})
            GROUP BY model
            """,
            params,
        )
    ]
    models.sort(
        key=lambda item: (
            int(item["input"])
            + int(item["output"])
            + int(item["cache_read"])
            + int(item["cache_write"])
        ),
        reverse=True,
    )
    return {
        "total": {
            "input": sum(int(m["input"]) for m in models),
            "output": sum(int(m["output"]) for m in models),
            "cache_read": sum(int(m["cache_read"]) for m in models),
            "cache_write": sum(int(m["cache_write"]) for m in models),
        },
        "models": models,
    }


def _raw_daily_map(conn: sqlite3.Connection, provider: str | None) -> dict[str, dict[str, int]]:
    rows_sql, params = _raw_rows_sql(provider)
    result: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        f"""
        SELECT date,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens
        FROM ({rows_sql})
        GROUP BY date
        """,
        params,
    ):
        result[str(row["date"])] = {
            "input": int(row["input_tokens"]),
            "output": int(row["output_tokens"]),
            "cache_read": int(row["cache_read_tokens"]),
            "cache_write": int(row["cache_write_tokens"]),
        }
    return result


def _usage_payload(
    rows: list[_Msg],
    model: str | None,
    *,
    host: str | None = None,
    cost: float | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "input": sum(row.input_tokens for row in rows),
        "output": sum(row.output_tokens for row in rows),
        "cache_read": sum(row.cache_read_tokens for row in rows),
        "cache_write": sum(row.cache_write_total for row in rows),
        "cost": _round_money(_mixed_cost(rows) if cost is None else cost),
    }
    if model is not None:
        payload["model"] = model
    if host is not None:
        payload["host"] = host
    return payload


def _mixed_cost(rows: list[_Msg]) -> float:
    total = 0.0
    for row in rows:
        if row.cost is not None:
            total += row.cost
    return total


def _group_by_model(rows: list[_Msg]) -> dict[str, list[_Msg]]:
    grouped: dict[str, list[_Msg]] = defaultdict(list)
    for row in rows:
        grouped[row.model].append(row)
    return grouped


def _date_part(value: object) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else ""


def _time_part(value: object) -> str:
    text = str(value or "")
    if "T" in text and len(text) >= 16:
        return text[11:16]
    if " " in text and len(text) >= 16:
        return text[11:16]
    return text[:5]


def _format_datetime(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    if "T" in text and len(text) >= 16:
        return f"{text[:10]} {text[11:16]}"
    if len(text) >= 16:
        return text[:16]
    return text


def _round_money(value: float) -> float:
    return round(value, 6)
