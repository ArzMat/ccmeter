"""SQLite storage: raw Claude lines, normalized messages and sessions, small aggregates.

Single writer by design (one server process). Costs are never stored: they are computed at
query time from the price book, so a price change applies to the whole history.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ccmeter.server.pricing import is_synthetic_model
from ccmeter.shared.claude_lines import minimize_line, parse_line
from ccmeter.shared.models import AssistantUsage, ProviderUsage, Unparseable, UserPrompt

SCHEMA_VERSION = "3"
BUSY_TIMEOUT_MS = 5000
CLAUDE_PROVIDER = "claude"
EXCLUDED_MODELS = frozenset({"codex-auto-review"})
LAST_RECORD_INGEST_KEY = "records_last_ingest_ts"
HOST_LAST_SEEN_PREFIX = "host_last_seen:"
HOST_LAST_DATA_PREFIX = "host_last_data:"
RAW_DAILY_LAST_ID_KEY = "raw_usage_daily_last_id"
# meta key of the last prompt of a transcript file: prefix + JSON [session, host, file]
CURSOR_PREFIX = "prompt_cursor:"
MAINTENANCE_BATCH_ROWS = 1000

PromptState = dict[tuple[str, str], tuple[str, int]]


@dataclass(frozen=True)
class IngestResult:
    """Counters returned by an ingest batch."""

    accepted: int
    parsed: int
    duplicates: int
    errors: int


@dataclass(frozen=True)
class RecordIngestResult:
    """Counters returned by a batch of agent-normalized usage records."""

    received: int
    inserted: int
    duplicates: int
    excluded: int


@dataclass(frozen=True)
class ReprocessResult:
    """Counters returned by a raw-line reprocess pass."""

    raw_lines: int
    parsed: int
    errors: int
    messages: int
    sessions: int
    duplicates: int


@dataclass(frozen=True)
class MinimizeResult:
    """Counters of a `minimize_raw_lines` pass (or of its dry run)."""

    scanned: int
    rewritten: int
    conflicts: int
    null_offsets: int
    bytes_before: int
    bytes_after: int


@dataclass(frozen=True)
class HostContact:
    """When a host last talked to the server (any request) and last sent usage."""

    last_seen: str | None
    last_data: str | None


def connect_db(db_path: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection for ccmeter.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        SQLite connection with row access by column name.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Create the SQLite schema if it does not exist (idempotent, upgrades old databases)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS raw_lines (
          id INTEGER PRIMARY KEY,
          host TEXT NOT NULL,
          file TEXT NOT NULL,
          byte_offset INTEGER,
          content TEXT NOT NULL,
          received_ts TEXT NOT NULL,
          parsed INTEGER NOT NULL DEFAULT 0,
          error TEXT,
          content_hash TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_dedup ON raw_lines(content_hash);
        CREATE INDEX IF NOT EXISTS idx_raw_host ON raw_lines(host);
        CREATE INDEX IF NOT EXISTS idx_raw_received_ts ON raw_lines(received_ts);
        CREATE INDEX IF NOT EXISTS idx_raw_parsed ON raw_lines(parsed);

        CREATE TABLE IF NOT EXISTS sessions (
          session_id TEXT PRIMARY KEY,
          host TEXT NOT NULL,
          project TEXT,
          first_ts TEXT,
          last_ts TEXT,
          provider TEXT NOT NULL DEFAULT 'claude'
        );

        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY,
          message_id TEXT NOT NULL,
          session_id TEXT NOT NULL REFERENCES sessions(session_id),
          host TEXT NOT NULL,
          ts TEXT NOT NULL,
          model TEXT NOT NULL,
          prompt_id TEXT,
          prompt_index INTEGER,
          is_sidechain INTEGER NOT NULL DEFAULT 0,
          sidechain_key TEXT,
          parent_uuid TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          cache_read_tokens INTEGER NOT NULL DEFAULT 0,
          cache_write_5m INTEGER NOT NULL DEFAULT 0,
          cache_write_1h INTEGER NOT NULL DEFAULT 0,
          cache_write_total INTEGER NOT NULL DEFAULT 0,
          raw_line_id INTEGER REFERENCES raw_lines(id),
          provider TEXT NOT NULL DEFAULT 'claude',
          reasoning_tokens INTEGER NOT NULL DEFAULT 0,
          service_tier TEXT,
          thread_id TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_dedup ON messages(message_id);
        CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, prompt_index);
        CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts);
        CREATE INDEX IF NOT EXISTS idx_msg_model ON messages(model);

        CREATE TABLE IF NOT EXISTS raw_usage_daily (
          date TEXT NOT NULL,
          model TEXT NOT NULL,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          cache_read_tokens INTEGER NOT NULL DEFAULT 0,
          cache_write_tokens INTEGER NOT NULL DEFAULT 0,
          messages INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (date, model)
        );
        """
    )
    conn.execute(
        """
        INSERT INTO meta(key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (SCHEMA_VERSION,),
    )
    _ensure_column(conn, "raw_lines", "byte_offset", "INTEGER")
    # schema 3 (Codex): columns added to existing databases, older rows stay 'claude'
    _ensure_column(conn, "sessions", "provider", "TEXT NOT NULL DEFAULT 'claude'")
    _ensure_column(conn, "messages", "provider", "TEXT NOT NULL DEFAULT 'claude'")
    _ensure_column(conn, "messages", "reasoning_tokens", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "messages", "service_tier", "TEXT")
    _ensure_column(conn, "messages", "thread_id", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_provider ON messages(provider)")
    conn.commit()


def touch_host(conn: sqlite3.Connection, host: str, *, has_data: bool) -> None:
    """Record that `host` contacted the server now; `has_data` also marks a usage delivery.

    Uses the reception time, not the event time: an agent catching up on old transcripts is
    still alive now. Not committed here: the caller's ingest transaction commits it.
    """
    keys = [HOST_LAST_SEEN_PREFIX + host]
    if has_data:
        keys.append(HOST_LAST_DATA_PREFIX + host)
    for key in keys:
        conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key,),
        )


def host_contacts(conn: sqlite3.Connection) -> dict[str, HostContact]:
    """Last contact and last delivery per host; None when unknown (before the upgrade)."""
    seen: dict[str, str] = {}
    data: dict[str, str] = {}
    for row in conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE ? OR key LIKE ?",
        (HOST_LAST_SEEN_PREFIX + "%", HOST_LAST_DATA_PREFIX + "%"),
    ):
        key = str(row["key"])
        if key.startswith(HOST_LAST_SEEN_PREFIX):
            seen[key.removeprefix(HOST_LAST_SEEN_PREFIX)] = str(row["value"])
        else:
            data[key.removeprefix(HOST_LAST_DATA_PREFIX)] = str(row["value"])
    hosts = set(hosts_seen(conn)) | set(seen) | set(data)
    return {
        host: HostContact(last_seen=seen.get(host), last_data=data.get(host))
        for host in sorted(hosts)
    }


def ingest_usage_records(
    conn: sqlite3.Connection,
    *,
    host: str,
    records: Sequence[ProviderUsage],
    commit: bool = True,
) -> RecordIngestResult:
    """Store agent-normalized usage records (Codex) directly in `messages`.

    No raw_lines: the agent sends numbers only. Deduplication by message_id (the response id
    for format B, a content hash for format A, so the copies inside forked rollouts collide).
    """
    inserted = 0
    duplicates = 0
    excluded = 0
    for record in records:
        if record.model.lower() in EXCLUDED_MODELS:
            excluded += 1
            continue
        existing = conn.execute(
            "SELECT 1 FROM messages WHERE message_id = ?",
            (record.message_id,),
        ).fetchone()
        if existing is not None:
            duplicates += 1
            continue
        _upsert_record_session(conn, host, record)
        prompt_index = (
            _prompt_index(conn, record.session_id, record.turn_id) if record.turn_id else 0
        )
        conn.execute(
            """
            INSERT INTO messages(
              message_id, session_id, host, ts, model, prompt_id, prompt_index,
              is_sidechain, sidechain_key, parent_uuid, input_tokens, output_tokens,
              cache_read_tokens, cache_write_5m, cache_write_1h, cache_write_total,
              raw_line_id, provider, reasoning_tokens, service_tier, thread_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0, 0, ?, NULL, ?, ?, ?, ?)
            """,
            (
                record.message_id,
                record.session_id,
                host,
                record.timestamp,
                record.model,
                record.turn_id,
                prompt_index,
                int(record.is_sidechain),
                record.thread_id if record.is_sidechain else None,
                record.input_tokens,
                record.output_tokens,
                record.cache_read_tokens,
                record.cache_write_tokens,
                record.provider,
                record.reasoning_tokens,
                record.service_tier,
                record.thread_id,
            ),
        )
        inserted += 1
    if records:
        conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (LAST_RECORD_INGEST_KEY,),
        )
    if commit:
        conn.commit()
    return RecordIngestResult(
        received=len(records),
        inserted=inserted,
        duplicates=duplicates,
        excluded=excluded,
    )


def last_ingest_ts(conn: sqlite3.Connection) -> str | None:
    """Most recent ingest time across raw lines and normalized records."""
    raw = conn.execute("SELECT MAX(received_ts) FROM raw_lines").fetchone()[0]
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        (LAST_RECORD_INGEST_KEY,),
    ).fetchone()
    values = [str(value) for value in (raw, None if row is None else row[0]) if value]
    return max(values) if values else None


def hosts_seen(conn: sqlite3.Connection) -> list[str]:
    """Hosts that sent raw lines or normalized records."""
    hosts = {str(row[0]) for row in conn.execute("SELECT DISTINCT host FROM raw_lines")}
    hosts.update(
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT host FROM messages WHERE provider != ?",
            (CLAUDE_PROVIDER,),
        )
    )
    return sorted(hosts)


def ingest_batch(
    conn: sqlite3.Connection,
    *,
    host: str,
    batch: Sequence[Mapping[str, str]],
    commit: bool = True,
) -> IngestResult:
    """Ingest Claude JSONL lines into raw storage and the normalized message tables.

    Lines are minimized first: whatever an agent sends, the server keeps no conversation
    text. Already minimized lines (current agents) are unchanged by this.
    """
    accepted = 0
    parsed_count = 0
    duplicate_count = 0
    error_count = 0
    prompt_state = _PromptStateLoader(conn, host)

    for item in batch:
        file_path = item["file"]
        raw_line = minimize_line(item["line"])
        byte_offset = _optional_int(item.get("offset"))
        raw_id = _insert_raw_line(
            conn,
            host=host,
            file_path=file_path,
            raw_line=raw_line,
            byte_offset=byte_offset,
        )
        if raw_id is None:
            duplicate_count += 1
            continue

        accepted += 1
        parsed = parse_line(raw_line)

        if isinstance(parsed, Unparseable):
            error_count += 1
            _mark_raw_error(conn, raw_id, parsed.reason)
            continue

        if isinstance(parsed, UserPrompt):
            _remember_prompt(conn, prompt_state, parsed.session_id, file_path, parsed.prompt_id)
            _mark_raw_parsed(conn, raw_id)
            parsed_count += 1
            continue

        if isinstance(parsed, AssistantUsage):
            prompt_id, prompt_index = _current_prompt(
                prompt_state,
                parsed.session_id,
                file_path,
            )
            _upsert_session(conn, host, parsed, _project_from_file(file_path))
            inserted = _upsert_message(conn, host, raw_id, parsed, prompt_id, prompt_index)
            if not inserted:
                duplicate_count += 1
            _mark_raw_parsed(conn, raw_id)
            parsed_count += 1
            continue

        _mark_raw_parsed(conn, raw_id)
        parsed_count += 1

    if commit:
        conn.commit()
    return IngestResult(
        accepted=accepted,
        parsed=parsed_count,
        duplicates=duplicate_count,
        errors=error_count,
    )


def reprocess_raw_lines(conn: sqlite3.Connection) -> ReprocessResult:
    """Rebuild the normalized Claude tables by parsing every stored raw line again.

    Claude rows only: Codex records have no raw lines and are kept. Rows are read in id
    chunks, so a large database is never loaded in memory at once. Messages that already
    existed keep the prompt ingest gave them: part of that attribution depends on how lines
    were batched, which the stored lines cannot reproduce, and a reprocess must not move
    usage between prompts.
    """
    known_prompts: dict[str, tuple[str | None, int]] = {
        str(row["message_id"]): (row["prompt_id"], int(row["prompt_index"] or 0))
        for row in conn.execute(
            "SELECT message_id, prompt_id, prompt_index FROM messages WHERE provider = ?",
            (CLAUDE_PROVIDER,),
        )
    }
    conn.execute("DELETE FROM messages WHERE provider = ?", (CLAUDE_PROVIDER,))
    conn.execute("DELETE FROM sessions WHERE provider = ?", (CLAUDE_PROVIDER,))
    conn.execute("UPDATE raw_lines SET parsed = 0, error = NULL")
    prompt_state: dict[tuple[str, str], tuple[str, int]] = {}
    raw_count = 0
    parsed_count = 0
    error_count = 0
    duplicate_count = 0

    for row in _raw_rows_by_id(conn, "id, host, file, content"):
        raw_count += 1
        parsed = parse_line(str(row["content"]))
        raw_id = int(row["id"])
        file_path = str(row["file"])

        if isinstance(parsed, Unparseable):
            error_count += 1
            _mark_raw_error(conn, raw_id, parsed.reason)
            continue

        if isinstance(parsed, UserPrompt):
            _remember_prompt(conn, prompt_state, parsed.session_id, file_path, parsed.prompt_id)
            _mark_raw_parsed(conn, raw_id)
            parsed_count += 1
            continue

        if isinstance(parsed, AssistantUsage):
            prompt_id, prompt_index = _current_prompt(
                prompt_state,
                parsed.session_id,
                file_path,
            )
            _upsert_session(conn, str(row["host"]), parsed, _project_from_file(file_path))
            inserted = _upsert_message(
                conn,
                str(row["host"]),
                raw_id,
                parsed,
                prompt_id,
                prompt_index,
            )
            if not inserted:
                duplicate_count += 1
            _mark_raw_parsed(conn, raw_id)
            parsed_count += 1
            continue

        _mark_raw_parsed(conn, raw_id)
        parsed_count += 1

    # ingest resolves some links from the batch boundaries, which the raw lines cannot
    # reproduce: every message that already existed keeps the prompt it was shown under
    conn.executemany(
        "UPDATE messages SET prompt_id = ?, prompt_index = ? WHERE message_id = ?",
        [
            (prompt_id, index, message_id)
            for message_id, (prompt_id, index) in known_prompts.items()
        ],
    )
    _renumber_prompt_cursors(conn)
    message_count = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    session_count = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
    conn.commit()
    return ReprocessResult(
        raw_lines=raw_count,
        parsed=parsed_count,
        errors=error_count,
        messages=message_count,
        sessions=session_count,
        duplicates=duplicate_count,
    )


def minimize_raw_lines(conn: sqlite3.Connection, *, dry_run: bool) -> MinimizeResult:
    """Rewrite stored raw lines in their minimized form, dropping conversation text.

    Run it with the server stopped (the caller holds the process lock). Idempotent: lines
    already minimized are left alone. A rewritten line whose new hash equals another row's is
    the same physical line stored twice (once full, once minimized by a newer agent): the
    older row is kept and the newer one deleted, its messages re-pointed to the kept row.
    Rows without byte offset are never merged with rows that have one: two identical lines
    in a file are legitimate. The raw-token aggregate is reset at the start and at the end,
    so the next dashboard refresh rebuilds it from the surviving rows.
    """
    scanned = rewritten = conflicts = null_offsets = 0
    bytes_before = bytes_after = 0
    projected: set[str] = set()
    if not dry_run:
        _reset_raw_usage_daily(conn)
        conn.commit()
    for row in _raw_rows_by_id(conn, "id, host, file, byte_offset, content"):
        scanned += 1
        if not dry_run and scanned % MAINTENANCE_BATCH_ROWS == 0:
            # an interruption keeps every chunk done so far; the aggregate stays empty and is
            # rebuilt from the surviving rows, and a new run picks up where this one stopped
            conn.commit()
        raw_id = int(row["id"])
        content = str(row["content"])
        offset = _optional_int(row["byte_offset"])
        if offset is None:
            null_offsets += 1
        reduced = minimize_line(content)
        bytes_before += len(content.encode("utf-8"))
        bytes_after += len(reduced.encode("utf-8"))
        if reduced == content:
            continue
        rewritten += 1
        new_hash = _content_hash(str(row["host"]), str(row["file"]), reduced, offset)
        other = conn.execute(
            "SELECT id FROM raw_lines WHERE content_hash = ? AND id != ?",
            (new_hash, raw_id),
        ).fetchone()
        if other is not None or new_hash in projected:
            conflicts += 1
        if dry_run:
            projected.add(new_hash)
            continue
        if other is not None:
            other_id = int(other["id"])
            keep_id, drop_id = (other_id, raw_id) if other_id < raw_id else (raw_id, other_id)
            conn.execute(
                "UPDATE messages SET raw_line_id = ? WHERE raw_line_id = ?", (keep_id, drop_id)
            )
            conn.execute("DELETE FROM raw_lines WHERE id = ?", (drop_id,))
            if drop_id == raw_id:
                continue
        conn.execute(
            "UPDATE raw_lines SET content = ?, content_hash = ? WHERE id = ?",
            (reduced, new_hash, raw_id),
        )
    if not dry_run:
        _reset_raw_usage_daily(conn)
        conn.commit()
    return MinimizeResult(
        scanned=scanned,
        rewritten=rewritten,
        conflicts=conflicts,
        null_offsets=null_offsets,
        bytes_before=bytes_before,
        bytes_after=bytes_after,
    )


def update_raw_usage_daily(conn: sqlite3.Connection) -> None:
    """Fold newly ingested raw_lines into the per-day/per-model raw aggregate.

    Only rows past the last processed id are parsed, so a refresh costs time proportional to
    the new lines, not the whole history. Callers serialize this (the dashboard builds hold a
    lock and maintenance commands hold the process lock), so a row is never folded twice.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (RAW_DAILY_LAST_ID_KEY,)).fetchone()
    last_id = int(row["value"]) if row is not None else 0
    acc: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    max_id = last_id
    for raw in conn.execute(
        "SELECT id, content FROM raw_lines WHERE id > ? ORDER BY id", (last_id,)
    ):
        max_id = int(raw["id"])
        parsed = parse_line(str(raw["content"]))
        if isinstance(parsed, AssistantUsage) and not is_synthetic_model(parsed.model):
            date = str(parsed.timestamp or "")[:10]
            bucket = acc[(date, parsed.model)]
            bucket[0] += parsed.input_tokens
            bucket[1] += parsed.output_tokens
            bucket[2] += parsed.cache_read_tokens
            bucket[3] += parsed.cache_write_total
            bucket[4] += 1
    if max_id == last_id:
        return
    for (date, model), (inp, out, cread, cwrite, msgs) in acc.items():
        conn.execute(
            """
            INSERT INTO raw_usage_daily(
              date, model, input_tokens, output_tokens,
              cache_read_tokens, cache_write_tokens, messages)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, model) DO UPDATE SET
              input_tokens = input_tokens + excluded.input_tokens,
              output_tokens = output_tokens + excluded.output_tokens,
              cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
              cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
              messages = messages + excluded.messages
            """,
            (date, model, inp, out, cread, cwrite, msgs),
        )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (RAW_DAILY_LAST_ID_KEY, str(max_id)),
    )
    conn.commit()


