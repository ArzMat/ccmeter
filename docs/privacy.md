# What ccmeter reads, sends and stores

ccmeter exists to count tokens, not to collect conversations. This page lists exactly what
crosses the network and what ends up in the database.

## Claude Code

**Read:** every `*.jsonl` file under `~/.claude/projects` (or `claude_projects_dir` in
`agent.json`), from the byte where the previous pass stopped.

**Sent:** each line is reduced on your machine by `minimize_line`
(`src/ccmeter/shared/claude_lines.py`) before it is sent. What survives:

| Field | Why |
|---|---|
| `type` | assistant reply, user prompt, or something to ignore |
| `uuid`, `parentUuid`, `sessionId`, `promptId`, `agentId`, `isSidechain` | identity: session, prompt, subagent |
| `timestamp` | the day and time of the request |
| `message.id` | deduplication: one API response is written on several lines |
| `message.model` | the price |
| `message.usage` | the token counters, numbers only (strings such as the service tier are dropped too) |
| `message.content` | its shape only: a string becomes `""`, a list keeps each block's `type` (that is how a typed prompt differs from a tool result) |

Everything else is dropped: the text of prompts and answers, tool inputs and outputs, thinking,
attachments, file snapshots, working directories, git branches, version strings. A line that is
not valid JSON is sent as an empty line, so the server can count it as unparseable.

The relative path of the transcript (for example `C--work-web-shop/3f2a....jsonl`) is sent too:
its first folder, the name Claude Code gives to the project, becomes the project name on the
dashboard.

## Codex

**Read:** `rollout-*.jsonl` under `~/.codex/sessions` and `~/.codex/archived_sessions` (or
`codex_dirs` in `agent.json`; `[]` turns Codex off), streamed from the last offset.

**Sent:** one numeric record per model request, built on your machine
(`src/ccmeter/client/codex_source.py`): ids (response id or a content hash, session, thread,
turn), timestamp, model, the name of the project folder, input / cached input / output /
reasoning tokens. Nothing else leaves the machine. Copies of a parent's events inside forked
rollouts get the same id as the original and are dropped by the server; `codex-auto-review`
guardian requests are never sent.

## The server

The server stores what it receives and nothing more: the reduced Claude lines (so it can
rebuild everything after a parser fix with `ccmeter server reprocess`), one row per model
response with its counters, sessions with their first and last timestamp and project, and when
each host was last in contact. It applies the same reduction to lines that older agents send
whole, so its database never holds conversation text.

Only `POST /ingest` requires the token. The dashboard and the read API are open to whoever
reaches the port: they show project folder names, hosts and usage. Keep the server on a private
network or behind an authenticated proxy.

## Cleaning an older database

Versions before 1.0 stored the whole transcript lines. To reduce them in place:

```bash
# stop the server first: these commands refuse to run next to it
ccmeter server backup ~/ccmeter-before-minimize.db   # consistent copy, kept until you are happy
ccmeter server minimize --dry-run                     # how many lines, how many duplicates
ccmeter server minimize --yes                         # rewrite the stored lines
ccmeter server vacuum --yes                           # give the space back to the disk
```

`minimize` works in chunks and can be interrupted: run it again and it continues. Two stored
rows become one only when, once reduced, they are the same line from the same host and file
with the same byte offset; the older row is kept. In practice:

- a line the same agent sent twice (once whole, once reduced) at the same offset ends up as
  one row;
- rows from very old agents that sent no byte offsets: two of them that become identical once
  reduced (for example lines that differed only in the text being dropped) are merged into
  one, so the raw token counts can go down; costs do not change, because they count each
  message id once. The dry run counts these merges (`duplicate copies`) before anything is
  written;
- a row without an offset is never merged with a row that has one, and identical lines at
  different offsets stay distinct.
