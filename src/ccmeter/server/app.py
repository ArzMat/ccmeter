"""The ccmeter server: ingest API for the agents, read API and the dashboard."""

from __future__ import annotations

import gzip
import json
import os
import re
import secrets
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from ccmeter.server.cache import GZIP_LEVEL, CachedAsset, StaleWhileRevalidate
from ccmeter.server.dashboard_data import (
    MESSAGE_COLUMNS,
    build_overview,
    build_session_detail,
    build_session_list,
    load_messages,
)
from ccmeter.server.db import (
    connect_db,
    host_contacts,
    hosts_seen,
    ingest_batch,
    ingest_usage_records,
    initialize_schema,
    last_ingest_ts,
    touch_host,
)
from ccmeter.server.lock import ProcessLock
from ccmeter.server.pricing import (
    BUNDLED_PRICES_PATH,
    PriceBook,
    UsageCounts,
    cost_usd,
    is_per_request_priced,
    is_synthetic_model,
    load_price_book,
)
from ccmeter.shared.models import ProviderUsage

MAX_BATCH_LINES = 500
MAX_BATCH_RECORDS = 2000
MAX_ID_CHARS = 200
MAX_FILE_CHARS = 1000
MAX_LINE_BYTES = 5_000_000
DEFAULT_DB_PATH = Path.home() / ".ccmeter" / "ccmeter.db"
PLACEHOLDER_API_TOKEN = "change-me"
MONTH_DAYS = 30
OVERVIEW_CACHE_TTL_SECONDS = 60.0
GZIP_MIN_BYTES = 1024
STATIC_DIR = Path(__file__).with_name("static")
PROVIDER_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")
MAX_OVERVIEW_CACHES = 8
DASHBOARD_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)


class IngestLine(BaseModel):
    """One Claude transcript line sent by an agent (minimized by current agents)."""

    file: str = Field(min_length=1, max_length=MAX_FILE_CHARS)
    line: str
    offset: int | None = Field(default=None, ge=0)


class UsageRecordIn(BaseModel):
    """Usage already normalized by the agent (Codex): numbers and ids only, never text.

    Format documented in `ccmeter/client/codex_source.py`.
    """

    provider: Literal["codex"]
    message_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    session_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    thread_id: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    turn_id: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    is_sidechain: bool = False
    timestamp: str = Field(min_length=10, max_length=40)
    model: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    project: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    input_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    service_tier: str | None = Field(default=None, max_length=40)


class IngestRequest(BaseModel):
    """Request body of `POST /ingest`.

    `batch` carries Claude transcript lines, `records` Codex usage already normalized by the
    agent. Both may be missing or empty: an empty request is an agent heartbeat.
    """

    host: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    batch: list[IngestLine] = Field(default_factory=list[IngestLine], max_length=MAX_BATCH_LINES)
    records: list[UsageRecordIn] = Field(
        default_factory=list[UsageRecordIn],
        max_length=MAX_BATCH_RECORDS,
    )


