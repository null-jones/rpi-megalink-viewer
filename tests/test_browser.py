"""Showing the scores in a browser instead of drawing them."""

from __future__ import annotations

import pytest

from megalink_viewer import browser
from megalink_viewer.config import Config, ConfigError


class FakeProcess:
    """Stands in for a browser, so none of this needs one installed."""

    def __init__(self, command):
        self.command = command
        self.returncode = None
        self.signals = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.signals.append("terminate")
        self.returncode = 0

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode

    def die(self, code=1):
        self.returncode = code


class FakeController:
    def __init__(self, config):
        self.config = config


class TestFeedUrl:
    """Where a browser-mode display points."""

    def config(self, **kwargs):
        return Config(**kwargs)

    def test_it_builds_megalinks_own_address(self):
        config = self.config(host="stord-pk", range="1-10", lane="9")
        assert browser.feed_url(config) == "https://live.megalink.no/#!/stord-pk/1-10/9"

    def test_a_firing_point_is_optional(self):
        config = self.config(host="stord-pk", range="1-10")
        assert browser.feed_url(config) == "https://live.megalink.no/#!/stord-pk/1-10"

    def test_several_firing_points_come_through(self):
        # Megalink's own links take a comma-separated list.
        config = self.config(host="usa-shooting", range="lower-range-comp-1", lane="36,37,38")
        assert browser.feed_url(config).endswith("/lower-range-comp-1/36,37,38")

    def test_an_explicit_address_wins(self):
        # How a range points its screens at a local server instead.
        config = self.config(host="stord-pk", range="1-10", lane="9")
        config.display.url = "http://10.0.0.5:8080/#!/local/range"
        assert browser.feed_url(config) == "http://10.0.0.5:8080/#!/local/range"

    def test_an_unconfigured_display_shows_its_own_setup_page(self):
        # More use on a screen than an empty range list.
        config = self.config()
        config.web.port = 8080
        assert browser.feed_url(config) == "http://localhost:8080/"

    def test_the_bound_port_is_preferred_over_the_configured_one(self):
        # web.port 0 means "ask the OS"; the real one is only known at runtime.
        config = self.config()
        config.web.port = 0
        assert browser.feed_url(config, web_port=51234) == "http://localhost:51234/"

    def test_an_address_must_be_http(self):
        config = self.config()
        config.display.url = "file:///etc/passwd"
        with pytest.raises(ConfigError):
            config.display.validate()

    def test_browser_is_a_mode(self):
        config = self.config(host="a", range="b")
        config.display.mode = "browser"
        config.display.validate()


class TestKioskCommand:
    """One page, full screen, and nothing that expects a keyboard."""

    def chromium(self, url="http://x/"):
        return browser.kiosk_command("/usr/bin/chromium-browser", url)

    def test_chromium_is_put_into_kiosk_mode(self):
        assert "--kiosk" in self.chromium()

    def test_the_address_is_the_last_argument(self):
        assert self.chromium("http://example/")[-1] == "http://example/"

    def test_the_crash_bubble_is_suppressed(self):
        # A display yanked from the wall crashes every time it is switched off.
        # The "restore pages?" bubble would sit there until somebody drove out.
        command = self.chromium()
        assert "--disable-session-crashed-bubble" in command
        assert "--hide-crash-restore-bubble" in command

    def test_the_profile_is_thrown_away(self):
        # Under /tmp, so it never wears out the SD card.
        profile = [a for a in self.chromium() if a.startswith("--user-data-dir=")]
        assert profile == ["--user-data-dir=/tmp/megalink-browser"]

    def test_firefox_gets_its_own_switches(self):
        command = browser.kiosk_command("/usr/bin/firefox-esr", "http://x/")
        assert command[:2] == ["/usr/bin/firefox-esr", "--kiosk"]
        assert "--incognito" not in command  # a Chromium switch

    def test_an_unknown_browser_is_treated_as_chromium(self):
        assert "--kiosk" in browser.kiosk_command("/opt/thing/browser", "http://x/")

    def test_nothing_can_put_a_question_on_the_screen(self):
        """There is no keyboard and no mouse, so a prompt never goes away.

        It does not interrupt the display, it becomes the display, until
        somebody drives out to the range and dismisses it by hand.
        """
        command = self.chromium()
        for switch in (
            "--disable-session-crashed-bubble",  # "restore pages?" after a power cut
            "--hide-crash-restore-bubble",
            "--test-type",  # the unsupported-flag infobar
            "--no-default-browser-check",
            "--deny-permission-prompts",  # camera, location, notifications
            "--disable-notifications",
            "--noerrdialogs",
            "--no-first-run",
        ):
            assert switch in command, switch


