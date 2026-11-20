# API

All read endpoints return JSON and accept these query parameters:

| Parameter | Meaning |
|---|---|
| `from`, `to` | `YYYY-MM-DD` (UTC). Both days included. |
| `host` | one machine, as named by its agent |
| `model` | one model id |
| `provider` | `claude` or `codex` |

Costs are US dollars at list prices, computed at request time. Days are UTC dates.

## Ingest

`POST /ingest` with `Authorization: Bearer <CCMETER_API_TOKEN>`:

```json
{
  "host": "laptop",
  "batch": [{"file": "C--work-web-shop/3f2a.jsonl", "line": "{...reduced line...}", "offset": 4096}],
  "records": [{"provider": "codex", "message_id": "resp_1", "session_id": "s", "timestamp": "2026-01-15T10:00:00Z",
               "model": "gpt-5.5", "input_tokens": 1200, "cache_read_tokens": 9000, "output_tokens": 300}]
}
```

`batch` (Claude lines, at most 500) and `records` (Codex usage, at most 2000) can both be empty:
that is a heartbeat. The answer counts what was accepted, duplicated or rejected; `records_*`
fields appear when records were sent. Paths must be relative and without `..`.

## Read

`GET /api/health`

```json
{"ok": true, "last_ingest_ts": "2026-01-15 10:00:03", "hosts_seen": ["laptop"],
 "unparsed_lines": 0, "hosts": {"laptop": {"last_seen": "2026-01-15 10:05:00", "last_data": "2026-01-15 10:00:03"}}}
```

`last_seen` moves with every authenticated request (heartbeats included), `last_data` only when
new usage was accepted. `null` means no contact since the server was upgraded to 1.0.

`GET /api/summary`: `by_model`, `by_host`, `by_provider` (each with `input`, `output`,
`cache_read`, `cache_write`, `cost_usd`), `total_cost_usd`, `unpriced_models`.

`GET /api/spend/daily`: `days` (`date`, `by_model`, `total_usd`), `projection_month_usd` (average
day of the period times 30).

`GET /api/sessions`: sessions with messages in the period, newest first.
`GET /api/sessions/{id}`: one session with its prompts and their messages.

`GET /api/subagents`: usage of subagents (Claude Code sidechains, Codex child threads) by agent,
session and model.

## Dashboard data

`GET /api/dashboard/overview?provider=`: one entry per day with usage.

```json
{"days": [{"date": "2026-01-15",
           "byModel": {"claude-sonnet-4-5": 1.92},
           "byHost": {"laptop": 1.92},
           "byModelTok": {"claude-sonnet-4-5": [1200, 300, 90000, 4000]},
           "rawTok": {"input": 2400, "output": 600, "cache_read": 180000, "cache_write": 8000},
           "total": 1.92}],
 "models": [{"model": "claude-sonnet-4-5", "provider": "claude", "cost": 1.92, "input": 1200, "...": "..."}],
 "hosts": [{"host": "laptop", "cost": 1.92, "lastSeen": "...", "lastData": "..."}],
 "providers": ["claude", "codex"], "rawUsage": {"total": {}, "models": []},
 "unpricedModels": [], "health": {}}
```

`byModelTok` is `[input, output, cache read, cache write]`, deduplicated; `rawTok` counts every
transcript line, as Claude Code does. Sum the days of any period to get its numbers. Served
from a one-minute cache with an ETag: send `If-None-Match` to get a `304` when nothing changed.
An older copy is answered at once while a newer one is built: `X-Ccmeter-Refreshing` is then
`1` (ask again shortly to get the new numbers), otherwise `0`; 304 answers carry it too.

`GET /api/dashboard/session-list?from=&to=&provider=`: sessions of the period, clipped to it
(`total`, `byModel`, `models`, `promptCount`, `messages`, `daily`, `project`, `host`,
`provider`, `started`, `last`).

`GET /api/dashboard/session-detail/{id}?from=&to=&provider=`: the same summary plus `prompts`
(per prompt: main and subagent usage, by model) and `subagents`. `404` when the session has no
usage in the period.

Both answer gzip-compressed when the client accepts it.

## Removed in 1.0

`GET /dashboard/ccdata.js` and `GET /api/dashboard/sessions` served the previous dashboard.
Use `/api/dashboard/overview`, `/api/dashboard/session-list` and
`/api/dashboard/session-detail/{id}`.