def backup_database(db_path: Path, dest_path: Path) -> None:
    """Consistent copy of a live database with SQLite's online backup API.

    Includes the transactions still in the WAL, unlike copying the main file. Refuses to
    overwrite an existing destination.

    Raises:
        FileExistsError: `dest_path` already exists.
        FileNotFoundError: `db_path` does not exist.
        ValueError: `db_path` is not a ccmeter database.
    """
    if dest_path.exists():
        raise FileExistsError(dest_path)
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    # read-only: a wrong path must never create an empty database that then gets "backed up"
    source = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        try:
            row = source.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"{db_path} is not a ccmeter database") from exc
        if row is None:
            raise ValueError(f"{db_path} is not a ccmeter database")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _reset_raw_usage_daily(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM raw_usage_daily")
    conn.execute("DELETE FROM meta WHERE key = ?", (RAW_DAILY_LAST_ID_KEY,))


def _raw_rows_by_id(conn: sqlite3.Connection, columns: str) -> Iterator[sqlite3.Row]:
    """All raw_lines rows in id order, read MAINTENANCE_BATCH_ROWS at a time.

    Only one chunk is in memory at once (the table can weigh tens of GB). Each chunk is
    fetched completely before its rows are yielded, so the caller may update or delete the
    rows it visits: no cursor stays open across its writes. `columns` must include `id`.
    """
    last_id = 0
    while True:
        chunk = conn.execute(
            f"SELECT {columns} FROM raw_lines WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, MAINTENANCE_BATCH_ROWS),
        ).fetchall()
        if not chunk:
            return
        yield from chunk
        last_id = int(chunk[-1]["id"])


class _PromptStateLoader(dict[tuple[str, str], tuple[str, int]]):
    """Prompt state of an ingest batch, loaded per session only when needed.

    Equivalent to loading, before the batch, the last prompt (by message id) of every
    session: a session's entry is read on first use and only from rows that existed when
    the batch started, so rows inserted by the batch itself never leak in.

    The last prompt of each transcript file is also kept between batches, as a `meta` key
    per session, host and file (the schema stays the one older servers read): a prompt line
    that reached the server in an earlier batch than its reply still owns that reply.
    """

    def __init__(self, conn: sqlite3.Connection, host: str) -> None:
        super().__init__()
        self._conn = conn
        self._host = host
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()
        self._max_id = int(row[0])
        self._loaded: set[str] = set()

    def remember(self, session_id: str, file_path: str, prompt_id: str) -> None:
        """Make `prompt_id` the current prompt of the file, here and for later batches."""
        index = self._index_for(session_id, file_path, prompt_id)
        self[(session_id, file_path)] = (prompt_id, index)
        after = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        self._conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (
                _cursor_key(session_id, self._host, file_path),
                json.dumps({"prompt": prompt_id, "index": index, "after": int(after)}),
            ),
        )

    def earlier_prompt(self, session_id: str, file_path: str) -> tuple[str, int] | None:
        """The prompt earlier batches left current for a file this batch has no prompt for.

        The file's own last prompt line wins. Only a message of the same file written after
        the cursor under a different prompt overrides it: this server gives such messages the
        cursor's prompt, or a newer prompt line that moves the cursor, so a different prompt
        means a server without cursors (e.g. after a rollback) ingested the file meanwhile,
        whatever index it chose. Messages of other files of the session never decide which
        prompt this file is answering.
        """
        cursor = self._cursors(session_id).get((self._host, file_path))
        if cursor is None:
            return self.last_prompt(session_id)
        row = self._conn.execute(
            """
            SELECT m.prompt_id, m.prompt_index
            FROM messages m JOIN raw_lines r ON r.id = m.raw_line_id
            WHERE m.session_id = ? AND m.provider = ? AND m.prompt_id IS NOT NULL
              AND m.id > ? AND m.id <= ? AND r.host = ? AND r.file = ?
            ORDER BY m.id DESC
            LIMIT 1
            """,
            (
                session_id,
                CLAUDE_PROVIDER,
                cursor.after,
                self._max_id,
                self._host,
                file_path,
            ),
        ).fetchone()
        if row is not None and str(row["prompt_id"]) != cursor.prompt_id:
            return str(row["prompt_id"]), int(row["prompt_index"])
        return cursor.prompt_id, cursor.index

    def _index_for(self, session_id: str, file_path: str, prompt_id: str) -> int:
        """Index of a prompt: its messages' one, or the one another file already gave it,
        or the next free one. Prompts still waiting for a reply in other files of the session
        count as taken, so two of them never share an index; this file's own previous prompt
        does not (a prompt interrupted before any reply leaves no gap)."""
        known = _known_prompt_index(self._conn, session_id, prompt_id)
        if known is not None:
            return known
        others = [
            cursor
            for place, cursor in self._cursors(session_id).items()
            if place != (self._host, file_path)
        ]
        for cursor in others:
            if cursor.prompt_id == prompt_id:
                return cursor.index
        return max([_max_prompt_index(self._conn, session_id)] + [c.index for c in others]) + 1

    def _cursors(self, session_id: str) -> dict[tuple[str, str], _PromptCursor]:
        start = _cursor_session_prefix(session_id)
        cursors: dict[tuple[str, str], _PromptCursor] = {}
        for row in self._conn.execute(
            "SELECT key, value FROM meta WHERE key >= ? AND key < ?",
            (start, start + "\U0010ffff"),
        ):
            _session, host, file_path = json.loads(str(row["key"]).removeprefix(CURSOR_PREFIX))
            data = json.loads(str(row["value"]))
            cursors[(str(host), str(file_path))] = _PromptCursor(
                prompt_id=str(data["prompt"]), index=int(data["index"]), after=int(data["after"])
            )
        return cursors

    def last_prompt(self, session_id: str) -> tuple[str, int] | None:
        key = (session_id, "")
        if session_id not in self._loaded:
            self._loaded.add(session_id)
            row = self._conn.execute(
                """
                SELECT prompt_id, prompt_index
                FROM messages
                WHERE session_id = ? AND prompt_id IS NOT NULL AND provider = ? AND id <= ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_id, CLAUDE_PROVIDER, self._max_id),
            ).fetchone()
            if row is not None:
                self[key] = (str(row["prompt_id"]), int(row["prompt_index"]))
        return self.get(key)


def _insert_raw_line(
    conn: sqlite3.Connection,
    *,
    host: str,
    file_path: str,
    raw_line: str,
    byte_offset: int | None = None,
) -> int | None:
    content_hash = _content_hash(host, file_path, raw_line, byte_offset)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO raw_lines(
          host, file, byte_offset, content, received_ts, content_hash
        )
        VALUES (?, ?, ?, ?, datetime('now'), ?)
        """,
        (host, file_path, byte_offset, raw_line, content_hash),
    )
    if cursor.rowcount == 0:
        return None
    if cursor.lastrowid is None:
        raise RuntimeError("raw line insert did not return a row id")
    return cursor.lastrowid


