"""The `ccmeter` command: `agent` (client), `server`, and `local` (both on one machine)."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from ccmeter import __version__
from ccmeter.client.config import (
    CCMETER_HOME,
    DEFAULT_CLAUDE_PROJECTS_DIR,
    DEFAULT_CODEX_DIRS,
    DEFAULT_CONFIG_PATH,
    DEFAULT_POLL_SECONDS,
    DEFAULT_STATE_PATH,
    AgentConfig,
    default_host_label,
    load_agent_config,
    write_agent_config,
)

DEFAULT_PORT = 8787
LOCAL_STATE_PATH = CCMETER_HOME / "local-state.json"
EXIT_USAGE = 2
SERVER_EXTRA_HINT = (
    "this command needs the server dependencies. Install them with:\n"
    '  uv tool install "ccmeter[server] @ git+https://github.com/ArzMat/ccmeter"\n'
    '  (or: pip install "ccmeter[server]")'
)

logger = logging.getLogger("ccmeter")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the `ccmeter` command."""
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE
    return int(handler(args))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccmeter",
        description="Token and cost tracker for Claude Code and Codex, across your machines.",
    )
    parser.add_argument("--version", action="version", version=f"ccmeter {__version__}")
    commands = parser.add_subparsers(title="commands", metavar="COMMAND")

    agent = commands.add_parser("agent", help="client: ship this machine's usage to a server")
    agent_commands = agent.add_subparsers(title="agent commands", metavar="ACTION")
    run = agent_commands.add_parser("run", help="watch local transcripts and ship new usage")
    run.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    run.add_argument("--once", action="store_true", help="one pass, then exit")
    run.add_argument("-v", "--verbose", action="store_true")
    run.set_defaults(handler=_agent_run)
    init = agent_commands.add_parser("init", help="write the agent configuration file")
    init.add_argument("--server", required=True, help="server URL, e.g. http://host:8787")
    init.add_argument(
        "--token",
        default=None,
        help="the server's token (default: the CCMETER_API_TOKEN environment variable, else "
        "typed without echo; a value given here is visible in the process list)",
    )
    init.add_argument("--host-label", default=None, help="name of this machine (default: hostname)")
    init.add_argument("--claude-dir", type=Path, default=DEFAULT_CLAUDE_PROJECTS_DIR)
    init.add_argument("--no-codex", action="store_true", help="do not read Codex rollouts")
    init.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.set_defaults(handler=_agent_init)

    server = commands.add_parser("server", help="server: API, database and dashboard")
    server_commands = server.add_subparsers(title="server commands", metavar="ACTION")
    serve = server_commands.add_parser("run", help="start the server")
    serve.add_argument("--host", default="127.0.0.1", help="interface to bind (default: local)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    _add_db_args(serve)
    serve.add_argument("-v", "--verbose", action="store_true")
    serve.set_defaults(handler=_server_run)
    report = server_commands.add_parser("report", help="print a spending report")
    report.add_argument("--days", type=int, default=7)
    _add_db_args(report)
    report.set_defaults(handler=_server_report)
    reprocess = server_commands.add_parser(
        "reprocess", help="rebuild Claude messages from the stored lines (server stopped)"
    )
    _add_db_args(reprocess)
    reprocess.set_defaults(handler=_server_reprocess)
    backup = server_commands.add_parser("backup", help="consistent copy of the database")
    backup.add_argument("destination", type=Path)
    _add_db_args(backup)
    backup.set_defaults(handler=_server_backup)
    minimize = server_commands.add_parser(
        "minimize",
        help="drop conversation text stored by older agents (irreversible, server stopped)",
    )
    _add_db_args(minimize)
    mode = minimize.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="only count what would change")
    mode.add_argument("--yes", action="store_true", help="really rewrite the stored lines")
    minimize.set_defaults(handler=_server_minimize)
    vacuum = server_commands.add_parser(
        "vacuum", help="give the space freed by minimize back to the disk (server stopped)"
    )
    _add_db_args(vacuum)
    vacuum.add_argument("--yes", action="store_true", required=True)
    vacuum.set_defaults(handler=_server_vacuum)

    local = commands.add_parser(
        "local", help="server and agent together on this machine, no configuration"
    )
    local.add_argument("--port", type=int, default=DEFAULT_PORT)
    _add_db_args(local)
    local.add_argument("--host-label", default=None, help="name of this machine")
    local.add_argument("--claude-dir", type=Path, default=DEFAULT_CLAUDE_PROJECTS_DIR)
    local.add_argument("--no-codex", action="store_true", help="do not read Codex rollouts")
    local.add_argument("--state", type=Path, default=LOCAL_STATE_PATH)
    local.add_argument("-v", "--verbose", action="store_true")
    local.set_defaults(handler=_local)
    return parser


