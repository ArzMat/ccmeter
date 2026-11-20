"""Install smoke test: the published wheel works outside the checkout.

Usage (standard library only, run from anywhere):

    python scripts/smoke_install.py --server-env PATH --agent-env PATH

`--server-env` is a virtual environment where `ccmeter[server]` is installed from the wheel,
`--agent-env` one where plain `ccmeter` (no extra) is installed. Checks:

1. `ccmeter local` (server env) serves the dashboard and picks up a transcript line.
2. `ccmeter server run` (server env) + `ccmeter agent init` / `agent run --once` (agent env)
   deliver a line; `ccmeter server run` in the agent env stops with the "install the server
   extra" message.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ASSISTANT_LINE = {
    "type": "assistant",
    "sessionId": "smoke-session",
    "uuid": "smoke-uuid",
    "timestamp": "2026-01-15T10:00:00.000Z",
    "message": {
        "id": "msg_smoke",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "never shipped"}],
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    },
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-env", type=Path, required=True)
    parser.add_argument("--agent-env", type=Path, required=True)
    args = parser.parse_args()
    server_bin = _ccmeter(args.server_env)
    agent_bin = _ccmeter(args.agent_env)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        projects = work / "projects"
        transcript = projects / "smoke-project" / "smoke-session.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps(ASSISTANT_LINE) + "\n", encoding="utf-8")
        _check_local(server_bin, work, projects)
        _check_server_and_agent(server_bin, agent_bin, work, projects)
        _check_agent_env_has_no_server(agent_bin, work)
    print("smoke test passed")
    return 0


def _check_local(server_bin: Path, work: Path, projects: Path) -> None:
    port = _free_port()
    process = subprocess.Popen(
        [
            str(server_bin),
            "local",
            "--port",
            str(port),
            "--db",
            str(work / "local.db"),
            "--claude-dir",
            str(projects),
            "--no-codex",
            "--state",
            str(work / "local-state.json"),
            "--host-label",
            "smoke-box",
        ],
        cwd=work,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        page = _wait_for(lambda: _get(base + "/dashboard/"), "dashboard page")
        assert "<title>ccmeter</title>" in page, "dashboard page content"
        _wait_for(lambda: _summary_with_usage(base), "line delivered by the local agent")
        print("ccmeter local: dashboard served, transcript line counted")
    finally:
        process.terminate()
        process.wait(30)


def _check_server_and_agent(server_bin: Path, agent_bin: Path, work: Path, projects: Path) -> None:
    port = _free_port()
    env = {**os.environ, "CCMETER_API_TOKEN": "smoke-secret-token"}
    process = subprocess.Popen(
        [str(server_bin), "server", "run", "--port", str(port), "--db", str(work / "server.db")],
        cwd=work,
        env=env,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        _wait_for(lambda: _get(base + "/api/health"), "server health")
        config = work / "agent.json"
        _run(
            [
                str(agent_bin),
                "agent",
                "init",
                "--server",
                base,
                "--token",
                "smoke-secret-token",
                "--config",
                str(config),
                "--claude-dir",
                str(projects),
                "--host-label",
                "smoke-agent",
                "--no-codex",
            ],
            work,
        )
        _run(
            [
                str(agent_bin),
                "agent",
                "run",
                "--once",
                "--config",
                str(config),
                "--state",
                str(work / "agent-state.json"),
            ],
            work,
        )
        summary = _summary_with_usage(base)
        assert summary is not None, "agent delivery reached the server"
        hosts = json.loads(_get(base + "/api/health") or "{}")["hosts"]
        assert hosts["smoke-agent"]["last_data"], "per-host delivery time"
        print("ccmeter server run + agent (no extra): line delivered and counted")
    finally:
        process.terminate()
        process.wait(30)


def _check_agent_env_has_no_server(agent_bin: Path, work: Path) -> None:
    result = subprocess.run(
        [str(agent_bin), "server", "run", "--db", str(work / "never.db")],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CCMETER_API_TOKEN": "smoke-secret-token"},
    )
    assert result.returncode == 2, result
    assert "ccmeter[server]" in result.stderr, result.stderr
    print("agent-only install: `server run` explains the missing extra")


def _ccmeter(env: Path) -> Path:
    for candidate in (env / "Scripts" / "ccmeter.exe", env / "bin" / "ccmeter"):
        if candidate.exists():
            return candidate
    raise SystemExit(f"no ccmeter executable in {env}")


def _summary_with_usage(base: str) -> dict[str, object] | None:
    body = _get(base + "/api/summary")
    if body is None:
        return None
    summary = json.loads(body)
    return summary if summary["total_cost_usd"] > 0 else None


def _get(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.read().decode("utf-8")
    except urllib.error.URLError, ConnectionError, TimeoutError:
        return None


def _wait_for[T](probe: Callable[[], T | None], what: str, timeout: float = 60.0) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for: {what}")


def _run(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"{command[1:3]} failed:\n{result.stdout}\n{result.stderr}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    sys.exit(main())