class _DashboardSecurityHeaders(BaseHTTPMiddleware):
    """The dashboard loads nothing from other origins: say so to the browser."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/dashboard"):
            response.headers["Content-Security-Policy"] = DASHBOARD_CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            # revalidate every time (a 304 when unchanged): after an upgrade no browser keeps
            # running the previous version's script against the new API
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    *,
    db_path: Path,
    api_token: str,
    prices_path: Path = BUNDLED_PRICES_PATH,
    warm_dashboard: bool = False,
) -> FastAPI:
    """Create the FastAPI app for ccmeter.

    Args:
        db_path: SQLite database path.
        api_token: Static bearer token expected by `/ingest`.
        prices_path: Price book used to compute costs at query time.
        warm_dashboard: Build the dashboard overview in the background at startup, so the
            first visitor does not wait for it.

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(title="ccmeter", docs_url=None, redoc_url=None)
    app.add_middleware(_DashboardSecurityHeaders)

    # create or upgrade the schema once: request handlers then only read, or write data, and
    # never take the write lock just to check the schema
    conn = connect_db(db_path)
    try:
        initialize_schema(conn)
    finally:
        conn.close()

    # Overview builds and session lists can each load every message in memory: one of each at
    # a time keeps the peak bounded on a small server. Separate locks, so a session list never
    # waits for a background overview refresh.
    build_lock = threading.Lock()
    list_lock = threading.Lock()

    @app.post("/ingest")
    def ingest(  # pyright: ignore[reportUnusedFunction]
        request: IngestRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, int]:
        _require_bearer_token(authorization, api_token)
        _validate_batch(request.batch)
        conn = connect_db(db_path)
        try:
            # lines, records and the host's contact times commit together or not at all
            result = ingest_batch(
                conn,
                host=request.host,
                batch=[
                    {"file": item.file, "line": item.line, "offset": str(item.offset)}
                    if item.offset is not None
                    else {"file": item.file, "line": item.line}
                    for item in request.batch
                ],
                commit=False,
            )
            records_result = ingest_usage_records(
                conn,
                host=request.host,
                records=[_provider_usage(record) for record in request.records],
                commit=False,
            )
            touch_host(
                conn,
                request.host,
                has_data=result.accepted > 0 or records_result.inserted > 0,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        response = {
            "accepted": result.accepted,
            "parsed": result.parsed,
            "duplicates": result.duplicates,
            "errors": result.errors,
        }
        if request.records:
            # same answer as before for line-only payloads; agents advance their Codex state
            # only when they see records_received
            response.update(
                {
                    "records_received": records_result.received,
                    "records_inserted": records_result.inserted,
                    "records_duplicates": records_result.duplicates,
                    "records_excluded": records_result.excluded,
                }
            )
        return response

    @app.get("/api/health")
    def health() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        conn = connect_db(db_path)
        try:
            last_ingest = last_ingest_ts(conn)
            hosts = hosts_seen(conn)
            unparsed = conn.execute("SELECT COUNT(*) FROM raw_lines WHERE parsed = 0").fetchone()[0]
            contacts = host_contacts(conn)
        finally:
            conn.close()
        return {
            "ok": True,
            "last_ingest_ts": last_ingest,
            "hosts_seen": hosts,
            "unparsed_lines": unparsed,
            "hosts": {
                name: {"last_seen": contact.last_seen, "last_data": contact.last_data}
                for name, contact in contacts.items()
            },
        }

    @app.get("/api/summary")
    def summary(request: Request) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        conn = connect_db(db_path)
        try:
            rows = _filtered_message_rows(conn, request)
        finally:
            conn.close()
        price_book = load_price_book(prices_path)
        by_model = _aggregate_by_key(rows, "model", "model", price_book)
        by_host = _aggregate_by_key(rows, "host", "host", price_book)
        by_provider = _aggregate_by_key(rows, "provider", "provider", price_book)
        for item in by_provider:
            item["reasoning"] = sum(
                int(row["reasoning_tokens"]) for row in rows if row["provider"] == item["provider"]
            )
        return {
            "period": _period_from_request(request),
            "by_model": by_model,
            "by_host": by_host,
            "by_provider": by_provider,
            "total_cost_usd": _sum_priced_costs(by_model),
            "unpriced_models": _unpriced_models(rows, price_book),
        }

    @app.get("/api/spend/daily")
    def spend_daily(request: Request) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        conn = connect_db(db_path)
        try:
            rows = _filtered_message_rows(conn, request)
        finally:
            conn.close()
        price_book = load_price_book(prices_path)
        daily = _daily_spend(rows, price_book)
        total = sum(day["total_usd"] for day in daily)
        average = total / len(daily) if daily else 0.0
        return {
            "days": daily,
            "projection_month_usd": average * MONTH_DAYS,
            "projection_method": "period_average_daily_total_usd_x_30",
        }

    @app.get("/api/sessions")
    def sessions(request: Request) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        conn = connect_db(db_path)
        try:
            rows = _filtered_message_rows(conn, request)
            session_rows = conn.execute("SELECT * FROM sessions").fetchall()
        finally:
            conn.close()
        price_book = load_price_book(prices_path)
        return {"sessions": _session_summaries(rows, session_rows, price_book)}

    @app.get("/api/sessions/{session_id}")
    def session_detail(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        request: Request,
    ) -> dict[str, object]:
        conn = connect_db(db_path)
        try:
            rows = [
                row
                for row in _filtered_message_rows(conn, request)
                if row["session_id"] == session_id
            ]
            session_row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        price_book = load_price_book(prices_path)
        return {
            "session": _one_session_summary(rows, session_row, price_book),
            "prompts": _prompt_details(rows, price_book),
        }

    @app.get("/api/subagents")
    def subagents(request: Request) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        conn = connect_db(db_path)
        try:
            rows = [
                row for row in _filtered_message_rows(conn, request) if row["is_sidechain"] == 1
            ]
        finally:
            conn.close()
        price_book = load_price_book(prices_path)
        subagent_rows = _subagent_summaries(rows, price_book)
        totals_by_model = _aggregate_by_key(rows, "model", "model", price_book)
        return {
            "subagents": subagent_rows,
            "totals_by_model": totals_by_model,
            "grand_total_usd": _sum_priced_costs(totals_by_model),
        }

    overview_caches: dict[str, StaleWhileRevalidate] = {}
    overview_caches_lock = threading.Lock()

    def overview_cache(provider: str | None) -> StaleWhileRevalidate:
        key = provider or ""
        with overview_caches_lock:
            cache = overview_caches.get(key)
            if cache is None:
                if len(overview_caches) >= MAX_OVERVIEW_CACHES:
                    raise HTTPException(status_code=400, detail="Too many provider filters")

                def render() -> CachedAsset:
                    with build_lock:
                        conn = connect_db(db_path)
                        try:
                            payload = build_overview(
                                conn, load_price_book(prices_path), provider=provider
                            )
                        finally:
                            conn.close()
                    return CachedAsset.of(json.dumps(payload, separators=(",", ":")).encode())

                cache = StaleWhileRevalidate(render, ttl_seconds=OVERVIEW_CACHE_TTL_SECONDS)
                overview_caches[key] = cache
            return cache

    if warm_dashboard:
        overview_cache(None).refresh_in_background()

    def known_providers() -> set[str]:
        conn = connect_db(db_path)
        try:
            return {str(row[0]) for row in conn.execute("SELECT DISTINCT provider FROM messages")}
        finally:
            conn.close()

    @app.get("/api/dashboard/overview")
    def dashboard_overview(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        provider = _provider_param(request)
        # only providers with data get a cache slot: made-up names cannot fill the cache
        if provider is not None and provider not in known_providers():
            raise HTTPException(status_code=404, detail="No usage for this provider")
        asset = overview_cache(provider).get()
        headers = {"ETag": asset.etag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == asset.etag:
            return Response(status_code=304, headers=headers)
        return _maybe_gzip(
            request, asset.body, "application/json", gzipped=asset.gzipped, headers=headers
        )

    @app.get("/api/dashboard/session-list")
    def dashboard_session_list(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> Response:
        with list_lock:
            conn = connect_db(db_path)
            try:
                rows = _filtered_message_rows(conn, request, MESSAGE_COLUMNS)
                session_rows = list(conn.execute("SELECT * FROM sessions").fetchall())
            finally:
                conn.close()
            messages = load_messages(rows, load_price_book(prices_path))
            payload = {"sessions": build_session_list(messages, session_rows)}
        return _json_response(request, payload)

    @app.get("/api/dashboard/session-detail/{session_id}")
    def dashboard_session_detail(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        request: Request,
    ) -> Response:
        where, params = _message_filter(request)
        conn = connect_db(db_path)
        try:
            rows = list(
                conn.execute(
                    f"SELECT {MESSAGE_COLUMNS} FROM messages {where} AND session_id = ? "
                    "ORDER BY ts, id",
                    [*params, session_id],
                )
            )
            session_row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        if not rows:
            raise HTTPException(status_code=404, detail="No usage for this session in the period")
        messages = load_messages(rows, load_price_book(prices_path))
        return _json_response(request, build_session_detail(messages, session_row))

    @app.get("/")
    def root() -> RedirectResponse:  # pyright: ignore[reportUnusedFunction]
        return RedirectResponse(url="/dashboard/")

    @app.get("/dashboard")
    def dashboard_index() -> RedirectResponse:  # pyright: ignore[reportUnusedFunction]
        return RedirectResponse(url="/dashboard/")

    if STATIC_DIR.exists():
        app.mount("/dashboard", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard")

    return app


def create_default_app() -> FastAPI:
    """App configured from environment variables (for `uvicorn --factory`).

    `CCMETER_API_TOKEN` (required), `CCMETER_DB_PATH`, `CCMETER_PRICES_PATH`.
    """
    api_token = os.getenv("CCMETER_API_TOKEN", "")
    if api_token.strip() == "" or api_token == PLACEHOLDER_API_TOKEN:
        raise RuntimeError("CCMETER_API_TOKEN must be set to a non-placeholder value")
    db_path = Path(os.getenv("CCMETER_DB_PATH", str(DEFAULT_DB_PATH)))
    prices_path = Path(os.getenv("CCMETER_PRICES_PATH", str(BUNDLED_PRICES_PATH)))
    # the server owns the database for its whole life, like `ccmeter server run`: maintenance
    # commands refuse to start next to it (raises DatabaseLocked if one is running)
    lock = ProcessLock(db_path)
    lock.acquire()
    app = create_app(
        db_path=db_path, api_token=api_token, prices_path=prices_path, warm_dashboard=True
    )
    app.state.process_lock = lock
    return app


def _json_response(request: Request, payload: dict[str, Any]) -> Response:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _maybe_gzip(request, body, "application/json")


def _maybe_gzip(
    request: Request,
    body: bytes,
    media_type: str,
    *,
    gzipped: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """Return `body` gzip-encoded when the client accepts it and it is worth it."""
    out_headers = {"Vary": "Accept-Encoding", **(headers or {})}
    accepts_gzip = "gzip" in request.headers.get("accept-encoding", "").lower()
    if accepts_gzip and len(body) >= GZIP_MIN_BYTES:
        compressed = gzipped if gzipped is not None else gzip.compress(body, GZIP_LEVEL)
        out_headers["Content-Encoding"] = "gzip"
        return Response(content=compressed, media_type=media_type, headers=out_headers)
    return Response(content=body, media_type=media_type, headers=out_headers)


def _provider_param(request: Request) -> str | None:
    provider = request.query_params.get("provider") or None
    if provider is not None and not PROVIDER_PATTERN.fullmatch(provider):
        raise HTTPException(status_code=400, detail="Invalid provider")
    return provider


def _require_bearer_token(authorization: str | None, api_token: str) -> None:
    expected = f"Bearer {api_token}".encode()
    received = (authorization or "").encode()
    if not secrets.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _validate_batch(batch: list[IngestLine]) -> None:
    for item in batch:
        if _has_path_traversal(item.file):
            raise HTTPException(status_code=400, detail="Invalid file path")
        if len(item.line.encode("utf-8")) > MAX_LINE_BYTES:
            raise HTTPException(status_code=413, detail="Line too large")


def _provider_usage(record: UsageRecordIn) -> ProviderUsage:
    return ProviderUsage(
        provider=record.provider,
        message_id=record.message_id,
        session_id=record.session_id,
        thread_id=record.thread_id,
        turn_id=record.turn_id,
        is_sidechain=record.is_sidechain,
        timestamp=record.timestamp,
        model=record.model,
        project=record.project,
        input_tokens=record.input_tokens,
        cache_read_tokens=record.cache_read_tokens,
        cache_write_tokens=record.cache_write_tokens,
        output_tokens=record.output_tokens,
        reasoning_tokens=record.reasoning_tokens,
        service_tier=record.service_tier,
    )


def _has_path_traversal(file_path: str) -> bool:
    normalized = file_path.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        return True
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return True
    return any(part == ".." for part in path.parts)


def _filtered_message_rows(
    conn: sqlite3.Connection,
    request: Request,
    columns: str = "*",
) -> list[sqlite3.Row]:
    where, params = _message_filter(request)
    return list(conn.execute(f"SELECT {columns} FROM messages {where} ORDER BY ts, id", params))


def _message_filter(request: Request) -> tuple[str, list[str]]:
    """WHERE clause for the `from`, `to`, `host`, `model` and `provider` query parameters."""
    query = "WHERE 1 = 1"
    params: list[str] = []
    from_value = request.query_params.get("from")
    to_value = request.query_params.get("to")
    host = request.query_params.get("host")
    model = request.query_params.get("model")
    provider = request.query_params.get("provider")
    if from_value:
        query += " AND ts >= ?"
        params.append(from_value)
    if to_value:
        query += " AND ts <= ?"
        # a bare date (YYYY-MM-DD) must cover the whole day: extend past any time of day
        params.append(to_value if "T" in to_value else to_value + "T99:99")
    if host:
        query += " AND host = ?"
        params.append(host)
    if model:
        query += " AND model = ?"
        params.append(model)
    if provider:
        query += " AND provider = ?"
        params.append(provider)
    return query, params


def _period_from_request(request: Request) -> dict[str, str | None]:
    return {"from": request.query_params.get("from"), "to": request.query_params.get("to")}


def _usage_from_row(row: sqlite3.Row) -> UsageCounts:
    return UsageCounts(
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        cache_read_tokens=int(row["cache_read_tokens"]),
        cache_write_5m=int(row["cache_write_5m"]),
        cache_write_1h=int(row["cache_write_1h"]),
        cache_write_total=int(row["cache_write_total"]),
    )


def _usage_from_rows(rows: list[sqlite3.Row]) -> UsageCounts:
    return UsageCounts(
        input_tokens=sum(int(row["input_tokens"]) for row in rows),
        output_tokens=sum(int(row["output_tokens"]) for row in rows),
        cache_read_tokens=sum(int(row["cache_read_tokens"]) for row in rows),
        cache_write_5m=sum(int(row["cache_write_5m"]) for row in rows),
        cache_write_1h=sum(int(row["cache_write_1h"]) for row in rows),
        cache_write_total=sum(int(row["cache_write_total"]) for row in rows),
    )


def _usage_payload(
    rows: list[sqlite3.Row],
    model: str | None,
    price_book: PriceBook,
) -> dict[str, Any]:
    usage = _usage_from_rows(rows)
    cost_value = _usage_cost_value(rows, model, usage, price_book)
    return {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "cache_read": usage.cache_read_tokens,
        "cache_write": usage.cache_write_total,
        "cost_usd": cost_value,
    }


def _usage_cost_value(
    rows: list[sqlite3.Row],
    model: str | None,
    usage: UsageCounts,
    price_book: PriceBook,
) -> float | None:
    if model is None:
        return _mixed_cost(rows, price_book)
    if is_per_request_priced(model, price_book):
        # long-context threshold (Codex, Claude Haiku 5.5): it applies per request, so price
        # row by row
        return _mixed_cost(rows, price_book)
    cost = cost_usd(model, usage, price_book)
    if cost is None:
        return None
    return cost.total_usd


def _mixed_cost(rows: list[sqlite3.Row], price_book: PriceBook) -> float:
    total = 0.0
    for row in rows:
        cost = cost_usd(str(row["model"]), _usage_from_row(row), price_book)
        if cost is not None:
            total += cost.total_usd
    return total


def _aggregate_by_key(
    rows: list[sqlite3.Row],
    key: str,
    output_key: str,
    price_book: PriceBook,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row[key])].append(row)
    output: list[dict[str, Any]] = []
    for value, group in sorted(grouped.items()):
        model = value if key == "model" else None
        payload = _usage_payload(group, model, price_book)
        payload[output_key] = value
        output.append(payload)
    return output


def _sum_priced_costs(rows: list[dict[str, Any]]) -> float:
    return sum(float(row["cost_usd"]) for row in rows if row["cost_usd"] is not None)


def _unpriced_models(rows: list[sqlite3.Row], price_book: PriceBook) -> list[str]:
    unpriced: set[str] = set()
    for row in rows:
        model = str(row["model"])
        if is_synthetic_model(model):
            continue
        if cost_usd(model, _usage_from_row(row), price_book) is None:
            unpriced.add(model)
    return sorted(unpriced)


def _daily_spend(rows: list[sqlite3.Row], price_book: PriceBook) -> list[dict[str, Any]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["ts"])[:10]].append(row)
    output: list[dict[str, Any]] = []
    for date, day_rows in sorted(grouped.items()):
        by_model: dict[str, float] = defaultdict(float)
        for row in day_rows:
            cost = cost_usd(str(row["model"]), _usage_from_row(row), price_book)
            if cost is not None:
                by_model[str(row["model"])] += cost.total_usd
        total = sum(by_model.values())
        output.append(
            {
                "date": date,
                "by_model": dict(sorted(by_model.items())),
                "total_usd": total,
            }
        )
    return output


def _session_summaries(
    rows: list[sqlite3.Row],
    session_rows: list[sqlite3.Row],
    price_book: PriceBook,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    metadata = {str(row["session_id"]): row for row in session_rows}
    for row in rows:
        grouped[str(row["session_id"])].append(row)
    summaries = [
        _one_session_summary(group, metadata.get(session_id), price_book)
        for session_id, group in grouped.items()
    ]
    return sorted(summaries, key=lambda item: str(item["last"]), reverse=True)


def _one_session_summary(
    rows: list[sqlite3.Row],
    session_row: sqlite3.Row | None,
    price_book: PriceBook,
) -> dict[str, Any]:
    session_id = str(rows[0]["session_id"]) if rows else ""
    host = str(rows[0]["host"]) if rows else ""
    by_model_cost: dict[str, float] = defaultdict(float)
    for row in rows:
        cost = cost_usd(str(row["model"]), _usage_from_row(row), price_book)
        if cost is not None:
            by_model_cost[str(row["model"])] += cost.total_usd
    return {
        "session_id": session_id,
        "host": host,
        "provider": str(rows[0]["provider"]) if rows else None,
        "project": None if session_row is None else session_row["project"],
        "started": None if session_row is None else session_row["first_ts"],
        "last": None if session_row is None else session_row["last_ts"],
        "messages": len(rows),
        "prompts": len({int(row["prompt_index"]) for row in rows if int(row["prompt_index"]) > 0}),
        "subagent_messages": sum(1 for row in rows if int(row["is_sidechain"]) == 1),
        "by_model_cost": dict(sorted(by_model_cost.items())),
        "total_cost_usd": sum(by_model_cost.values()),
    }


def _prompt_details(rows: list[sqlite3.Row], price_book: PriceBook) -> list[dict[str, Any]]:
    grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[int(row["prompt_index"])].append(row)
    output: list[dict[str, Any]] = []
    for prompt_index, group in sorted(grouped.items()):
        main_rows = [row for row in group if int(row["is_sidechain"]) == 0]
        subagent_rows = [row for row in group if int(row["is_sidechain"]) == 1]
        output.append(
            {
                "prompt_index": prompt_index,
                "ts_first": min(str(row["ts"]) for row in group),
                "main": _usage_payload(main_rows, None, price_book),
                "subagents": _usage_payload(subagent_rows, None, price_book),
                "messages": [_message_payload(row, price_book) for row in group],
            }
        )
    return output


def _message_payload(row: sqlite3.Row, price_book: PriceBook) -> dict[str, Any]:
    cost = cost_usd(str(row["model"]), _usage_from_row(row), price_book)
    return {
        "message_id": row["message_id"],
        "ts": row["ts"],
        "model": row["model"],
        "provider": row["provider"],
        "is_sidechain": bool(row["is_sidechain"]),
        "sidechain_key": row["sidechain_key"],
        "input": row["input_tokens"],
        "output": row["output_tokens"],
        "cache_read": row["cache_read_tokens"],
        "cache_write": row["cache_write_total"],
        "cost_usd": None if cost is None else cost.total_usd,
    }


def _subagent_summaries(rows: list[sqlite3.Row], price_book: PriceBook) -> list[dict[str, Any]]:
    grouped: dict[tuple[str | None, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[(row["sidechain_key"], str(row["session_id"]), str(row["model"]))].append(row)
    output: list[dict[str, Any]] = []
    for (sidechain_key, session_id, model), group in sorted(
        grouped.items(), key=lambda item: (item[0][0] or "", item[0][1], item[0][2])
    ):
        payload = _usage_payload(group, model, price_book)
        payload.update(
            {
                "sidechain_key": sidechain_key,
                "session_id": session_id,
                "model": model,
                "messages": len(group),
            }
        )
        output.append(payload)
    return output
