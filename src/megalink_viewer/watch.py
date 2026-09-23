"""Keeping a local copy of a range's data in step with the live stream.

One event stream is enough for a per-position display. On the current protocol
that stream covers exactly one range, which is as narrow as the feed gets; on
the older one it covers all of a club's ranges, because that is how the older
tree is laid out. Either way the tree is maintained here and views over a single
firing point are rebuilt from it on demand, so only the lane on display is ever
decoded.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .client import MegalinkClient, Source, apply_event
from .models import LaneView, RangeInfo


class RangeState:
    """A thread-safe mirror of the subtree a :class:`~.client.Source` names."""

    def __init__(self, source: Source, tree: dict[str, Any] | None = None) -> None:
        self.source = source
        self._tree: dict[str, Any] = tree or {}
        self._lock = threading.Lock()
        #: When the last event was applied, or ``None`` before the first arrives.
        #:
        #: On the monotonic clock, not the wall clock. A Pi has no real-time
        #: clock: it boots with the time it was last shut down, and NTP then
        #: steps it forward by however long it was off -- often days. Measured
        #: on the wall clock, a display that was perfectly live would report
        #: its last update as days old until the next event happened to arrive.
        self.updated_at: float | None = None if tree is None else time.monotonic()

    @property
    def tree(self) -> dict[str, Any]:
        with self._lock:
            return self._tree

    @property
    def connected(self) -> bool:
        return self.updated_at is not None

    def apply(self, event: Any) -> None:
        with self._lock:
            new_tree = apply_event(self._tree, event)
            self._tree = new_tree if isinstance(new_tree, dict) else {}
            self.updated_at = time.monotonic()

    def range_info(self) -> RangeInfo | None:
        return self.source.range_info(self.tree)

    def lanes(self) -> list[str]:
        return self.source.lanes(self.tree)

    def lane_view(self, lane: int | str) -> LaneView:
        return self.source.lane_view(self.tree, lane)

    def age(self, now: float | None = None) -> float | None:
        """Seconds since the last update, or ``None`` if nothing has arrived."""
        if self.updated_at is None:
            return None
        return (now if now is not None else time.monotonic()) - self.updated_at


class RangeWatcher:
    """Streams one range in a background thread, into a :class:`RangeState`."""

    def __init__(self, source: Source, client: MegalinkClient | None = None) -> None:
        self.source = source
        self.client = client or MegalinkClient()
        self.state = RangeState(source)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        for event in self.client.stream_source(self.source, stop=self._stop.is_set):
            if self._stop.is_set():
                return
            self.state.apply(event)

    def start(self) -> RangeState:
        self._thread = threading.Thread(
            target=self._run,
            name=f"megalink-{self.source.host}",
            daemon=True,
        )
        self._thread.start()
        return self.state

    def stop(self) -> None:
        self._stop.set()

    def wait_for_data(self, timeout: float = 30.0, interval: float = 0.05) -> bool:
        """Block until the first snapshot arrives. Returns whether it did."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state.connected:
                return True
            if self._thread is not None and not self._thread.is_alive():
                return False
            time.sleep(interval)
        return self.state.connected

    def __enter__(self) -> RangeState:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def watch_lane(
    host: str,
    range_name: str,
    lane: int | str,
    on_update: Callable[[LaneView, RangeState], None],
    client: MegalinkClient | None = None,
    interval: float = 1.0,
    stop: Callable[[], bool] | None = None,
) -> None:
    """Call *on_update* with one firing point's state, about every *interval*.

    Redrawing on a timer rather than per event keeps the clock ticking on screen
    while nothing is being shot, and coalesces the burst of patches that arrives
    when a range starts a relay.
    """
    client = client or MegalinkClient()
    watcher = RangeWatcher(client.resolve(host, range_name), client)
    state = watcher.start()
    try:
        watcher.wait_for_data(timeout=interval * 30)
        while stop is None or not stop():
            on_update(state.lane_view(lane), state)
            time.sleep(interval)
    finally:
        watcher.stop()
