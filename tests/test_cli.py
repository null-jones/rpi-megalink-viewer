"""Argument wiring and command output."""

from __future__ import annotations

import json

import pytest

from megalink_viewer import v2
from megalink_viewer.cli import (
    build_parser,
    cmd_hosts,
    cmd_lanes,
    cmd_ranges,
    cmd_show,
    frame,
    main,
)
from megalink_viewer.client import ARENA_DB, MegalinkClient, Source, parse_active_hosts


class FakeClient(MegalinkClient):
    def __init__(self, active, tree):
        super().__init__()
        self._active = active
        self._tree = tree

    def active(self):
        return parse_active_hosts(self._active)

    def resolve(self, host, wanted=""):
        return Source(2, ARENA_DB, f"data/{host}/1-10", host, "1-10")

    def snapshot(self, source):
        return self._tree

    def snapshot_v1(self, host):
        return {}


@pytest.fixture
def client(v2_pistol):
    return FakeClient(
        {"stord-pk": {"1-10": {"rangeName": "1-10", "hostName": "Stord PK"}}}, v2_pistol
    )


def args(*argv):
    return build_parser().parse_args(list(argv))


class TestParser:
    def test_watch_defaults(self):
        parsed = args("watch", "stord-pk", "1-10", "9")
        assert parsed.host == "stord-pk"
        assert parsed.range == "1-10"
        assert parsed.lane == "9"
        assert parsed.interval == 1.0

    def test_colour_before_the_subcommand(self):
        assert args("--color", "never", "show", "h", "r", "1").color == "never"

    def test_colour_after_the_subcommand(self):
        assert args("show", "h", "r", "1", "--color", "never").color == "never"

    def test_colour_defaults_to_auto(self):
        assert args("show", "h", "r", "1").color == "auto"

    def test_a_command_is_required(self):
        with pytest.raises(SystemExit):
            args()

    def test_unknown_command(self):
        with pytest.raises(SystemExit):
            args("frobnicate")


class TestCommands:
    def test_hosts(self, client, capsys):
        assert cmd_hosts(args("hosts"), client) == 0
        out = capsys.readouterr().out
        assert "stord-pk" in out
        assert "Stord PK" in out

    def test_hosts_quiet_prints_slugs_only(self, client, capsys):
        assert cmd_hosts(args("hosts", "-q"), client) == 0
        assert capsys.readouterr().out.strip() == "stord-pk"

    def test_ranges(self, client, capsys):
        assert cmd_ranges(args("ranges", "stord-pk"), client) == 0
        assert "1-10" in capsys.readouterr().out

    def test_ranges_of_a_quiet_club(self, client, capsys):
        assert cmd_ranges(args("ranges", "nobody"), client) == 1
        assert "no ranges" in capsys.readouterr().err

    def test_lanes(self, client, capsys):
        assert cmd_lanes(args("lanes", "stord-pk", "1-10"), client) == 0
        out = capsys.readouterr().out
        assert "Etai Moredehi Bogen" in out
        assert "84" in out

    def test_show(self, client, capsys):
        assert cmd_show(args("show", "stord-pk", "1-10", "9", "--width", "80"), client) == 0
        out = capsys.readouterr().out
        assert "LANE 9" in out
        assert "TOTAL" in out

    def test_show_has_no_ansi_when_colour_is_off(self, client, capsys):
        cmd_show(args("show", "stord-pk", "1-10", "9", "--color", "never", "--width", "80"), client)
        assert "\x1b[" not in capsys.readouterr().out


