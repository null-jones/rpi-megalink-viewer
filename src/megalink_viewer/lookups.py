"""What there is to choose from: the clubs streaming now, their ranges, and the
firing points on each.

Both the settings page on a display and the range dashboard fill their lists
from these, so that nobody has to know that Stord PK is ``stord-pk`` or that its
range is ``1-10``. Cached for a few seconds, because opening a dropdown should
not cost a round trip to Megalink every time.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .client import MegalinkError

#: Answers are cached for this long. The live host list changes on the order of
#: minutes, and a dropdown should not put a Pi Zero on the network for every
#: keystroke.
CACHE_SECONDS = 20.0


class Cache:
    """A tiny time-based cache, so browsing the dropdowns stays cheap."""

    def __init__(self, seconds: float = CACHE_SECONDS) -> None:
        self._seconds = seconds
        self._entries: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, produce: Any) -> Any:
        now = time.monotonic()
        with self._lock:
            found = self._entries.get(key)
            if found is not None and now - found[0] < self._seconds:
                return found[1]
        value = produce()
        with self._lock:
            self._entries[key] = (now, value)
        return value


class Lookups:
    """The three lists, from one Megalink client."""

    def __init__(self, client: Any, cache: Cache | None = None) -> None:
        self.client = client
        self.cache = cache if cache is not None else Cache()

    def hosts(self) -> Any:
        def produce() -> Any:
            active = self.client.active()
            return [
                {
                    "host": host,
                    "name": entries[0].host_name if entries else host,
                    "ranges": [{"key": r.key, "name": r.name, "event": r.event} for r in entries],
                }
                for host, entries in sorted(active.items())
            ]

        return self.cache.get("hosts", produce)

    def ranges(self, host: str) -> Any:
        def produce() -> Any:
            return [
                {"key": r.key, "name": r.name, "protocol": r.protocol, "event": r.event}
                for r in self.client.ranges(host)
            ]

        return self.cache.get(f"ranges:{host}", produce)

    def lanes(self, host: str, range_name: str) -> Any:
        def produce() -> Any:
            source = self.client.resolve(host, range_name)
            return self.client.source_lanes(source)

        return self.cache.get(f"lanes:{host}:{range_name}", produce)

    def route(
        self, path: str, query: dict[str, str], host: str = "", range_name: str = ""
    ) -> tuple[int, Any] | None:
        """Answer ``/api/hosts``, ``/api/ranges`` and ``/api/lanes``, or ``None``.

        ``host`` and ``range_name`` are what to fall back on when the query
        leaves them out: a display's own, on its settings page.
        """
        try:
            if path == "/api/hosts":
                return 200, {"hosts": self.hosts()}
            if path == "/api/ranges":
                host = query.get("host") or host
                if not host:
                    raise ValueError("a host is required")
                return 200, {"host": host, "ranges": self.ranges(host)}
            if path == "/api/lanes":
                host = query.get("host") or host
                range_name = query.get("range") or range_name
                if not host:
                    raise ValueError("a host is required")
                return 200, {"lanes": self.lanes(host, range_name)}
        except MegalinkError as exc:
            return 502, {"error": str(exc)}
        return None