def _mark_raw_error(conn: sqlite3.Connection, raw_id: int, error: str) -> None:
    conn.execute(
        "UPDATE raw_lines SET parsed = 0, error = ? WHERE id = ?",
        (error, raw_id),
    )


def _mark_raw_parsed(conn: sqlite3.Connection, raw_id: int) -> None:
    conn.execute(
        "UPDATE raw_lines SET parsed = 1, error = NULL WHERE id = ?",
        (raw_id,),
    )


def _upsert_message(
    conn: sqlite3.Connection,
    host: str,
    raw_id: int,
    parsed: AssistantUsage,
    prompt_id: str | None,
    prompt_index: int,
) -> bool:
    existing = conn.execute(
        "SELECT 1 FROM messages WHERE message_id = ?",
        (parsed.message_id,),
    ).fetchone()
    if existing is not None:
        # streaming chunks of one message repeat its id with growing counters: keep the max
        conn.execute(
            """
            UPDATE messages
            SET
              output_tokens = MAX(output_tokens, ?),
              input_tokens = MAX(input_tokens, ?),
              cache_read_tokens = MAX(cache_read_tokens, ?),
              cache_write_5m = MAX(cache_write_5m, ?),
              cache_write_1h = MAX(cache_write_1h, ?),
              cache_write_total = MAX(cache_write_total, ?)
            WHERE message_id = ?
            """,
            (
                parsed.output_tokens,
                parsed.input_tokens,
                parsed.cache_read_tokens,
                parsed.cache_write_5m,
                parsed.cache_write_1h,
                parsed.cache_write_total,
                parsed.message_id,
            ),
        )
        return False

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
            parsed.message_id,
            parsed.session_id,
            host,
            parsed.timestamp or "",
            parsed.model,
            prompt_id,
            prompt_index,
            int(parsed.is_sidechain),
            parsed.sidechain_key,
            parsed.parent_uuid,
            parsed.input_tokens,
            parsed.output_tokens,
            parsed.cache_read_tokens,
            parsed.cache_write_5m,
            parsed.cache_write_1h,
            parsed.cache_write_total,
            raw_id,
        ),
    )
    return True