class TestMain:
    def test_version(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0
        assert "megalink" in capsys.readouterr().out

    def test_backend_errors_exit_with_a_message(self, capsys, monkeypatch):
        def boom(self):
            from megalink_viewer.client import MegalinkError

            raise MegalinkError("could not reach the database")

        monkeypatch.setattr(MegalinkClient, "active", boom)
        assert main(["hosts"]) == 2
        assert "could not reach" in capsys.readouterr().err


class TestLaneSelection:
    def test_every_lane_the_range_lists_can_be_shown(self, client, v2_pistol, capsys):
        for lane in v2.lanes(v2_pistol):
            assert cmd_show(args("show", "stord-pk", "1-10", lane, "--width", "80"), client) == 0
            assert f"LANE {lane}" in capsys.readouterr().out


class TestFrame:
    """Terminal control belongs to a terminal, not to a pipe."""

    def test_interactive_frame_repositions_the_cursor(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        out = frame(view, 80, 24, 0.0, color=False, interactive=True)
        assert out.startswith("\x1b[H")
        assert "\x1b[K" in out
        assert out.endswith("\x1b[J")

    def test_interactive_frame_fills_the_height(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        out = frame(view, 80, 24, 0.0, color=False, interactive=True)
        assert out.count("\n") == 23

    def test_piped_frame_has_no_escape_sequences(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        out = frame(view, 80, 24, 0.0, color=False, interactive=False)
        assert "\x1b" not in out
        assert out.endswith("\n\n")

    def test_piped_frame_still_honours_colour(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        out = frame(view, 80, 24, 0.0, color=True, interactive=False)
        assert "\x1b[" in out


class TestNewCommandParsing:
    def test_display_takes_no_required_arguments(self):
        """The systemd unit runs `megalink display` and nothing else."""
        parsed = args("display")
        assert parsed.config is None

    def test_display_accepts_a_config_path(self):
        assert args("display", "--config", "/etc/megalink/display.json").config == (
            "/etc/megalink/display.json"
        )

    def test_fleet_defaults_to_loopback(self):
        parsed = args("fleet")
        assert parsed.bind == "127.0.0.1"
        assert parsed.port == 8081

    def test_fleet_options(self):
        parsed = args("fleet", "--port", "9000", "--bind", "0.0.0.0", "--token", "s", "--open")
        assert (parsed.port, parsed.bind, parsed.token, parsed.open) == (9000, "0.0.0.0", "s", True)

    def test_config_flags_are_all_optional(self):
        parsed = args("config")
        assert parsed.host is None and parsed.lane is None and parsed.fullscreen is None

    def test_config_fullscreen_can_be_turned_off(self):
        assert args("config", "--no-fullscreen").fullscreen is False
        assert args("config", "--fullscreen").fullscreen is True


class TestConfigCommand:
    def test_it_writes_and_prints(self, tmp_path, capsys, client):
        from megalink_viewer.cli import cmd_config

        target = tmp_path / "display.json"
        code = cmd_config(
            args("config", "--config", str(target), "--host", "stord-pk", "--lane", "9"), client
        )
        assert code == 0
        assert target.exists()
        printed = json.loads(capsys.readouterr().out)
        assert printed["host"] == "stord-pk"
        assert printed["lane"] == "9"

    def test_it_merges_rather_than_replaces(self, tmp_path, capsys, client):
        from megalink_viewer.cli import cmd_config

        target = tmp_path / "display.json"
        cmd_config(
            args("config", "--config", str(target), "--host", "stord-pk", "--lane", "9"), client
        )
        capsys.readouterr()
        cmd_config(args("config", "--config", str(target), "--lane", "3"), client)
        printed = json.loads(capsys.readouterr().out)
        assert printed["lane"] == "3"
        assert printed["host"] == "stord-pk"

    def test_it_never_prints_the_token(self, tmp_path, capsys, client):
        from megalink_viewer.cli import cmd_config

        target = tmp_path / "display.json"
        cmd_config(args("config", "--config", str(target), "--token", "hunter2"), client)
        printed = json.loads(capsys.readouterr().out)
        assert printed["web"]["token"] is True

    def test_an_invalid_value_is_refused(self, tmp_path, capsys, client):
        from megalink_viewer.cli import cmd_config

        target = tmp_path / "display.json"
        code = cmd_config(args("config", "--config", str(target), "--mode", "gui"), client)
        assert code == 0
        # argparse rejects an unknown mode before it reaches us.
        with pytest.raises(SystemExit):
            args("config", "--mode", "hologram")

    def test_a_broken_file_is_reported(self, tmp_path, capsys, client):
        from megalink_viewer.cli import cmd_config

        target = tmp_path / "display.json"
        target.write_text("{ broken", encoding="utf-8")
        assert cmd_config(args("config", "--config", str(target)), client) == 2
        assert "not valid JSON" in capsys.readouterr().err


class TestStopFlag:
    """Being asked to stop has to work while Tk owns the main thread.

    A plain signal handler only runs when the interpreter regains control, which
    Tk's main loop does not reliably give it -- so this is done with a blocked
    signal and a waiting thread. Tested in a subprocess because it is about how
    the whole process behaves on a signal.
    """

    SCRIPT = """
import sys, time
sys.path.insert(0, {src!r})
from megalink_viewer.cli import stop_flag
stopping = stop_flag()
print("ready", flush=True)
deadline = time.monotonic() + 20
while not stopping() and time.monotonic() < deadline:
    time.sleep(0.02)
print("STOPPED" if stopping() else "TIMED OUT", flush=True)
print("REASON", stopping.reason, flush=True)
"""

    def _run(self, tmp_path, signal_number, report_reason=False):
        import subprocess
        import sys
        from pathlib import Path

        src = str(Path(__file__).resolve().parent.parent / "src")
        script = tmp_path / "flag.py"
        script.write_text(self.SCRIPT.format(src=src), encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-u", str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout.readline().strip() == "ready"
        process.send_signal(signal_number)
        try:
            out, _ = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            raise AssertionError(f"process ignored signal {signal_number}") from None
        return process.returncode, out

    PENDING_SCRIPT = """
import os, signal, sys, time
sys.path.insert(0, {src!r})
# A signal left pending by whatever ran before us -- exactly what survives an
# exec when the shell that started us was signalled.
signal.pthread_sigmask(signal.SIG_BLOCK, {{signal.SIGTERM}})
os.kill(os.getpid(), signal.SIGTERM)
from megalink_viewer.cli import stop_flag
stopping = stop_flag()
time.sleep(0.5)
print("DISCARDED", ",".join(stopping.discarded), flush=True)
print("STOPPED" if stopping() else "STILL RUNNING", flush=True)
"""

    def test_a_signal_pending_at_startup_is_discarded(self, tmp_path):
        """Otherwise the display stops before drawing a frame, and exits 0 doing it."""
        import subprocess
        import sys
        from pathlib import Path

        src = str(Path(__file__).resolve().parent.parent / "src")
        script = tmp_path / "pending.py"
        script.write_text(self.PENDING_SCRIPT.format(src=src), encoding="utf-8")
        out = subprocess.run(
            [sys.executable, "-u", str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        assert "DISCARDED SIGTERM" in out, out
        assert "STILL RUNNING" in out, out

    def test_the_reason_is_recorded(self, tmp_path):
        """The journal has to say why a display gave up."""
        import signal as signal_module

        _code, out = self._run(tmp_path, signal_module.SIGTERM, report_reason=True)
        assert "REASON signal SIGTERM" in out, out

    @pytest.mark.parametrize("name", ["SIGTERM", "SIGINT"])
    def test_the_flag_trips_and_the_process_exits_cleanly(self, tmp_path, name):
        import signal as signal_module

        code, out = self._run(tmp_path, getattr(signal_module, name))
        assert "STOPPED" in out, out
        assert code == 0, out
