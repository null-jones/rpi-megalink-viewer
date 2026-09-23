"""Showing the scores in a web browser instead of drawing them.

A third display mode, beside the window and the console: put Megalink's own
page on the screen full-screen and let it do the drawing. The rest of the
machine is unchanged -- the configuration page, the beacon and the fleet
dashboard all run exactly as they do in the other modes, so a display in this
mode is still discovered, still managed from any other display, and still
switched to another firing point from the dashboard.

Two reasons to want it. The page is always current, including any discipline
this package has not been taught to draw; and pointing it at a local
MLLiveArena server instead of live.megalink.no is a URL away, which is the
shape a range with no internet will eventually need.

Bear in mind that Chromium and a live single-page app want a few hundred
megabytes, which a Pi Zero 2 W does not have spare once it is running an X
server. This suits a Pi 4.

Nothing here may ever put something on the screen that has to be clicked. These
displays have no keyboard and no mouse, so a dialogue waiting for an answer is
not an inconvenience -- it is the screen, permanently, until somebody drives out
to it. That is what most of :func:`kiosk_command` is for.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any

from .config import Config

#: Where Megalink's own display lives.
LIVE_URL = "https://live.megalink.no"

#: Browsers to look for, in the order they are preferred. Chromium first
#: because it is what Raspberry Pi OS ships and what its kiosk switches suit.
BROWSERS = (
    "chromium-browser",
    "chromium",
    "firefox-esr",
    "firefox",
    "epiphany-browser",
)

#: A browser that dies faster than this is failing rather than being closed,
#: so the supervisor waits before trying again instead of spinning.
MIN_RUN_SECONDS = 5.0
RESTART_DELAY = 3.0


def feed_url(config: Config, web_port: int = 0, lane: str | None = None) -> str:
    """The page this display should show.

    An explicit ``display.url`` wins, which is how a range points its screens at
    a local server rather than at Megalink's. Otherwise it is built from the
    club, range and firing point, in the form Megalink's own links use.

    A display that has not been told what to show gets the page saying how to
    set it up, which is more use on a screen than an empty range list.
    """
    override = (config.display.url or "").strip()
    if override:
        return override
    host = (config.host or "").strip()
    range_key = (config.range or "").strip()
    if not host or not range_key:
        port = web_port or config.web.port or 8080
        # The set-up instructions rather than the settings form: this is a
        # screen with nobody at a keyboard in front of it.
        return f"http://localhost:{port}/setup"
    url = f"{LIVE_URL}/#!/{host}/{range_key}"
    wanted = (config.lane if lane is None else lane) or ""
    wanted = str(wanted).strip()
    if wanted:
        url = f"{url}/{wanted}"
    return url


def find_browser(candidates: Sequence[str] = BROWSERS) -> str | None:
    """The first browser installed, or ``None``."""
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None


def kiosk_command(
    browser: str,
    url: str,
    profile: str = "/tmp/megalink-browser",
    screen: Any = None,
) -> list[str]:
    """The command line for a browser showing one page and nothing else.

    Chromium needs telling several times over that this is not a desktop: no
    first-run dialogue, no infobars, no "restore pages?" after a power cut --
    which on a screen with no keyboard would sit there until someone drove out
    to dismiss it. The profile goes under /tmp so it is thrown away on reboot
    and never wears out the SD card.
    """
    name = os.path.basename(browser).lower()
    if "firefox" in name:
        return [browser, "--kiosk", "--private-window", url]
    if "epiphany" in name:
        return [browser, "--application-mode", url]
    placement = []
    if screen is not None:
        # Placed on one output rather than left to --kiosk, which fills the
        # whole X screen. Across two HDMI sockets that is both monitors, so
        # both firing points would pile onto one and leave the other blank.
        placement = [
            f"--window-position={screen.x},{screen.y}",
            f"--window-size={screen.width},{screen.height}",
        ]
    return [
        browser,
        *(["--start-fullscreen"] if screen is not None else ["--kiosk"]),
        *placement,
        "--incognito",
        "--noerrdialogs",
        "--disable-infobars",
        "--no-first-run",
        "--fast",
        "--fast-start",
        # Everything below exists to stop the browser asking a question. A
        # display with no keyboard and no mouse cannot answer one, so a prompt
        # does not interrupt the screen, it *becomes* the screen.
        #
        # After an unclean shutdown -- which, for a display switched off at the
        # wall, is every shutdown -- Chromium offers to restore the session.
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        # The yellow "unsupported command-line flag" infobar. --disable-infobars
        # stopped covering this one; --test-type is what still does.
        "--test-type",
        "--no-default-browser-check",
        # A page may ask for notifications, the camera or a location. Refused
        # outright rather than left sitting in a bubble nobody can dismiss.
        "--deny-permission-prompts",
        "--disable-notifications",
        "--disable-features=Translate,InfiniteSessionRestore,MediaRouter",
        # A score display has nothing to update itself for and no user to ask.
        "--check-for-update-interval=31536000",
        "--disable-pinch",
        "--overscroll-history-navigation=0",
        f"--user-data-dir={profile}",
        url,
    ]


class BrowserDisplay:
    """Keeps a browser on screen showing the configured page.

    Restarts it if it dies, and reloads it at a new address when the firing
    point is changed -- from the screen's own configuration page or from the
    fleet dashboard, neither of which knows or cares that this display happens
    to be a browser.
    """

    def __init__(
        self,
        controller: Any,
        should_stop: Callable[[], bool] | None = None,
        spawn: Callable[[list[str]], Any] | None = None,
        browser: str | None = None,
        report: Callable[[str], None] | None = None,
        poll: float = 1.0,
        screen: Any = None,
        lane_source: Callable[[], str] | None = None,
        profile: str = "/tmp/megalink-browser",
    ) -> None:
        self._controller = controller
        self._should_stop = should_stop or (lambda: False)
        self._spawn = spawn or self._default_spawn
        self._browser = browser
        self._report = report or (lambda _message: None)
        self._poll = poll
        self._process: Any = None
        self._url = ""
        self._started_at = 0.0
        #: The output this pane fills, or None for the whole screen.
        self._screen = screen
        self._lane_source = lane_source
        #: Chromium shares one window per profile, so a second pane needs a
        #: profile of its own or it opens a tab in the first one instead.
        self._profile = profile

    @staticmethod
    def _default_spawn(command: list[str]) -> Any:  # pragma: no cover - needs a browser
        return subprocess.Popen(command)

    @property
    def url(self) -> str:
        """The address currently on screen."""
        return self._url

    def wanted_url(self) -> str:
        port = getattr(self._controller.config.web, "port", 0)
        lane = None
        if self._lane_source is not None:
            lane = self._lane_source()
        return feed_url(self._controller.config, web_port=port, lane=lane)

    def start(self, url: str) -> None:
        """Put a page on the screen, replacing whatever is there."""
        self.stop_browser()
        command = kiosk_command(
            self._browser or "", url, profile=self._profile, screen=self._screen
        )
        self._report(f"showing {url}")
        self._process = self._spawn(command)
        self._url = url
        self._started_at = time.monotonic()

    def stop_browser(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        for step in ("terminate", "kill"):
            try:
                getattr(process, step)()
                process.wait(timeout=5)
                return
            except Exception:
                continue

    def alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def tick(self) -> None:
        """One pass of the supervisor: reload on a change, restart on a death."""
        wanted = self.wanted_url()
        if wanted != self._url:
            self.start(wanted)
            return
        if self.alive():
            return
        # A browser that exits almost immediately is failing to start, not being
        # closed. Waiting keeps a broken configuration from becoming a loop that
        # spawns processes as fast as the machine can fork them.
        if time.monotonic() - self._started_at < MIN_RUN_SECONDS:
            self._report("the browser exited immediately; waiting before retrying")
            time.sleep(RESTART_DELAY)
        self.start(wanted)

    def run(self) -> None:
        """Show the page until told to stop."""
        run_panes([self], self._should_stop, self._poll)


def run_panes(
    panes: Sequence[BrowserDisplay],
    should_stop: Callable[[], bool],
    poll: float = 1.0,
) -> None:
    """Keep every pane on screen until told to stop.

    One loop for all of them, so two HDMI outputs cost one process rather than
    two: one feed, one configuration page, one entry in the fleet.
    """
    try:
        while not should_stop():
            for pane in panes:
                pane.tick()
            time.sleep(poll)
    finally:
        for pane in panes:
            pane.stop_browser()
