"""Package boundaries and the `ccmeter` command line."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ccmeter import __version__
from ccmeter.cli import main
from ccmeter.server.db import connect_db, initialize_schema
from ccmeter.server.lock import ProcessLock

# Columns of every table as the 1.0 schema (version 3) defines them. Older servers read the
# same database, so a server can be rolled back without touching the data: keep this list
# additive-only and in sync with the schema.
SCHEMA_V3 = {
    "meta": ["key", "value"],
    "raw_lines": [
        "id",
        "host",
        "file",
        "byte_offset",
        "content",
        "received_ts",
        "parsed",
        "error",
        "content_hash",
    ],
    "sessions": ["session_id", "host", "project", "first_ts", "last_ts", "provider"],
    "messages": [
        "id",
        "message_id",
        "session_id",
        "host",
        "ts",
        "model",
        "prompt_id",
        "prompt_index",
        "is_sidechain",
        "sidechain_key",
        "parent_uuid",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_5m",
        "cache_write_1h",
        "cache_write_total",
        "raw_line_id",
        "provider",
        "reasoning_tokens",
        "service_tier",
        "thread_id",
    ],
    "raw_usage_daily": [
        "date",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "messages",
    ],
}


def test_client_and_shared_code_do_not_load_the_server_stack() -> None:
    """`pip install ccmeter` (no extra) must be enough for the agent."""
    script = (
        "import sys\n"
        "import ccmeter.cli, ccmeter.client.agent, ccmeter.shared.claude_lines\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('fastapi', 'uvicorn', 'starlette', 'pydantic') or m.startswith('ccmeter.server'))\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_database_schema_is_the_stable_version_3(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "ccmeter.db")
    initialize_schema(conn)

    tables = {
        str(row[0]): [str(column[1]) for column in conn.execute(f"PRAGMA table_info({row[0]})")]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }

    assert tables == SCHEMA_V3
    assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "3"


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"ccmeter {__version__}"


def test_agent_init_writes_the_config_and_refuses_to_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "agent.json"
    args = ["agent", "init", "--server", "http://s:8787", "--token", "t", "--config", str(config)]

    assert main([*args, "--host-label", "desk", "--no-codex"]) == 0
    assert main(args) == 2
    assert main([*args, "--force"]) == 0

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["server_url"] == "http://s:8787"
    assert data["api_token"] == "t"
    assert "exists" in capsys.readouterr().err


def test_agent_init_reads_the_token_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token can stay off the command line, where other local users could read it."""
    monkeypatch.setenv("CCMETER_API_TOKEN", " from-env ")
    config = tmp_path / "agent.json"

    assert main(["agent", "init", "--server", "http://s:8787", "--config", str(config)]) == 0

    assert json.loads(config.read_text(encoding="utf-8"))["api_token"] == "from-env"


def test_agent_init_without_any_token_and_no_terminal_explains_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CCMETER_API_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    config = tmp_path / "agent.json"

    code = main(["agent", "init", "--server", "http://s:8787", "--config", str(config)])

    assert code == 2
    assert not config.exists()
    assert "CCMETER_API_TOKEN" in capsys.readouterr().err


def test_agent_init_asks_for_the_token_without_echo_on_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CCMETER_API_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def typed(prompt: str) -> str:
        return "typed-token\n"

    monkeypatch.setattr("getpass.getpass", typed)
    config = tmp_path / "agent.json"

    assert main(["agent", "init", "--server", "http://s:8787", "--config", str(config)]) == 0

    assert json.loads(config.read_text(encoding="utf-8"))["api_token"] == "typed-token"


def test_agent_init_refuses_an_existing_config_before_asking_for_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "agent.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("CCMETER_API_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def no_prompt(prompt: str) -> str:
        raise AssertionError("asked for a token although the config exists")

    monkeypatch.setattr("getpass.getpass", no_prompt)

    assert main(["agent", "init", "--server", "http://s:8787", "--config", str(config)]) == 2
    assert config.read_text(encoding="utf-8") == "{}"


def test_agent_run_without_config_explains_how_to_create_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["agent", "run", "--config", str(tmp_path / "missing.json")])

    assert code == 2
    assert "ccmeter agent init" in capsys.readouterr().err


def test_server_refuses_to_start_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CCMETER_API_TOKEN", raising=False)

    code = main(["server", "run", "--db", str(tmp_path / "ccmeter.db")])

    assert code == 2
    assert "CCMETER_API_TOKEN" in capsys.readouterr().err


def test_minimize_requires_an_explicit_mode(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["server", "minimize", "--db", str(tmp_path / "ccmeter.db")])

    assert exit_info.value.code == 2


def test_maintenance_commands_refuse_to_run_next_to_a_running_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "ccmeter.db"
    with ProcessLock(db_path):
        for command in (["minimize", "--dry-run"], ["reprocess"], ["vacuum", "--yes"]):
            assert main(["server", *command, "--db", str(db_path)]) == 1
    assert "Stop the server first" in capsys.readouterr().err


def test_minimize_dry_run_reports_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "ccmeter.db"
    conn = connect_db(db_path)
    initialize_schema(conn)
    conn.close()

    assert main(["server", "minimize", "--dry-run", "--db", str(db_path)]) == 0

    assert "scanned 0 lines, would rewrite 0" in capsys.readouterr().out


def test_backup_command_copies_the_database(tmp_path: Path) -> None:
    db_path = tmp_path / "ccmeter.db"
    conn = connect_db(db_path)
    initialize_schema(conn)
    conn.close()
    destination = tmp_path / "copy.db"

    assert main(["server", "backup", str(destination), "--db", str(db_path)]) == 0
    assert main(["server", "backup", str(destination), "--db", str(db_path)]) == 2

    copied = sqlite3.connect(destination)
    assert (
        copied.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "3"
    )
    copied.close()
