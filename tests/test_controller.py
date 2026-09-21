"""Following the configuration file while a display is running."""

from __future__ import annotations

import time

from megalink_viewer.config import Config, save
from megalink_viewer.controller import Controller


def written(path, **kwargs):
    config = Config(**kwargs)
    save(config, path)
    return config


class TestApplying:
    def test_an_unconfigured_display_says_so(self, config_path, fake_client):
        written(config_path)
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        assert controller.error == "not configured"
        assert controller.state is None
        # And still renders something rather than crashing.
        assert controller.lane_view().shooter_name

    def test_a_configured_display_subscribes(self, config_path, fake_client):
        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        assert controller.error is None
        assert controller.wait_for_data(timeout=5)
        assert controller.lane_view().shooter.name == "Etai Moredehi Bogen"

    def test_an_unreachable_backend_is_reported_not_raised(self, config_path, fake_client):
        written(config_path, host="stord-pk", range="1-10", lane="9")
        fake_client.fail = "could not reach the database"
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        assert controller.error and "could not reach" in controller.error
        assert controller.lane_view().result is None

    def test_an_unknown_range_is_reported(self, config_path, fake_client):
        written(config_path, host="stord-pk", range="300m", lane="1")
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        assert controller.error and "300m" in controller.error


class TestReconfiguring:
    def test_changing_the_lane_keeps_the_connection(self, config_path, fake_client):
        """Moving along the range must not drop and rebuild the subscription."""
        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        controller.wait_for_data(timeout=5)
        before = controller.state
        resolves = len(fake_client.resolved)

        written(config_path, host="stord-pk", range="1-10", lane="3")
        assert controller.reload()
        assert controller.state is before, "same range: the feed should be untouched"
        assert len(fake_client.resolved) == resolves
        assert controller.config.lane == "3"

    def test_changing_the_range_resubscribes(self, config_path, fake_client):
        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        controller.wait_for_data(timeout=5)
        before = controller.state

        written(config_path, host="mulberry", range="mulberry", lane="1")
        assert controller.reload()
        assert controller.state is not before
        assert controller.wait_for_data(timeout=5)
        assert controller.status()["host_name"] == "Mulberry"

    def test_the_generation_marks_a_new_subscription(self, config_path, fake_client):
        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        first = controller.generation
        written(config_path, host="mulberry", range="mulberry", lane="1")
        controller.reload()
        assert controller.generation > first

    def test_an_unchanged_file_is_not_re_read(self, config_path, fake_client):
        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client)
        assert controller.reload()
        assert not controller.reload()

    def test_a_file_that_becomes_invalid_leaves_the_display_running(self, config_path, fake_client):
        """A fat-fingered edit should not blank the screen."""
        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        controller.wait_for_data(timeout=5)
        showing = controller.lane_view().shooter_name

        config_path.write_text("{ broken", encoding="utf-8")
        controller.reload()
        assert controller.error and "configuration" in controller.error
        assert controller.lane_view().shooter_name == showing

    def test_writing_adopts_immediately(self, config_path, fake_client):
        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        controller.write(controller.config.replace(lane="4"))
        assert controller.config.lane == "4"
        assert controller.lane_view().lane == "4"
        # And does not leave the file looking newer than what was applied.
        assert not controller.reload()

    def test_set_lane_persists(self, config_path, fake_client):
        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        controller.set_lane("6")
        from megalink_viewer.config import load

        assert load(config_path).lane == "6"


class TestWatchingTheFile:
    def test_the_background_thread_picks_up_changes(self, config_path, fake_client):
        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client, poll_seconds=0.05)
        controller.start()
        try:
            controller.wait_for_data(timeout=5)
            written(config_path, host="stord-pk", range="1-10", lane="5")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and controller.config.lane != "5":
                time.sleep(0.02)
            assert controller.config.lane == "5"
        finally:
            controller.stop()

    def test_a_deleted_file_does_not_crash_the_watcher(self, config_path, fake_client):
        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        config_path.unlink()
        controller.reload()
        assert not controller.config.configured


class TestStatus:
    def test_status_describes_what_is_on_screen(self, config_path, fake_client):
        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        controller.wait_for_data(timeout=5)
        status = controller.status()
        assert status["configured"] is True
        assert status["connected"] is True
        assert status["protocol"] == 2
        assert status["shooter"] == "Etai Moredehi Bogen"
        assert status["total"] == "84 (1x)"
        assert status["shots"] == 15
        assert status["lane"] == "9"
        assert status["lanes"]
        assert status["error"] is None

    def test_status_of_an_unconfigured_display(self, config_path, fake_client):
        written(config_path)
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        status = controller.status()
        assert status["configured"] is False
        assert status["connected"] is False
        assert status["shooter"] == ""


class TestIdentify:
    def test_identify_expires(self, config_path, fake_client):
        controller = Controller(path=config_path, client=fake_client)
        assert not controller.identifying()
        controller.identify(seconds=1.0)
        assert controller.identifying()
        assert controller.status()["identifying"] is True

    def test_identify_has_a_floor(self, config_path, fake_client):
        controller = Controller(path=config_path, client=fake_client)
        controller.identify(seconds=0.0)
        assert controller.identifying()


class TestTerminalRendering:
    def test_it_renders_the_configured_lane(self, config_path, fake_client):
        from megalink_viewer.controller import run_terminal

        written(config_path, host="stord-pk", range="1-10", lane="9")
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        controller.wait_for_data(timeout=5)

        frames = []
        run_terminal(
            controller,
            frames.append,
            interval=0.0,
            stop=lambda: len(frames) >= 2,
            width=80,
        )
        assert len(frames) == 2
        assert "Etai Moredehi Bogen" in frames[0]

    def test_an_error_is_shown_on_the_console(self, config_path, fake_client):
        from megalink_viewer.controller import run_terminal

        written(config_path, host="stord-pk", range="1-10", lane="9")
        fake_client.fail = "network unreachable"
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        frames = []
        run_terminal(controller, frames.append, interval=0.0, stop=lambda: bool(frames))
        assert "network unreachable" in frames[0]


class TestModeChanges:
    """Switching how the scores are shown has to work from the dashboard.

    The launcher chooses between a window and the console before the process
    starts, so a mode change cannot be applied in place: the display exits and
    systemd starts it again. That is what lets a Pi whose X server is broken be
    switched to console mode remotely.
    """

    def test_the_controller_sees_a_mode_change(self, config_path, fake_client):
        from megalink_viewer.config import Config, save

        config = Config(host="stord-pk", range="1-10", lane="9")
        config.display.mode = "gui"
        save(config, config_path)
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        assert controller.config.display.mode == "gui"

        config.display.mode = "terminal"
        save(config, config_path)
        controller.reload()
        assert controller.config.display.mode == "terminal"

    def test_a_mode_change_does_not_disturb_the_feed(self, config_path, fake_client):
        """Only the renderer changes; the subscription should survive."""
        from megalink_viewer.config import Config, save

        config = Config(host="stord-pk", range="1-10", lane="9")
        save(config, config_path)
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        controller.wait_for_data(timeout=5)
        before = controller.state

        config.display.mode = "terminal"
        save(config, config_path)
        controller.reload()
        assert controller.state is before
