from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from ccmeter.shared.claude_lines import (
    KEPT_LINE_FIELDS,
    KEPT_MESSAGE_FIELDS,
    minimize_line,
    parse_line,
)
from ccmeter.shared.models import AssistantUsage, Ignored, Unparseable, UserPrompt

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8").splitlines()


def test_parse_assistant_usage_with_cache_split_and_ignores_iterations() -> None:
    raw = _fixture_lines("main_session_extract.jsonl")[1]

    parsed = parse_line(raw)

    assert isinstance(parsed, AssistantUsage)
    assert parsed.message_id == "msg_gPu7cuJN9vuFN2yz0ipSpXfu"
    assert parsed.session_id == "60ce41f3-dfbc-472e-8297-629a073f21dd"
    assert parsed.timestamp == "2025-05-08T10:20:25.886Z"
    assert parsed.model == "claude-fable-5"
    assert parsed.input_tokens == 12864
    assert parsed.output_tokens == 6079
    assert parsed.cache_read_tokens == 23938
    assert parsed.cache_write_5m == 0
    assert parsed.cache_write_1h == 7355
    assert parsed.cache_write_total == 7355
    assert parsed.is_sidechain is False
    assert parsed.sidechain_key is None
    assert parsed.prompt_id is None
    assert parsed.parent_uuid == "55c31c66-5607-40fb-867e-56a010789b8f"
    assert parsed.uuid == "c5fb34b2-31e9-4a50-8285-ebb8b5f65692"


def test_parse_user_prompt_when_content_is_text() -> None:
    raw = _fixture_lines("main_session_extract.jsonl")[0]

    parsed = parse_line(raw)

    assert isinstance(parsed, UserPrompt)
    assert parsed.session_id == "60ce41f3-dfbc-472e-8297-629a073f21dd"
    assert parsed.timestamp == "2025-05-08T10:18:50.376Z"
    assert parsed.uuid == "7f222e87-cb93-43f6-85dd-b79ab865aebc"
    assert parsed.is_sidechain is False
    assert parsed.prompt_id == "7321d145-3376-4db0-8cc4-9b00be261255"


def test_parse_user_tool_result_as_ignored() -> None:
    raw = _fixture_lines("main_session_extract.jsonl")[2]

    parsed = parse_line(raw)

    assert isinstance(parsed, Ignored)
    assert parsed.kind == "user_tool_result"


def test_parse_subagent_assistant_uses_agent_id_as_sidechain_key() -> None:
    raw = _fixture_lines("subagent_extract.jsonl")[1]

    parsed = parse_line(raw)

    assert isinstance(parsed, AssistantUsage)
    assert parsed.is_sidechain is True
    assert parsed.sidechain_key == "a1097c2b70a1dff39"
    assert parsed.model == "claude-opus-4-8"
    assert parsed.cache_write_5m == 32396
    assert parsed.cache_write_1h == 0
    assert parsed.cache_write_total == 32396


def test_parse_synthetic_usage_as_assistant_usage() -> None:
    raw = _fixture_lines("synthetic_extract.jsonl")[0]

    parsed = parse_line(raw)

    assert isinstance(parsed, AssistantUsage)
    assert parsed.model == "<synthetic>"
    assert parsed.input_tokens == 0
    assert parsed.output_tokens == 0


@pytest.mark.parametrize("raw", _fixture_lines("ignored_types_extract.jsonl"))
def test_parse_known_ignored_types(raw: str) -> None:
    parsed = parse_line(raw)

    assert isinstance(parsed, Ignored)
    assert parsed.kind == json.loads(raw)["type"]


@pytest.mark.parametrize("raw", _fixture_lines("workflow_journal_extract.jsonl"))
def test_parse_workflow_journal_types_as_ignored(raw: str) -> None:
    parsed = parse_line(raw)

    assert isinstance(parsed, Ignored)
    assert parsed.kind == json.loads(raw)["type"]


def test_parse_unknown_type_as_ignored() -> None:
    raw = '{"type":"future-type","sessionId":"s1"}'

    parsed = parse_line(raw)

    assert isinstance(parsed, Ignored)
    assert parsed.kind == "unknown:future-type"


def test_parse_invalid_json_as_unparseable() -> None:
    parsed = parse_line('{"type":"assistant"')

    assert isinstance(parsed, Unparseable)
    assert "json" in parsed.reason.lower()


