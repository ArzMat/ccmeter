# Claude Code transcript format: what the fixtures cover

The fixtures in this folder are short extracts shaped like real Claude Code transcripts
(`~/.claude/projects/<project>/<session>.jsonl`). They are synthetic: identifiers,
timestamps, paths and project names were replaced with generated values that keep the links
between lines (uuid/parentUuid, sessionId, agentId, message ids), text content is replaced with
placeholders, and only the structure, models and usage numbers matter.

| Fixture | What it exercises |
|---|---|
| `main_session_extract.jsonl` | a main session: user prompt, tool result, assistant replies with usage |
| `subagent_extract.jsonl` | a subagent file: `isSidechain: true`, `agentId`, same `sessionId` as the main session |
| `synthetic_extract.jsonl` | `<synthetic>` assistant messages that still carry usage |
| `prompt_sequence_extract.jsonl` | prompt counting across user prompts and tool results |
| `duplicate_message_id_extract.jsonl` | the same `message.id` written on several lines with identical usage |
| `ignored_types_extract.jsonl` | line types that carry no usage and are skipped |
| `workflow_journal_extract.jsonl` | a workflow journal (`started` / `result`), no `message`, no usage |
| `claude_golden.json` | expected outputs for the regression test (`tests/server/test_regression_claude.py`) |

## Line types

- Main session files: `queue-operation`, `attachment`, `user`, `last-prompt`, `assistant`,
  `system`, `mode`, `custom-title`.
- Subagent files: `user`, `attachment`, `assistant`.
- Workflow journals: `started`, `result`.

Only `user` and `assistant` lines matter for accounting; the others are skipped.

## Fields ccmeter relies on

Top level: `type`, `uuid`, `parentUuid`, `sessionId`, `timestamp` (ISO UTC with `Z`),
`isSidechain`, `agentId` (subagent files), `promptId` (human prompts and tool results).
Inside `message`: `id`, `model`, `usage` on assistant lines; `content` on user lines.

`message.usage` keys seen: `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, `cache_creation` (with `ephemeral_5m_input_tokens` and
`ephemeral_1h_input_tokens`), `server_tool_use`, `service_tier`, `inference_geo`, `iterations`,
`speed`. The parser must tolerate a missing `cache_creation` and missing accessory fields.

## Rules the fixtures pin down

- **Human prompt vs tool result**: a `user` line whose `message.content` is a string is a human
  prompt; a list containing a `tool_result` item is a tool result and does not count as a prompt.
  Only non-sidechain human prompts increase the prompt counter.
- **Subagents** live in `<project>/<session_id>/subagents/agent-<agent_id>.jsonl`, with
  `isSidechain: true`, `agentId` equal to the file name suffix and the main session's
  `sessionId`; their first user line has `parentUuid: null`. `agentId` is the sidechain key.
- **Sessions span files**: a session is its main file plus its subagent files, so rebuilding
  a session must gather every file with the same `sessionId`.
- **Duplicates**: the same `message.id` can appear on several lines with identical usage; it
  is counted once.
- **Models** look like `claude-fable-5`, `claude-opus-4-8` or `<synthetic>`; prices match them
  as described in `docs/pricing.md`.
