"""Runs a configured display, and follows the configuration as it changes.

A display bolted to a firing point has no keyboard. Everything about what it
shows -- club, range, firing point -- comes from the configuration file, and
changing that file has to be enough: the screen must follow within a second or
two, without a restart and without anyone walking to the shed.

So this owns the config, notices when it changes on disk, and swaps the feed
subscription underneath the renderer when it does. The renderer just asks for
the current lane view whenever it draws.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from .client import MegalinkClient, MegalinkError, Source
from .config import Config, default_path, load
from .models import LaneView
from .watch import RangeState, RangeWatcher


class Controller:
    """Keeps a feed subscription in step with the configuration.

    :meth:`lane_view` is what a renderer calls; it never raises, because a
    display with a bad configuration should say so on screen rather than die.
    """

    def __init__(
        self,
        config: Config | None = None,
        path: Path | None = None,
        client: MegalinkClient | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.config = config if config is not None else Config()
        self.client = client or MegalinkClient()
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        #: The outputs with a screen plugged in, when the display knows: set by
        #: the display at the start, for the settings page.
        self.screens: list[str] = []
        self._watcher: RangeWatcher | None = None
        self._state: RangeState | None = None
        self._source: Source | None = None
        #: Why the display is not showing anything, in words fit for a screen.
        self.error: str | None = None
        self._stamp: tuple[int, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Bumped whenever the subscription is replaced, so a renderer can tell
        #: that what it is looking at is a different card now.
        self.generation = 0
        #: Until when this display should shout about itself. Twenty screens in
        #: a row look alike; this is how you find the one you are editing.
        self._identify_until = 0.0
        #: Whether a connection attempt is in flight.
        self.connecting = False
        #: Bumped for each attempt, so a slow one that has been superseded knows
        #: to throw its result away rather than install it over a newer one.
        self._attempt = 0

    # -- the feed ----------------------------------------------------------

    @property
    def state(self) -> RangeState | None:
        with self._lock:
            return self._state

    @property
    def source(self) -> Source | None:
        with self._lock:
            return self._source

    def _stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
        self._watcher = None
        self._state = None
        self._source = None

    def apply(self, config: Config) -> None:
        """Point the display at what *config* asks for.

        Resolving a range means talking to Megalink, and that must not happen on
        the caller's thread: a display started before the network is up would
        block here for as long as the request takes, drawing nothing at all. So
        the connection is made in the background and the screen comes up
        immediately, saying it is connecting.

        Only re-subscribes when the club or range actually changed -- moving to
        the next firing point on the same range needs no new connection, and
        tearing one down would blank the screen for no reason.
        """
        config.coerce()
        with self._lock:
            previous = self.config
            self.config = config
            if (
                self._source is not None
                and previous.host == config.host
                and previous.range == config.range
            ):
                return

            self._attempt += 1
            attempt = self._attempt
            self._stop_watcher()
            self.generation += 1
            if not config.configured:
                self.error = "not configured"
                self.connecting = False
                return
            self.error = None
            self.connecting = True

        threading.Thread(
            target=self._connect,
            args=(config, attempt),
            name="megalink-connect",
            daemon=True,
        ).start()

    def _connect(self, config: Config, attempt: int) -> None:
        """Resolve and subscribe, off the caller's thread."""
        try:
            source = self.client.resolve(config.host, config.range)
        except MegalinkError as exc:
            with self._lock:
                if attempt == self._attempt:
                    self.error = str(exc)
                    self.connecting = False
            return
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                if attempt == self._attempt:
                    self.error = f"{type(exc).__name__}: {exc}"
                    self.connecting = False
            return

        watcher = RangeWatcher(source, self.client)
        state = watcher.start()
        with self._lock:
            if attempt != self._attempt:
                # Superseded while we were connecting.
                watcher.stop()
                return
            self._stop_watcher()
            self._watcher = watcher
            self._state = state
            self._source = source
            self.error = None
            self.connecting = False
            self.generation += 1

    def retry(self) -> bool:
        """Try again if a display is configured but not connected.

        A Pi that boots before its Wi-Fi associates fails the first attempt.
        Without this it would sit there showing the failure until somebody
        rewrote the configuration file.
        """
        with self._lock:
            if self._source is not None or self.connecting or not self.config.configured:
                return False
            config = self.config
            self._attempt += 1
            attempt = self._attempt
            self.connecting = True
        threading.Thread(
            target=self._connect,
            args=(config, attempt),
            name="megalink-reconnect",
            daemon=True,
        ).start()
        return True

    def lane_view(self, lane: int | str | None = None) -> LaneView:
        """The current firing point, or an empty view when there is nothing.

        Takes an optional lane so a controller can stand in for a
        :class:`~.watch.RangeState` wherever a renderer expects one; without it
        the configured firing point is used.
        """
        with self._lock:
            state = self._state
            wanted = str(lane) if lane not in (None, "") else (self.config.lane or "?")
        if state is None:
            return LaneView(wanted)
        try:
            return state.lane_view(wanted)
        except Exception:  # pragma: no cover - defensive
            return LaneView(wanted)

    def set_lane(self, lane: int | str) -> None:
        """Move to another firing point and remember it.

        Used when someone changes the lane at the screen itself; persisting it
        means the display comes back to the same firing point after a reboot.
        """
        self.write(self.config.replace(lane=str(lane)))

    def age(self) -> float | None:
        state = self.state
        return state.age() if state is not None else None

    def lanes(self) -> list[str]:
        state = self.state
        return state.lanes() if state is not None else []

    def status(self) -> dict[str, object]:
        """A summary for the web API and the fleet dashboard."""
        with self._lock:
            config = self.config
            source = self._source
            error = self.error
        state = self.state
        view = self.lane_view()
        result = view.result
        return {
            "name": config.name,
            "configured": config.configured,
            "showing": config.describe(),
            "host": config.host,
            "range": source.range_key if source is not None else config.range,
            "lane": config.lane,
            "lane2": config.display.lane2,
            "screens": list(self.screens),
            "protocol": source.protocol if source is not None else None,
            "connected": state is not None and state.connected,
            "age": self.age(),
            "error": error,
            "connecting": self.connecting,
            "lanes": self.lanes(),
            "shooter": view.shooter_name if view.present else "",
            "total": result.total.display() if result is not None else "",
            "shots": result.shot_count if result is not None else 0,
            "range_name": view.range.name if view.range is not None else "",
            "host_name": view.range.host_name if view.range is not None else "",
            "identifying": self.identifying(),
        }

    def logo_path(self) -> Path | None:
        """The picture to show on an unused firing point, if one is set."""
        from .config import find_logo

        return find_logo(self.config, self.path)

    def identify(self, seconds: float = 8.0) -> None:
        """Make this display announce itself on screen for a few seconds."""
        self._identify_until = time.monotonic() + max(1.0, float(seconds))

    def identifying(self) -> bool:
        return time.monotonic() < self._identify_until

    @property
    def identify_until(self) -> float:
        """When the current request to identify ends, on the monotonic clock.

        Changes with every request, so a browser pane can tell a new one from
        the one it is already showing.
        """
        return self._identify_until

    # -- following the file ------------------------------------------------

    def _mtime(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        # Size alongside mtime: two writes inside one filesystem timestamp tick
        # are unlikely to land on the same length as well.
        return (int(stat.st_mtime_ns), int(stat.st_size))

    def reload(self) -> bool:
        """Re-read the file if it changed. Returns whether anything was applied."""
        stamp = self._mtime()
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        try:
            config = load(self.path)
        except Exception as exc:
            with self._lock:
                self.error = f"configuration: {exc}"
            return False
        self.apply(config)
        return True

    def write(self, config: Config) -> None:
        """Persist *config* and adopt it immediately.

        Saving first and applying second would leave a window where the file and
        the screen disagree; doing both here, and recording the file's new stamp,
        also stops :meth:`reload` from redoing the work a moment later.
        """
        from .config import save

        save(config, self.path)
        self._stamp = self._mtime()
        self.apply(config)

    def start(self) -> None:
        """Load the configuration and keep watching the file for changes."""
        self.reload()

        def run() -> None:
            while not self._stop.wait(self._poll_seconds):
                self.reload()
                self.retry()

        self._thread = threading.Thread(target=run, name="megalink-config", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._stop_watcher()

    def wait_for_data(self, timeout: float = 15.0) -> bool:
        """Block briefly so a display opens with a card rather than a placeholder."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state
            if state is not None and state.connected:
                return True
            if not self.config.configured:
                return False
            if self.error and not self.connecting:
                return False
            time.sleep(0.05)
        return False

    def __enter__(self) -> Controller:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def run_terminal(
    controller: Controller,
    write: Callable[[str], None],
    interval: float = 1.0,
    stop: Callable[[], bool] | None = None,
    width: int = 80,
    height: int = 24,
    interactive: bool = False,
) -> None:
    """Render to a stream, for a Pi with no X server.

    Kept here rather than in the CLI so the console display follows the
    configuration in exactly the same way the window does. On a real terminal
    each frame is drawn over the last one; otherwise they are written in
    sequence, which is what a log wants.
    """
    from . import address, network
    from .render import CLEAR_SCREEN, HIDE_CURSOR, SHOW_CURSOR, compose, frame, render_setup

    if interactive:
        write(HIDE_CURSOR + CLEAR_SCREEN)
    reach, reach_at, spot, status = None, float("-inf"), None, {}
    try:
        while stop is None or not stop():
            config = controller.config
            # Where to set it up, looked up now and then: it runs a command, and
            # an address does not change from one frame to the next.
            if time.monotonic() - reach_at >= 5.0:
                reach_at = time.monotonic()
                status = network.read_status() or {}
                spot = status.get("hotspot") if status.get("mode") == "hotspot" else None
                if not config.configured or spot:
                    reach = address.find(config.web.port if config.web.enabled else 0)
            if not config.configured or spot:
                lines = render_setup(
                    reach,
                    width,
                    height,
                    config.name,
                    interactive,
                    hotspot=spot,
                    network_status=status,
                )
                write(compose(lines, height, interactive))
                time.sleep(interval)
                continue
            note = controller.error or (
                "connecting to Megalink Live…" if controller.connecting else ""
            )
            write(
                frame(
                    controller.lane_view(),
                    width=width,
                    height=height,
                    age=controller.age(),
                    interactive=interactive,
                    extra=note,
                )
            )
            time.sleep(interval)
    finally:
        if interactive:
            write(SHOW_CURSOR + "\n")