class TestTwoScreens:
    """A Pi 4 or Pi 5 has two HDMI sockets; each shows its own firing point."""

    def screen(self, x=1920):
        from megalink_viewer.outputs import Screen

        return Screen("HDMI-2", 1920, 1080, x, 0)

    def test_a_pane_is_placed_on_its_output(self):
        command = browser.kiosk_command(
            "/usr/bin/chromium-browser", "http://x/", screen=self.screen()
        )
        assert "--window-position=1920,0" in command
        assert "--window-size=1920,1080" in command

    def test_a_placed_pane_does_not_use_kiosk(self):
        # --kiosk fills the whole X screen, which across two sockets is both
        # monitors: both firing points would pile onto one.
        command = browser.kiosk_command(
            "/usr/bin/chromium-browser", "http://x/", screen=self.screen()
        )
        assert "--kiosk" not in command
        assert "--start-fullscreen" in command

    def test_a_single_screen_still_uses_kiosk(self):
        assert "--kiosk" in browser.kiosk_command("/usr/bin/chromium-browser", "http://x/")

    def test_the_prompt_switches_survive_placement(self):
        command = browser.kiosk_command(
            "/usr/bin/chromium-browser", "http://x/", screen=self.screen()
        )
        assert "--deny-permission-prompts" in command
        assert "--disable-session-crashed-bubble" in command

    def test_each_pane_needs_its_own_profile(self):
        # Chromium puts a second window into the first one's session otherwise,
        # and it opens on whichever screen the first is already on.
        one = browser.kiosk_command("/c", "http://x/", profile="/tmp/a", screen=self.screen(0))
        two = browser.kiosk_command("/c", "http://x/", profile="/tmp/b", screen=self.screen())
        assert "--user-data-dir=/tmp/a" in one
        assert "--user-data-dir=/tmp/b" in two

    def test_a_second_lane_gets_its_own_address(self):
        config = Config(host="stord-pk", range="1-10", lane="9")
        assert browser.feed_url(config).endswith("/1-10/9")
        assert browser.feed_url(config, lane="4").endswith("/1-10/4")

    def test_both_panes_are_ticked_by_one_loop(self):
        config = Config(host="stord-pk", range="1-10", lane="9")
        config.display.lane2 = "4"
        controller = FakeController(config)
        spawned = []

        def make(lane_source=None, profile="/tmp/p"):
            return browser.BrowserDisplay(
                controller,
                spawn=lambda cmd: (spawned.append(FakeProcess(cmd)), spawned[-1])[1],
                browser="/usr/bin/chromium-browser",
                lane_source=lane_source,
                profile=profile,
            )

        panes = [
            make(profile="/tmp/p1"),
            make(lane_source=lambda: config.display.lane2, profile="/tmp/p2"),
        ]
        stop = [False]
        browser.run_panes(panes, lambda: stop.pop(0) if stop else True, poll=0)
        shown = [p.command[-1] for p in spawned]
        assert len(shown) == 2
        assert shown[0].endswith("/1-10/9")
        assert shown[1].endswith("/1-10/4")

    def test_stopping_closes_every_pane(self):
        config = Config(host="stord-pk", range="1-10", lane="9")
        controller = FakeController(config)
        spawned = []
        panes = [
            browser.BrowserDisplay(
                controller,
                spawn=lambda cmd: (spawned.append(FakeProcess(cmd)), spawned[-1])[1],
                browser="/c",
                profile=f"/tmp/p{n}",
            )
            for n in (1, 2)
        ]
        stop = [False]
        browser.run_panes(panes, lambda: stop.pop(0) if stop else True, poll=0)
        assert len(spawned) == 2
        assert all("terminate" in p.signals for p in spawned)