def _upsert_session(
    conn: sqlite3.Connection,
    host: str,
    parsed: AssistantUsage,
    project: str | None,
) -> None:
    timestamp = parsed.timestamp or ""
    conn.execute(
        """
        INSERT INTO sessions(session_id, host, project, first_ts, last_ts)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
          last_ts = MAX(sessions.last_ts, excluded.last_ts),
          first_ts = MIN(sessions.first_ts, excluded.first_ts),
          project = COALESCE(sessions.project, excluded.project)
        """,
        (parsed.session_id, host, project, timestamp, timestamp),
    )


def _upsert_record_session(
    conn: sqlite3.Connection,
    host: str,
    record: ProviderUsage,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions(session_id, host, project, first_ts, last_ts, provider)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
          last_ts = MAX(sessions.last_ts, excluded.last_ts),
          first_ts = MIN(sessions.first_ts, excluded.first_ts),
          project = COALESCE(sessions.project, excluded.project)
        """,
        (
            record.session_id,
            host,
            record.project,
            record.timestamp,
            record.timestamp,
            record.provider,
        ),
    )


def _remember_prompt(
    conn: sqlite3.Connection,
    prompt_state: PromptState,
    session_id: str,
    file_path: str,
    prompt_id: str | None,
) -> None:
    if prompt_id is None:
        return
    if isinstance(prompt_state, _PromptStateLoader):
        prompt_state.remember(session_id, file_path, prompt_id)
    else:
        prompt_state[(session_id, file_path)] = (
            prompt_id,
            _prompt_index(conn, session_id, prompt_id),
        )


def _current_prompt(
    prompt_state: PromptState,
    session_id: str,
    file_path: str,
) -> tuple[str | None, int]:
    current = prompt_state.get((session_id, file_path))
    if current is not None:
        return current
    if isinstance(prompt_state, _PromptStateLoader):
        current = prompt_state.earlier_prompt(session_id, file_path)
    else:
        current = prompt_state.get((session_id, ""))
    if current is not None:
        return current
    return None, 0


def _prompt_index(conn: sqlite3.Connection, session_id: str, prompt_id: str) -> int:
    known = _known_prompt_index(conn, session_id, prompt_id)
    if known is not None:
        return known
    return _max_prompt_index(conn, session_id) + 1


def _known_prompt_index(conn: sqlite3.Connection, session_id: str, prompt_id: str) -> int | None:
    row = conn.execute(
        """
        SELECT prompt_index
        FROM messages
        WHERE session_id = ? AND prompt_id = ?
        ORDER BY prompt_index
        LIMIT 1
        """,
        (session_id, prompt_id),
    ).fetchone()
    return None if row is None else int(row["prompt_index"])


def _max_prompt_index(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(prompt_index), 0) AS max_index FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["max_index"])


@dataclass(frozen=True)
class _PromptCursor:
    """Last prompt of a transcript file; `after`: the highest message id when it was set."""

    prompt_id: str
    index: int
    after: int


def _renumber_prompt_cursors(conn: sqlite3.Connection) -> None:
    """After a reprocess every message has a new id: the cursors stay valid, so their
    `after` moves past the rebuilt rows, which this server wrote itself."""
    top = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])
    start = CURSOR_PREFIX + "["
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key >= ? AND key < ?", (start, start + "\U0010ffff")
    ).fetchall()
    for row in rows:
        data = json.loads(str(row["value"]))
        data["after"] = top
        conn.execute("UPDATE meta SET value = ? WHERE key = ?", (json.dumps(data), row["key"]))


def _cursor_key(session_id: str, host: str, file_path: str) -> str:
    return CURSOR_PREFIX + json.dumps([session_id, host, file_path])


def _cursor_session_prefix(session_id: str) -> str:
    """Start of the keys of one session's cursors: `prompt_cursor:["<session>",`."""
    return CURSOR_PREFIX + json.dumps([session_id])[:-1] + ","


def _content_hash(
    host: str,
    file_path: str,
    raw_line: str,
    byte_offset: int | None,
) -> str:
    digest = hashlib.sha256()
    digest.update(host.encode("utf-8"))
    digest.update(b"|")
    digest.update(file_path.encode("utf-8"))
    digest.update(b"|")
    if byte_offset is not None:
        digest.update(str(byte_offset).encode("utf-8"))
        digest.update(b"|")
    digest.update(raw_line.encode("utf-8"))
    return digest.hexdigest()


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _project_from_file(file_path: str) -> str | None:
    normalized = file_path.replace("\\", "/")
    first_part = normalized.split("/", maxsplit=1)[0]
    if first_part:
        return first_part
    return None
