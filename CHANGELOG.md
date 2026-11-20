# Changelog

## 1.0.1

### Fixed
- The agent reads the Codex rollouts that Codex compresses (`rollout-*.jsonl.zst`) instead of
  skipping them, so sessions compressed before the agent saw them are counted. A rollout the
  agent already read and Codex compressed later is not counted twice: the server already has
  its requests and drops them by id. A compressed rollout read to the end is not decompressed
  again until it changes. Needs a Python with `compression.zstd` (the standard builds have
  it); without it the agent skips them with one warning, as before.
- After a pause the dashboard no longer shows the numbers of its last opening for up to a
  minute: an overview served from an older copy while a newer one is built says so
  (`X-Ccmeter-Refreshing: 1`, otherwise `0`) and the page asks again a moment later.

## 1.0.0

First public release.

### Added
- One `ccmeter` command with three roles: `ccmeter agent` (the client that runs on every
  machine), `ccmeter server`, and `ccmeter local` (both on one machine, no configuration).
- Codex usage next to Claude Code usage, with a provider filter in the dashboard and the API.
- New dashboard served by the server itself: no CDN, no third-party requests.
- Dashboard API: `GET /api/dashboard/overview`, `GET /api/dashboard/session-list` and
  `GET /api/dashboard/session-detail/{session_id}`.
- `/api/health` reports, for every host, when it was last seen (agents send a heartbeat every
  5 minutes when idle) and when it last delivered usage.
- `ccmeter server backup` (consistent copy of a live database), `ccmeter server minimize`
  (drops conversation text stored by earlier versions) and `ccmeter server vacuum`.
- Prices for Claude Fable 5.2, Claude Haiku 5.5 (priced by prompt length, higher rates over
  100,000 prompt tokens), GPT-6.2 Astra, GPT-6.2 Sol and GPT-6.1 Terra.

### Changed
- The agent sends only the fields usage accounting needs: prompts, answers, tool inputs and
  outputs, attachments and working directories never leave the machine. What it does send is
  the path of each transcript relative to `~/.claude/projects` (whose first folder is the
  project name) and, for Codex, the name of the project folder. The server applies the same
  reduction to lines sent by older agents. Details in `docs/privacy.md`.
- The dashboard is fast on large histories: each message is priced once per refresh, the data
  is served from a cache that refreshes in the background, compressed, with ETag revalidation.
- `ccmeter server report --days N` covers the last N calendar days; it used to sum everything.
- `ccmeter agent init` reads the server token from `CCMETER_API_TOKEN` or asks for it
  without echo; `--token` still works but leaves the token in the process list. The Windows
  installer no longer takes `-Token`.
- Python 3.15 or newer.

### Fixed
- Claude Sonnet 5.5 cache reads cost 0.05x the input price, not 0.1x.
- A prompt that reaches the server in one batch and its reply in the next one stay linked
  (the server keeps the last prompt of each transcript file between batches).

### Removed
- `GET /dashboard/ccdata.js` and `GET /api/dashboard/sessions`, the data endpoints of the
  previous dashboard (their only consumer). Use `/api/dashboard/overview` for the per-day,
  per-model and per-host numbers, `/api/dashboard/session-list` for the sessions of a period
  and `/api/dashboard/session-detail/{session_id}` for a session's prompts and subagents.