class TestFindBrowser:
    def test_it_returns_none_when_nothing_is_installed(self, monkeypatch):
        monkeypatch.setattr(browser.shutil, "which", lambda _name: None)
        assert browser.find_browser() is None

    def test_the_first_installed_one_wins(self, monkeypatch):
        monkeypatch.setattr(
            browser.shutil, "which", lambda name: "/usr/bin/firefox" if "firefox" in name else None
        )
        assert browser.find_browser() == "/usr/bin/firefox"


class TestBrowserDisplay:
    """Keeping the right page on the screen."""

    @pytest.fixture
    def display(self):
        config = Config(host="stord-pk", range="1-10", lane="9")
        config.display.mode = "browser"
        controller = FakeController(config)
        spawned = []

        def spawn(command):
            process = FakeProcess(command)
            spawned.append(process)
            return process

        shown = browser.BrowserDisplay(
            controller, spawn=spawn, browser="/usr/bin/chromium-browser", poll=0
        )
        return shown, controller, spawned

    def test_the_first_tick_opens_the_page(self, display):
        shown, _controller, spawned = display
        shown.tick()
        assert len(spawned) == 1
        assert spawned[0].command[-1].endswith("/stord-pk/1-10/9")

    def test_a_running_browser_is_left_alone(self, display):
        shown, _controller, spawned = display
        shown.tick()
        for _ in range(5):
            shown.tick()
        assert len(spawned) == 1

    def test_changing_the_firing_point_reloads_the_page(self, display):
        shown, controller, spawned = display
        shown.tick()
        controller.config.lane = "4"
        shown.tick()
        assert len(spawned) == 2
        assert spawned[1].command[-1].endswith("/stord-pk/1-10/4")
        # The old one is not left running behind the new.
        assert "terminate" in spawned[0].signals

    def test_a_change_from_the_fleet_dashboard_is_picked_up(self, display):
        # The dashboard writes the controller's config; nothing tells the
        # browser directly, so it has to notice.
        shown, controller, spawned = display
        shown.tick()
        controller.config.host = "mulberry"
        controller.config.range = "mulberry"
        shown.tick()
        assert spawned[-1].command[-1] == "https://live.megalink.no/#!/mulberry/mulberry/9"

    def test_a_browser_that_dies_is_restarted(self, display, monkeypatch):
        shown, _controller, spawned = display
        monkeypatch.setattr(browser.time, "monotonic", lambda: 1e6)
        shown.tick()
        spawned[0].die()
        shown.tick()
        assert len(spawned) == 2

    def test_a_browser_that_dies_at_once_is_not_respawned_in_a_loop(self, display, monkeypatch):
        # A failing browser would otherwise be forked as fast as the machine can.
        shown, _controller, spawned = display
        slept = []
        monkeypatch.setattr(browser.time, "sleep", slept.append)
        shown.tick()
        spawned[0].die()
        shown.tick()
        assert slept == [browser.RESTART_DELAY]

    def test_stopping_closes_the_browser(self, display):
        shown, _controller, spawned = display
        stop = [False]
        shown._should_stop = lambda: stop[0]

        def spawn_then_stop(command):
            stop[0] = True
            process = FakeProcess(command)
            spawned.append(process)
            return process

        shown._spawn = spawn_then_stop
        shown.run()
        assert spawned and "terminate" in spawned[-1].signals

    def test_a_browser_that_ignores_terminate_is_killed(self, display):
        shown, _controller, spawned = display
        shown.tick()
        process = spawned[0]
        process.terminate = lambda: (_ for _ in ()).throw(OSError("stubborn"))
        shown.stop_browser()
        assert "kill" in process.signals
