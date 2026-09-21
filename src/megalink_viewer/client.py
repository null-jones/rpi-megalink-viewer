"""Access to the Megalink Live backend.

Megalink Live is a Firebase Realtime Database front end, and there are two
databases, both readable without credentials:

* **arena** (``mllivearena``) holds ``hosts_active`` -- who is streaming and
  which ranges -- and the current, v2 score tree at ``data/<host>/<range>``;
* **live** (``mllive``) holds the older v1 tree at ``data/<host>``, still
  written by some ranges.

Rather than pull in the Firebase SDK, this uses the database's REST interface:
any path plus ``.json`` for a snapshot, and the same URL with an
``Accept: text/event-stream`` header for a push stream. That keeps the package on
the standard library, which is what makes it comfortable on a Pi Zero.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any, Callable

from . import v1, v2
from .models import LaneView, RangeInfo
from .parse import entries, loose_key

#: Database holding the active-host list and the current (v2) score tree.
ARENA_DB = "https://mllivearena-default-rtdb.europe-west1.firebasedatabase.app"
#: Database holding the older (v1) score tree.
LIVE_DB = "https://mllive.firebaseio.com"

USER_AGENT = "megalink-viewer/0.1 (+https://live.megalink.no/)"

DEFAULT_TIMEOUT = 20.0
#: Firebase sends a keep-alive about every 30s; allow for a missed one.
STREAM_READ_TIMEOUT = 90.0


class MegalinkError(RuntimeError):
    """Raised when the backend cannot be read, or holds nothing usable."""


def _request(url: str, stream: bool = False) -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if stream:
        headers["Accept"] = "text/event-stream"
    return urllib.request.Request(url, headers=headers)


def _quote(segment: str) -> str:
    return urllib.parse.quote(str(segment), safe="")


def get_json(base: str, path: str, shallow: bool = False, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Fetch one database path as JSON."""
    url = f"{base.rstrip('/')}/{path.strip('/')}.json"
    if shallow:
        url += "?shallow=true"
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise MegalinkError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise MegalinkError(f"could not reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MegalinkError(f"{url} returned malformed JSON") from exc


# -- streaming -------------------------------------------------------------


class Event:
    """One server-sent event from the database stream."""

    __slots__ = ("data", "name", "path")

    def __init__(self, name: str, path: str, data: Any) -> None:
        self.name = name
        #: Path of the change, relative to the streamed path.
        self.path = path
        self.data = data

    def __repr__(self) -> str:
        return f"Event({self.name!r}, {self.path!r})"


def stream(
    base: str,
    path: str,
    stop: Callable[[], bool] | None = None,
    retry_seconds: float = 2.0,
    max_retry_seconds: float = 30.0,
) -> Iterator[Event]:
    """Yield database events for *path*, reconnecting for as long as it runs.

    Firebase's stream protocol uses two event names: ``put`` replaces the value
    at ``Event.path``, ``patch`` merges into it. ``keep-alive`` is swallowed. On
    ``cancel``/``auth_revoked``, and on any network failure, the connection is
    re-established with a backoff; each reconnection replays the whole state as
    a ``put`` at ``/``, so a consumer that applies events to a tree recovers on
    its own.
    """
    url = f"{base.rstrip('/')}/{path.strip('/')}.json"
    delay = retry_seconds
    while stop is None or not stop():
        try:
            request = _request(url, stream=True)
            with urllib.request.urlopen(request, timeout=STREAM_READ_TIMEOUT) as response:
                delay = retry_seconds
                name = None
                for raw in response:
                    if stop is not None and stop():
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        name = None
                        continue
                    if line.startswith("event: "):
                        name = line[7:].strip()
                    elif line.startswith("data: "):
                        payload = line[6:]
                        if name in ("put", "patch"):
                            try:
                                body = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(body, dict):
                                yield Event(name, str(body.get("path") or "/"), body.get("data"))
                        elif name in ("cancel", "auth_revoked"):
                            break
                        name = None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            pass
        if stop is not None and stop():
            return
        time.sleep(delay)
        delay = min(delay * 2, max_retry_seconds)


def as_mapping(node: Any) -> dict[str, Any]:
    """Coerce a tree node to a mapping keyed by string.

    Firebase serialises an integer-keyed map as a JSON array, and then addresses
    it by index in event paths (``/series/1/shots/6``). Turning arrays into
    mappings on the way in keeps those paths meaningful and, crucially, keeps
    the existing entries -- descending into an array as if it were empty would
    silently drop every series a lane had accumulated. Readers go through
    :func:`~.parse.entries`, which accepts either shape.
    """
    if isinstance(node, dict):
        return dict(node)
    if isinstance(node, list):
        return {str(i): value for i, value in enumerate(node) if value is not None}
    return {}


def apply_event(tree: Any, event: Event) -> Any:
    """Apply one stream event to a JSON tree and return the new tree.

    Firebase's semantics: ``put`` replaces the node at the event path, ``patch``
    merges the given keys into it, and a ``None`` value deletes. Missing
    intermediate nodes are created. The input tree is left untouched.
    """
    segments = [s for s in event.path.split("/") if s]

    if not segments:
        if event.name == "patch" and isinstance(event.data, dict):
            merged = as_mapping(tree)
            for key, value in event.data.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            return merged
        return event.data

    root = as_mapping(tree)
    node = root
    for segment in segments[:-1]:
        child = as_mapping(node.get(segment))
        node[segment] = child
        node = child

    leaf = segments[-1]
    if event.name == "patch":
        if isinstance(event.data, dict):
            merged = as_mapping(node.get(leaf))
            for key, value in event.data.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            node[leaf] = merged
        elif event.data is not None:
            node[leaf] = event.data
    elif event.data is None:
        node.pop(leaf, None)
    else:
        node[leaf] = event.data
    return root


# -- discovery -------------------------------------------------------------


class ActiveRange:
    """One range a club is currently streaming, as ``hosts_active`` lists it."""

    __slots__ = ("event", "host", "host_name", "key", "name")

    def __init__(self, host: str, key: str, name: str, host_name: str, event: str) -> None:
        self.host = host
        self.key = key
        self.name = name
        self.host_name = host_name
        self.event = event

    def __repr__(self) -> str:
        return f"ActiveRange({self.host!r}, {self.key!r}, {self.name!r})"


def parse_active_hosts(data: Any) -> dict[str, list[ActiveRange]]:
    """Decode ``hosts_active`` into ranges per host.

    Each host maps to its live ranges, keyed by the same range key the score
    tree uses, with the club's and range's display names alongside.
    """
    hosts: dict[str, list[ActiveRange]] = {}
    for host, value in entries(data):
        found = []
        for key, meta in entries(value):
            meta = meta if isinstance(meta, dict) else {}
            found.append(
                ActiveRange(
                    host=host,
                    key=key,
                    name=str(meta.get("rangeName") or key),
                    host_name=str(meta.get("hostName") or host),
                    event=str(meta.get("eventName") or ""),
                )
            )
        found.sort(key=lambda r: r.name.lower())
        hosts[host] = found
    return hosts


def match_range(candidates: list, wanted: str):
    """Pick the range *wanted* names, or the only one if it names nothing.

    Tried in order: exact key, exact display name, then substring -- all on
    :func:`~.parse.loose_key` so that punctuation differences between a Live URL
    slug, a database key and a display name do not matter. Works on anything
    with ``.key`` and ``.name``, which both :class:`ActiveRange` and
    :class:`~.models.RangeInfo` have.
    """
    if not candidates:
        return None
    target = loose_key(wanted)
    if not target:
        return candidates[0]
    for test in (
        lambda c: loose_key(c.key) == target,
        lambda c: loose_key(c.name) == target,
        lambda c: target in loose_key(c.key) or target in loose_key(c.name),
    ):
        for candidate in candidates:
            if test(candidate):
                return candidate
    return None


# -- a resolved place to read from ----------------------------------------


class Source:
    """A resolved range: which database, which path, and which protocol.

    Holding these together is what lets the rest of the package treat the two
    feed generations alike -- the caller streams :attr:`path` and hands the tree
    back to :meth:`lane_view`.
    """

    __slots__ = ("base", "host", "path", "protocol", "range_key")

    def __init__(self, protocol: int, base: str, path: str, host: str, range_key: str) -> None:
        self.protocol = protocol
        self.base = base
        self.path = path
        self.host = host
        self.range_key = range_key

    @property
    def _adapter(self):
        return v2 if self.protocol == 2 else v1

    def ranges(self, tree: dict[str, Any]) -> list[RangeInfo]:
        if self.protocol == 2:
            return v2.ranges(tree, self.range_key)
        return v1.ranges(tree)

    def range_info(self, tree: dict[str, Any]) -> RangeInfo | None:
        for info in self.ranges(tree):
            if self.protocol == 2 or info.key == self.range_key:
                return info
        return None

    def lanes(self, tree: dict[str, Any]) -> list[str]:
        return self._adapter.lanes(tree, self.range_key)

    def lane_view(self, tree: dict[str, Any], lane: int | str) -> LaneView:
        return self._adapter.lane_view(tree, self.range_key, lane)

    def __repr__(self) -> str:
        return f"Source(v{self.protocol}, {self.host!r}/{self.range_key!r})"


class MegalinkClient:
    """Read-only client for the Megalink Live databases."""

    def __init__(
        self,
        arena_db: str = ARENA_DB,
        live_db: str = LIVE_DB,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.arena_db = arena_db
        self.live_db = live_db
        self.timeout = timeout

    # -- discovery ---------------------------------------------------------

    def active(self) -> dict[str, list[ActiveRange]]:
        """Every club currently streaming, with its live ranges."""
        return parse_active_hosts(get_json(self.arena_db, "hosts_active", timeout=self.timeout))

    def active_hosts(self) -> list[str]:
        """Host slugs currently streaming, sorted."""
        return sorted(self.active())

    def host_name(self, host: str) -> str:
        """A club's display name, falling back to its slug."""
        for ranges_ in self.active().get(host, []):
            if ranges_.host_name:
                return ranges_.host_name
        data = get_json(self.live_db, f"hosts/{_quote(host)}", timeout=self.timeout)
        if isinstance(data, dict) and data.get("name"):
            return str(data["name"])
        return host

    def ranges(self, host: str) -> list[RangeInfo]:
        """Every range a club is publishing, across both protocols."""
        found = [
            RangeInfo(
                key=entry.key,
                name=entry.name,
                host_name=entry.host_name,
                event=entry.event,
                protocol=2,
            )
            for entry in self.active().get(host, [])
        ]
        seen = {loose_key(r.key) for r in found}
        try:
            legacy = v1.ranges(self.snapshot_v1(host))
        except MegalinkError:
            legacy = []
        found.extend(r for r in legacy if loose_key(r.key) not in seen)
        return found

    # -- resolving ---------------------------------------------------------

    def resolve(self, host: str, wanted: str = "") -> Source:
        """Find where to read *host*'s *wanted* range from.

        Prefers the current v2 tree and falls back to the older v1 one. *wanted*
        may be the range key, its display name, or the slug from a Live URL --
        ``live.megalink.no/#!/hanebjerg-skyttecenter/50m`` addresses the range
        keyed ``50m`` there and ``50-m`` in the v1 tree, so matching ignores
        punctuation.
        """
        chosen = match_range(self.active().get(host, []), wanted)
        if chosen is not None:
            return Source(
                protocol=2,
                base=self.arena_db,
                path=f"data/{_quote(host)}/{_quote(chosen.key)}",
                host=host,
                range_key=chosen.key,
            )

        # Nothing in the active list matched; try the older tree.
        legacy = match_range(v1.ranges(self.snapshot_v1(host)), wanted)
        if legacy is not None:
            return Source(
                protocol=1,
                base=self.live_db,
                path=f"data/{_quote(host)}",
                host=host,
                range_key=legacy.key,
            )

        if not wanted:
            raise MegalinkError(f"{host}: no live ranges")
        available = ", ".join(r.key for r in self.ranges(host)) or "none"
        raise MegalinkError(f"{host}: no range matching {wanted!r} (live ranges: {available})")

    # -- snapshots ---------------------------------------------------------

    def snapshot_v1(self, host: str) -> dict[str, Any]:
        """The whole v1 ``data/<host>`` subtree."""
        data = get_json(self.live_db, f"data/{_quote(host)}", timeout=self.timeout)
        return data if isinstance(data, dict) else {}

    def snapshot(self, source: Source) -> dict[str, Any]:
        """The subtree *source* points at."""
        data = get_json(source.base, source.path, timeout=self.timeout)
        return data if isinstance(data, dict) else {}

    def source_lanes(self, source: Source) -> list[str]:
        """Firing points on an already-resolved range."""
        return source.lanes(self.snapshot(source))

    def lane_view(self, host: str, wanted: str, lane: int | str) -> LaneView:
        """A one-off snapshot of a single firing point."""
        source = self.resolve(host, wanted)
        return source.lane_view(self.snapshot(source), lane)

    def stream_source(
        self, source: Source, stop: Callable[[], bool] | None = None
    ) -> Iterator[Event]:
        """Stream every change under *source*."""
        return stream(source.base, source.path, stop=stop)
