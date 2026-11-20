# Architecture

```
src/ccmeter/
  shared/   no dependencies: data records, the Claude line parser and minimizer
  client/   the agent: config, state, Claude and Codex sources, the shipping loop
  server/   FastAPI app, SQLite storage, prices, dashboard data and static files
  cli.py    the `ccmeter` command
```

`pip install ccmeter` brings the client only (`httpx`); the `server` extra adds FastAPI and
Uvicorn. A test makes sure the client never imports the server stack.

## Agent

Each pass (every 5 seconds by default):

1. List the transcript files and, for each, read the bytes after the saved offset. Only
   complete lines are taken; a half-written last line waits for the next pass.
2. Reduce each Claude line (`minimize_line`) and send batches of up to 500 to `POST /ingest`.
   A batch's offsets are saved only after the server accepted it: delivery is at least once
   and the server deduplicates.
3. Read the Codex rollouts the same way and send their numeric records in batches.
4. With nothing to send for five minutes, send an empty request (heartbeat).

State lives in `~/.ccmeter/state.json` (byte offsets per file, Codex reader state per rollout),
written atomically. Server errors make the agent back off exponentially up to a minute.

## Server

`POST /ingest` stores Claude lines in `raw_lines` (deduplicated by host, file, byte offset and
content) and parses them into `messages`: one row per API response (`message.id`), keeping the
largest counters when a response appears several times. Codex records go straight into
`messages` with `provider = 'codex'`. Lines, records and the host's contact times commit in one
transaction.

Costs are never stored. Every read prices the messages with `prices.json`, so a price change
applies to the whole history at the next refresh.

The dashboard overview (one entry per day with cost by model and host, tokens by model and raw
tokens) is cached: a request gets the last copy at once, and a copy older than a minute is
rebuilt in the background. Only the very first request after a start waits for a build, and
the server starts that build itself. Session lists and details are computed per request for the
chosen period and are small.

Raw tokens (every transcript line, as Claude Code counts them) come from an aggregate per day
and model that is updated incrementally from the new `raw_lines`.

One server process owns a database: it holds an operating-system lock on `<db>.lock`, and the
maintenance commands (`minimize`, `vacuum`, `reprocess`) refuse to run while it is held. Uvicorn
runs with a single worker because SQLite has a single writer.

## Dashboard

Plain files in `src/ccmeter/server/static/`: `index.html`, `app.js` (Preact + htm, no build),
`styles.css`, the bundled libraries and fonts. The page loads nothing from other origins and
the server sends a Content-Security-Policy that forbids it. All state (period, provider,
grouping, open session) is in the URL, so a reload or a shared link shows the same view.
