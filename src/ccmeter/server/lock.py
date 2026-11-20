"""Process lock next to the database: one server, or one maintenance command, at a time.

The lock is an operating system lock on `<db>.lock`, not a row in the database: the OS
releases it when the holding process exits for any reason (crash, kill, power loss), and a
database restored from a backup never carries a stale lock.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import IO


class DatabaseLocked(RuntimeError):
    """Another process (a running server or a maintenance command) holds the lock."""


def lock_path_for(db_path: Path) -> Path:
    """`ccmeter.db` -> `ccmeter.db.lock`."""
    return db_path.with_name(db_path.name + ".lock")


class ProcessLock:
    """Exclusive, non-blocking lock held for the lifetime of the process (or the `with`).

    Keep a reference to the object while the lock must be held: once it is garbage
    collected its file is closed and the operating system releases the lock.
    """

    def __init__(self, db_path: Path) -> None:
        self.path = lock_path_for(db_path)
        self._handle: IO[bytes] | None = None

    def acquire(self) -> None:
        """Take the lock or raise `DatabaseLocked` at once (never waits)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            _lock(handle)
        except OSError as exc:
            handle.close()
            raise DatabaseLocked(
                f"{self.path} is held by another ccmeter process "
                "(a running server or a maintenance command)"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        """Release the lock (also released by the OS when the process exits)."""
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