def test_parse_missing_usage_defaults_to_zeroes() -> None:
    obj = json.loads(_fixture_lines("subagent_extract.jsonl")[1])
    del obj["message"]["usage"]["cache_creation"]
    del obj["message"]["usage"]["cache_creation_input_tokens"]

    parsed = parse_line(json.dumps(obj))

    assert isinstance(parsed, AssistantUsage)
    assert parsed.cache_write_5m == 0
    assert parsed.cache_write_1h == 0
    assert parsed.cache_write_total == 0


def test_parse_assistant_without_message_id_falls_back_to_uuid() -> None:
    obj = json.loads(_fixture_lines("main_session_extract.jsonl")[1])
    del obj["message"]["id"]

    parsed = parse_line(json.dumps(obj))

    assert isinstance(parsed, AssistantUsage)
    assert parsed.message_id == "c5fb34b2-31e9-4a50-8285-ebb8b5f65692"


def _all_fixture_lines() -> list[str]:
    return [
        line
        for path in sorted(FIXTURES_DIR.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_minimized_lines_parse_exactly_like_the_originals() -> None:
    lines = _all_fixture_lines()
    assert len(lines) > 20

    for line in lines:
        original = parse_line(line)
        minimized = parse_line(minimize_line(line))
        if isinstance(original, Unparseable):
            assert isinstance(minimized, Unparseable)
        else:
            assert minimized == original


def test_minimized_lines_keep_only_accounting_fields() -> None:
    for line in _all_fixture_lines():
        reduced = minimize_line(line)
        if not reduced:
            continue
        data = cast(dict[str, Any], json.loads(reduced))
        assert set(data) <= set(KEPT_LINE_FIELDS) | {"message"}
        message: object = data.get("message")
        if isinstance(message, dict):
            fields = cast(dict[str, Any], message)
            assert set(fields) <= set(KEPT_MESSAGE_FIELDS) | {"content"}
            content: object = fields.get("content")
            if isinstance(content, list):
                blocks = cast(list[dict[str, Any]], content)
                assert all(set(block) <= {"type"} for block in blocks)
            elif content is not None:
                assert content == ""


def _non_numbers_in(value: object) -> list[object]:
    """Every leaf of a JSON value that is not a number (strings, booleans, nulls)."""
    if isinstance(value, dict):
        items = cast(dict[str, object], value).values()
        return [leaf for item in items for leaf in _non_numbers_in(item)]
    if isinstance(value, list):
        return [leaf for item in cast(list[object], value) for leaf in _non_numbers_in(item)]
    if isinstance(value, int | float) and not isinstance(value, bool):
        return []
    return [value]


def test_minimized_usage_carries_numbers_only() -> None:
    lines = [line for line in _all_fixture_lines() if '"usage"' in line]
    with_other_leaves = [
        line
        for line in lines
        if _non_numbers_in(cast(dict[str, Any], json.loads(line))["message"]["usage"])
    ]
    assert with_other_leaves, "fixtures with strings or nulls in usage"

    for line in lines:
        message = cast(dict[str, Any], json.loads(minimize_line(line))["message"])
        assert _non_numbers_in(message["usage"]) == []
        assert parse_line(minimize_line(line)) == parse_line(line)


def test_minimize_drops_text_everywhere_but_keeps_the_prompt_shape() -> None:
    prompt = json.dumps(
        {
            "type": "user",
            "sessionId": "s",
            "uuid": "u",
            "cwd": "/home/someone/secret-project",
            "message": {"role": "user", "content": "my secret prompt"},
        }
    )
    tool_result = json.dumps(
        {
            "type": "user",
            "sessionId": "s",
            "toolUseResult": {"stdout": "secret output"},
            "message": {"content": [{"type": "tool_result", "content": "secret output"}]},
        }
    )

    assert "secret" not in minimize_line(prompt)
    assert "secret" not in minimize_line(tool_result)
    assert isinstance(parse_line(minimize_line(prompt)), UserPrompt)
    assert parse_line(minimize_line(tool_result)) == Ignored(kind="user_tool_result")


def test_minimize_turns_invalid_json_and_non_objects_into_empty_lines() -> None:
    assert minimize_line('{"type": "assistant"') == ""
    assert minimize_line('["a", "b"]') == ""
    assert minimize_line("not json at all") == ""
    assert isinstance(parse_line(minimize_line("not json")), Unparseable)


def test_minimize_keeps_non_object_messages_without_text() -> None:
    line = json.dumps({"type": "user", "sessionId": "s", "message": "some text"})

    assert json.loads(minimize_line(line)) == {"type": "user", "sessionId": "s", "message": ""}
    assert parse_line(minimize_line(line)) == parse_line(line)
