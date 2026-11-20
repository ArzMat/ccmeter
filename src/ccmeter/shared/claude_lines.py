"""Claude Code transcript lines: parsing for usage accounting and minimization for shipping.

The parser runs on the server; the minimizer runs on the client so that conversation text
never leaves the machine. Both live here because they must agree on the fields that matter.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from ccmeter.shared.models import (
    AssistantUsage,
    Ignored,
    ParsedLine,
    Unparseable,
    UserPrompt,
)

logger = logging.getLogger(__name__)

KNOWN_IGNORED_TYPES = frozenset(
    {
        "attachment",
        "custom-title",
        "last-prompt",
        "mode",
        "queue-operation",
        "result",
        "started",
        "system",
    }
)
TOOL_RESULT_TYPE = "tool_result"
JsonObject = dict[str, Any]

# What `parse_line` reads, and therefore all that `minimize_line` keeps. Keep the two in sync:
# tests check parse_line(minimize_line(x)) == parse_line(x) on every fixture line.
KEPT_LINE_FIELDS = (
    "type",
    "uuid",
    "parentUuid",
    "sessionId",
    "timestamp",
    "isSidechain",
    "agentId",
    "promptId",
)
KEPT_MESSAGE_FIELDS = ("id", "model", "usage")


def minimize_line(raw: str) -> str:
    """Reduce a Claude Code JSONL line to the fields usage accounting needs.

    Conversation text never leaves the machine: prompts, answers, tool inputs and outputs,
    attachments, `cwd` and every other field are dropped. `message.content` keeps only its
    shape (a string becomes "", a list keeps each block's `type`), which is how the parser
    tells a typed prompt from a tool result. Invalid JSON becomes an empty line, which the
    server records as unparseable like the original.

    Args:
        raw: One line of a Claude Code transcript.

    Returns:
        A compact JSON line that parses to the same record as `raw`.
    """
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(decoded, dict):
        return ""
    payload = cast(JsonObject, decoded)
    reduced: JsonObject = {key: payload[key] for key in KEPT_LINE_FIELDS if key in payload}
    if "message" in payload:
        message_object = payload["message"]
        if isinstance(message_object, dict):
            message = cast(JsonObject, message_object)
            kept: JsonObject = {key: message[key] for key in KEPT_MESSAGE_FIELDS if key in message}
            if "usage" in kept:
                # counters only: drop every string (service tier, region, iteration types, and
                # whatever text a future version might add) while keeping the nesting
                kept["usage"] = _numbers_only(kept["usage"])
            if "content" in message:
                kept["content"] = _content_shape(message["content"])
            reduced["message"] = kept
        else:
            reduced["message"] = _empty_like(message_object)
    return json.dumps(reduced, separators=(",", ":"), ensure_ascii=False)


def parse_line(raw: str) -> ParsedLine:
    """Parse one Claude Code JSONL line into a normalized record.

    Args:
        raw: Raw JSONL line from a Claude Code transcript.

    Returns:
        A parsed usage or prompt record, an ignored valid line, or an
        unparseable marker. This function never raises for malformed input.
    """
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Unparseable(reason=f"json decode error: {exc.msg}")

    if not isinstance(decoded, dict):
        return Unparseable(reason="json root is not an object")

    payload = cast(JsonObject, decoded)
    line_type = _optional_str(payload.get("type"))
    if line_type == "assistant":
        return _parse_assistant(payload)
    if line_type == "user":
        return _parse_user(payload)
    if line_type in KNOWN_IGNORED_TYPES:
        return Ignored(kind=line_type)
    return Ignored(kind=f"unknown:{line_type}")


def _parse_assistant(payload: JsonObject) -> AssistantUsage | Ignored:
    message_object = payload.get("message")
    if not isinstance(message_object, dict):
        return Ignored(kind="assistant_without_message")
    message = cast(JsonObject, message_object)

    usage_object = message.get("usage")
    usage = cast(JsonObject, usage_object) if isinstance(usage_object, dict) else {}

    cache_creation_object = usage.get("cache_creation")
    if isinstance(cache_creation_object, dict):
        cache_creation = cast(JsonObject, cache_creation_object)
        cache_write_5m = _int_value(cache_creation.get("ephemeral_5m_input_tokens"))
        cache_write_1h = _int_value(cache_creation.get("ephemeral_1h_input_tokens"))
        cache_write_total = cache_write_5m + cache_write_1h
    else:
        cache_write_5m = 0
        cache_write_1h = 0
        cache_write_total = _int_value(usage.get("cache_creation_input_tokens"))

    _warn_on_multi_iteration_usage(usage)

    uuid = _optional_str(payload.get("uuid"))
    message_id = _optional_str(message.get("id")) or uuid or ""
    is_sidechain = bool(payload.get("isSidechain"))

    return AssistantUsage(
        message_id=message_id,
        session_id=_required_str(payload.get("sessionId")),
        timestamp=_optional_str(payload.get("timestamp")),
        model=_required_str(message.get("model")),
        input_tokens=_int_value(usage.get("input_tokens")),
        output_tokens=_int_value(usage.get("output_tokens")),
        cache_read_tokens=_int_value(usage.get("cache_read_input_tokens")),
        cache_write_5m=cache_write_5m,
        cache_write_1h=cache_write_1h,
        cache_write_total=cache_write_total,
        is_sidechain=is_sidechain,
        sidechain_key=_optional_str(payload.get("agentId")) if is_sidechain else None,
        prompt_id=_optional_str(payload.get("promptId")),
        parent_uuid=_optional_str(payload.get("parentUuid")),
        uuid=uuid,
    )


def _parse_user(payload: JsonObject) -> UserPrompt | Ignored:
    message_object = payload.get("message")
    if not isinstance(message_object, dict):
        return Ignored(kind="user_without_message")
    message = cast(JsonObject, message_object)

    content = message.get("content")
    if isinstance(content, str):
        return UserPrompt(
            session_id=_required_str(payload.get("sessionId")),
            timestamp=_optional_str(payload.get("timestamp")),
            uuid=_optional_str(payload.get("uuid")),
            is_sidechain=bool(payload.get("isSidechain")),
            prompt_id=_optional_str(payload.get("promptId")),
        )

    if _contains_tool_result(content):
        return Ignored(kind="user_tool_result")
    return Ignored(kind="user_non_prompt")


def _contains_tool_result(content: object) -> bool:
    if not isinstance(content, list):
        return False
    content_items = cast(list[object], content)
    for item in content_items:
        if not isinstance(item, dict):
            continue
        item_object = cast(JsonObject, item)
        if item_object.get("type") == TOOL_RESULT_TYPE:
            return True
    return False


def _content_shape(content: object) -> object:
    if not isinstance(content, list):
        return _empty_like(content)
    shape: list[JsonObject] = []
    for item in cast(list[object], content):
        if isinstance(item, dict) and "type" in item:
            shape.append({"type": cast(JsonObject, item)["type"]})
        else:
            shape.append({})
    return shape


def _numbers_only(value: object) -> object:
    """Same nesting with numbers only: strings, booleans and nulls are dropped.

    The parser reads only numeric counters (a missing counter counts as zero), so nothing it
    needs is lost, and no text of any kind can travel inside `usage`.
    """
    if isinstance(value, dict):
        kept: JsonObject = {}
        for key, item in cast(JsonObject, value).items():
            reduced = _numbers_only(item)
            if reduced is not None:
                kept[str(key)] = reduced
        return kept
    if isinstance(value, list):
        items: list[object] = []
        for item in cast(list[object], value):
            reduced = _numbers_only(item)
            if reduced is not None:
                items.append(reduced)
        return items
    return value if _is_number(value) else None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _empty_like(value: object) -> object:
    """Same JSON type without any text: strings, lists and objects become empty."""
    if isinstance(value, str):
        return ""
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    return value


def _warn_on_multi_iteration_usage(usage: JsonObject) -> None:
    iterations = usage.get("iterations")
    if isinstance(iterations, list):
        iteration_items = cast(list[object], iterations)
        if len(iteration_items) > 1:
            logger.warning("assistant usage has multiple iterations: %s", iteration_items)


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _optional_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _required_str(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""