def _add_db_args(parser: argparse.ArgumentParser) -> None:
    default_db = Path(os.getenv("CCMETER_DB_PATH", str(CCMETER_HOME / "ccmeter.db")))
    parser.add_argument("--db", type=Path, default=default_db, help=f"default: {default_db}")
    prices = os.getenv("CCMETER_PRICES_PATH")
    parser.add_argument(
        "--prices",
        type=Path,
        default=Path(prices) if prices else None,
        help="price book (default: the one shipped with ccmeter)",
    )


def _agent_run(args: argparse.Namespace) -> int:
    from ccmeter.client.agent import run_agent_loop, scan_and_ship
    from ccmeter.client.state import load_offset_state, save_offset_state

    config_path: Path = args.config
    if not config_path.exists():
        print(
            f"ccmeter: no agent configuration at {config_path}. Create it with:\n"
            "  ccmeter agent init --server http://SERVER:8787",
            file=sys.stderr,
        )
        return EXIT_USAGE
    cfg = load_agent_config(config_path)
    if args.once:
        import httpx

        state = load_offset_state(args.state)
        with httpx.Client() as client:
            result = scan_and_ship(cfg, state, http_client=client)
        if result.ok:
            save_offset_state(args.state, state)
        print(f"sent {result.sent} lines and {result.records_sent} records")
        return 0 if result.ok else 1
    logger.info("agent %s shipping to %s", cfg.host_label, cfg.server_url)
    run_agent_loop(cfg, state_path=args.state)
    return 0


def _agent_token(given: str | None) -> str:
    """The token from --token, else CCMETER_API_TOKEN, else typed at the terminal."""
    if given and given.strip():
        return given.strip()
    token = os.getenv("CCMETER_API_TOKEN", "").strip()
    if token or not sys.stdin.isatty():
        return token
    import getpass

    return getpass.getpass("server token (CCMETER_API_TOKEN): ").strip()


