"""Agent configuration: where to send usage and which local folders to read."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

CCMETER_HOME = Path.home() / ".ccmeter"
DEFAULT_CONFIG_PATH = CCMETER_HOME / "agent.json"
DEFAULT_STATE_PATH = CCMETER_HOME / "state.json"
DEFAULT_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_CODEX_DIRS = (
    Path.home() / ".codex" / "sessions",
    Path.home() / ".codex" / "archived_sessions",
)
DEFAULT_POLL_SECONDS = 5


@dataclass(frozen=True)
class AgentConfig:
    """Runtime configuration for the ccmeter client agent."""

    server_url: str
    api_token: str
    host_label: str
    claude_projects_dir: Path = DEFAULT_CLAUDE_PROJECTS_DIR
    poll_seconds: int = DEFAULT_POLL_SECONDS
    # empty = Codex disabled; a config file without the key gets DEFAULT_CODEX_DIRS
    codex_dirs: tuple[Path, ...] = ()


def default_host_label() -> str:
    """Name this machine reports as: the hostname, which the user can override."""
    return socket.gethostname() or "localhost"


def load_agent_config(path: Path) -> AgentConfig:
    """Load agent configuration from a JSON file.

    Keys: `server_url`, `api_token`, `host_label` (required), `claude_projects_dir`,
    `poll_seconds`, `codex_dirs` (optional; `[]` disables Codex).
    """
    data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8-sig")))
    raw_codex_dirs = data.get("codex_dirs")
    codex_dirs = (
        tuple(Path(str(item)).expanduser() for item in cast(list[object], raw_codex_dirs))
        if isinstance(raw_codex_dirs, list)
        else DEFAULT_CODEX_DIRS
    )
    return AgentConfig(
        server_url=str(data["server_url"]),
        api_token=str(data["api_token"]),
        host_label=str(data["host_label"]),
        claude_projects_dir=Path(
            str(data.get("claude_projects_dir", DEFAULT_CLAUDE_PROJECTS_DIR))
        ).expanduser(),
        poll_seconds=int(data.get("poll_seconds", DEFAULT_POLL_SECONDS)),
        codex_dirs=codex_dirs,
    )


def write_agent_config(path: Path, cfg: AgentConfig, *, force: bool = False) -> None:
    """Write `cfg` as JSON, refusing to replace an existing file unless `force`.

    Raises:
        FileExistsError: `path` exists and `force` is false.
    """
    if path.exists() and not force:
        raise FileExistsError(path)
    data: dict[str, object] = {
        "server_url": cfg.server_url,
        "api_token": cfg.api_token,
        "host_label": cfg.host_label,
        "claude_projects_dir": cfg.claude_projects_dir.as_posix(),
        "poll_seconds": cfg.poll_seconds,
        "codex_dirs": [item.as_posix() for item in cfg.codex_dirs],
    }
    # the file holds the ingest token: readable by its owner only (no effect on Windows,
    # where the user profile is already private)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    os.chmod(path, 0o600)
