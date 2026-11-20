"""Response cache that never makes a visitor wait for a rebuild, except the very first one."""

from __future__ import annotations

import gzip
import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

GZIP_LEVEL = 6

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedAsset:
    """A rendered response body, its gzip copy (compressed once) and an ETag."""

    body: bytes
    gzipped: bytes
    etag: str

    @classmethod
    def of(cls, body: bytes) -> CachedAsset:
        digest = hashlib.sha256(body).hexdigest()[:32]
        return cls(body=body, gzipped=gzip.compress(body, GZIP_LEVEL), etag=f'"{digest}"')


class StaleWhileRevalidate:
    """Serve the last rendered asset at once and rebuild it in the background when stale.

    Only the very first request (nothing rendered yet) waits for a build, and if a build is
    already running (startup warm-up) it waits for that one instead of starting another.
    Afterwards a request past the TTL returns the current copy immediately and starts one
    background rebuild; a failed rebuild keeps serving the previous copy.
    """

    def __init__(
        self,
        render: Callable[[], CachedAsset],
        *,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._render = render
        self._ttl = ttl_seconds
        self._clock = clock
        self._cond = threading.Condition()
        self._asset: CachedAsset | None = None
        self._built_at = 0.0
        self._building = False

    def get(self) -> CachedAsset:
        return self.get_entry()[0]

    def get_entry(self) -> tuple[CachedAsset, bool]:
        """The current copy, and whether it is past the TTL with a newer one being built.

        A client told so can ask again in a moment instead of showing old numbers until its
        next regular refresh.
        """
        asset = self._asset
        if asset is None:
            return self._first_build(), False
        if self._clock() - self._built_at >= self._ttl:
            self.refresh_in_background()
            return asset, True
        return asset, False

    def refresh_in_background(self) -> None:
        with self._cond:
            if self._building:
                return
            self._building = True
        threading.Thread(target=self._refresh, name="ccmeter-cache-refresh", daemon=True).start()

    def _first_build(self) -> CachedAsset:
        with self._cond:
            while self._asset is None and self._building:
                self._cond.wait()
            if self._asset is not None:
                return self._asset
            self._building = True
        try:
            asset = self._render()
        except BaseException:
            self._finish(None)
            raise
        self._finish(asset)
        return asset

    def _refresh(self) -> None:
        try:
            asset = self._render()
        except Exception:
            logger.exception("dashboard data refresh failed, serving the previous copy")
            self._finish(None)
            return
        self._finish(asset)

    def _finish(self, asset: CachedAsset | None) -> None:
        with self._cond:
            if asset is not None:
                self._asset = asset
                self._built_at = self._clock()
            self._building = False
            self._cond.notify_all()