def _agent_init(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    if config_path.exists() and not args.force:
        print(f"ccmeter: {config_path} exists, use --force to replace it", file=sys.stderr)
        return EXIT_USAGE
    token = _agent_token(args.token)
    if not token:
        print(
            "ccmeter: no token. Set CCMETER_API_TOKEN, or run `ccmeter agent init` in a "
            "terminal to type it.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    cfg = AgentConfig(
        server_url=str(args.server),
        api_token=token,
        host_label=args.host_label or default_host_label(),
        claude_projects_dir=args.claude_dir,
        poll_seconds=DEFAULT_POLL_SECONDS,
        codex_dirs=() if args.no_codex else DEFAULT_CODEX_DIRS,
    )
    try:
        write_agent_config(args.config, cfg, force=args.force)
    except FileExistsError:
        print(f"ccmeter: {args.config} exists, use --force to replace it", file=sys.stderr)
        return EXIT_USAGE
    print(f"wrote {args.config} (host label: {cfg.host_label})")
    return 0


def _server_run(args: argparse.Namespace) -> int:
    if not _server_extra_installed():
        return EXIT_USAGE
    import uvicorn

    from ccmeter.server.app import PLACEHOLDER_API_TOKEN, create_app
    from ccmeter.server.lock import DatabaseLocked, ProcessLock

    token = os.getenv("CCMETER_API_TOKEN", "").strip()
    if not token or token == PLACEHOLDER_API_TOKEN:
        print(
            "ccmeter: set CCMETER_API_TOKEN to a random secret first, e.g.\n"
            '  python -c "import secrets; print(secrets.token_hex(32))"',
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.host in {"0.0.0.0", "::", ""}:
        logger.warning(
            "listening on every interface: the dashboard and the read API have no "
            "authentication, expose them only on a private network (LAN, Tailscale, VPN)"
        )
    lock = ProcessLock(args.db)
    try:
        lock.acquire()
    except DatabaseLocked as exc:
        print(f"ccmeter: {exc}", file=sys.stderr)
        return 1
    try:
        app = create_app(
            db_path=args.db, api_token=token, prices_path=_prices(args), warm_dashboard=True
        )
        logger.info("dashboard: http://%s:%s/dashboard/", args.host, args.port)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning", workers=1)
    finally:
        lock.release()
    return 0


def _server_report(args: argparse.Namespace) -> int:
    from ccmeter.server.db import connect_db, initialize_schema
    from ccmeter.server.report import build_report

    conn = connect_db(args.db)
    try:
        initialize_schema(conn)
        print(build_report(conn, prices_path=_prices(args), days=args.days))
    finally:
        conn.close()
    return 0


def _server_reprocess(args: argparse.Namespace) -> int:
    from ccmeter.server.db import connect_db, initialize_schema, reprocess_raw_lines

    with _maintenance_lock(args.db) as locked:
        if not locked:
            return 1
        conn = connect_db(args.db)
        try:
            initialize_schema(conn)
            result = reprocess_raw_lines(conn)
        finally:
            conn.close()
    print(
        f"reprocessed raw={result.raw_lines} parsed={result.parsed} errors={result.errors} "
        f"messages={result.messages} sessions={result.sessions} duplicates={result.duplicates}"
    )
    return 0


def _server_backup(args: argparse.Namespace) -> int:
    from ccmeter.server.db import backup_database

    try:
        backup_database(args.db, args.destination)
    except FileExistsError:
        print(f"ccmeter: {args.destination} exists, choose another name", file=sys.stderr)
        return EXIT_USAGE
    print(f"backup written to {args.destination}")
    return 0


def _server_minimize(args: argparse.Namespace) -> int:
    from ccmeter.server.db import connect_db, initialize_schema, minimize_raw_lines

    with _maintenance_lock(args.db) as locked:
        if not locked:
            return 1
        conn = connect_db(args.db)
        try:
            initialize_schema(conn)
            result = minimize_raw_lines(conn, dry_run=args.dry_run)
        finally:
            conn.close()
    verb = "would rewrite" if args.dry_run else "rewrote"
    print(
        f"scanned {result.scanned} lines, {verb} {result.rewritten}, "
        f"duplicate copies {result.conflicts}, lines without offset {result.null_offsets}, "
        f"text {result.bytes_before} -> {result.bytes_after} bytes"
    )
    if not args.dry_run:
        print("run `ccmeter server vacuum --yes` to give the freed space back to the disk")
    return 0


def _server_vacuum(args: argparse.Namespace) -> int:
    from ccmeter.server.db import connect_db

    with _maintenance_lock(args.db) as locked:
        if not locked:
            return 1
        conn = connect_db(args.db)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
    print("vacuum done")
    return 0


def _local(args: argparse.Namespace) -> int:
    if not _server_extra_installed():
        return EXIT_USAGE
    import uvicorn

    from ccmeter.client.agent import run_agent_loop
    from ccmeter.server.app import create_app
    from ccmeter.server.lock import DatabaseLocked, ProcessLock

    lock = ProcessLock(args.db)
    try:
        lock.acquire()
    except DatabaseLocked as exc:
        print(f"ccmeter: {exc}", file=sys.stderr)
        return 1
    token = secrets.token_urlsafe(32)
    cfg = AgentConfig(
        server_url=f"http://127.0.0.1:{args.port}",
        api_token=token,
        host_label=args.host_label or default_host_label(),
        claude_projects_dir=args.claude_dir,
        codex_dirs=() if args.no_codex else DEFAULT_CODEX_DIRS,
    )
    try:
        app = create_app(
            db_path=args.db, api_token=token, prices_path=_prices(args), warm_dashboard=True
        )
        agent = threading.Thread(
            target=run_agent_loop,
            args=(cfg,),
            kwargs={"state_path": args.state},
            name="ccmeter-agent",
            daemon=True,
        )
        agent.start()
        logger.info("dashboard: http://127.0.0.1:%s/dashboard/", args.port)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", workers=1)
    finally:
        lock.release()
    return 0


def _prices(args: argparse.Namespace) -> Path:
    from ccmeter.server.pricing import BUNDLED_PRICES_PATH

    prices: Path | None = args.prices
    return prices if prices is not None else BUNDLED_PRICES_PATH


def _server_extra_installed() -> bool:
    try:
        import fastapi  # noqa: F401  # pyright: ignore[reportUnusedImport]
        import uvicorn  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        print(f"ccmeter: {SERVER_EXTRA_HINT}", file=sys.stderr)
        return False
    return True


class _maintenance_lock:  # noqa: N801 - used as a context manager, reads like a function
    """Hold the database's process lock; yields False (after printing why) if it is taken."""

    def __init__(self, db_path: Path) -> None:
        from ccmeter.server.lock import ProcessLock

        self._lock = ProcessLock(db_path)

    def __enter__(self) -> bool:
        from ccmeter.server.lock import DatabaseLocked

        try:
            self._lock.acquire()
        except DatabaseLocked as exc:
            print(f"ccmeter: {exc}. Stop the server first.", file=sys.stderr)
            return False
        return True

    def __exit__(self, *exc_info: object) -> None:
        self._lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
