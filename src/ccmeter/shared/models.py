from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssistantUsage:
    """Normalized assistant message usage parsed from one JSONL line."""

    message_id: str
    session_id: str
    timestamp: str | None
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_5m: int
    cache_write_1h: int
    cache_write_total: int
    is_sidechain: bool
    sidechain_key: str | None
    prompt_id: str | None
    parent_uuid: str | None
    uuid: str | None


@dataclass(frozen=True)
class UserPrompt:
    """User prompt line that starts or identifies a prompt turn."""

    session_id: str
    timestamp: str | None
    uuid: str | None
    is_sidechain: bool
    prompt_id: str | None


@dataclass(frozen=True)
class Ignored:
    """Valid JSONL line that is not relevant for usage accounting."""

    kind: str


@dataclass(frozen=True)
class Unparseable:
    """JSONL line that could not be parsed safely."""

    reason: str


ParsedLine = AssistantUsage | UserPrompt | Ignored | Unparseable


@dataclass(frozen=True)
class ProviderUsage:
    """Usage already normalized by the agent (currently only Codex), one model request.

    `input_tokens` is the NON-cached input, as for Claude; `reasoning_tokens` is
    informational (already included in `output_tokens`).
    """

    provider: str
    message_id: str
    session_id: str
    thread_id: str | None
    turn_id: str | None
    is_sidechain: bool
    timestamp: str
    model: str
    project: str | None
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    service_tier: str | None
