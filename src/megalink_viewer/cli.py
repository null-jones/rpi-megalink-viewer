"""Command line entry point.

``megalink hosts`` / ``ranges`` / ``lanes`` explore what is live; ``show`` prints
one snapshot; ``watch`` is the per-position display, and is what a Raspberry Pi
would run at boot.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from .client import MegalinkClient, MegalinkError
from .config import DEFAULT_BEACON_PORT, MODES, ConfigError
from .render import frame, render_lane
from .watch import RangeWatcher

CLEAR_SCREEN = "\x1b[2J\x1b[H"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
HOME = "\x1b[H"
CLEAR_TO_EOL = "\x1b[K"
CLEAR_BELOW = "\x1b[J"


def _use_color(choice: str, stream: object) -> bool:
    if choice == "always":
        return True
    if choice == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _terminal_size(width: int | None, height: int | None) -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(80, 24))
    return width or size.columns, height or size.lines


def cmd_hosts(args: argparse.Namespace, client: MegalinkClient) -> int:
    active = client.active()
    if not active:
        print("no clubs are streaming right now")
        return 0
    for host in sorted(active):
        ranges = active[host]
        if args.quiet:
            print(host)
            continue
        name = ranges[0].host_name if ranges else host
        detail = ", ".join(r.name for r in ranges)
        print(f"{host:34} {name:32} {detail}")
    return 0


def cmd_ranges(args: argparse.Namespace, client: MegalinkClient) -> int:
    found = client.ranges(args.host)
    if not found:
        print(f"{args.host}: no ranges are live", file=sys.stderr)
        return 1
    for range_ in found:
        bits = [f"protocol v{range_.protocol}"]
        if range_.event:
            bits.append(range_.event)
        print(f"{range_.key:28} {range_.name:28} {' · '.join(bits)}")
    return 0


def cmd_lanes(args: argparse.Namespace, client: MegalinkClient) -> int:
    source = client.resolve(args.host, args.range)
    tree = client.snapshot(source)
    lanes = source.lanes(tree)
    if not lanes:
        print(f"{args.host}/{source.range_key}: no firing points", file=sys.stderr)
        return 1
    for lane in lanes:
        view = source.lane_view(tree, lane)
        result = view.result
        total = result.total.display() if result is not None else "-"
        shots = result.shot_count if result is not None else 0
        print(f"{lane:>4}  {view.shooter_name:34} {total:>12}  {shots:>3} shots")
    return 0


def cmd_show(args: argparse.Namespace, client: MegalinkClient) -> int:
    source = client.resolve(args.host, args.range)
    view = source.lane_view(client.snapshot(source), args.lane)
    width, _ = _terminal_size(args.width, None)
    color = _use_color(args.color, sys.stdout)
    print("\n".join(render_lane(view, width=width, age=0.0, color=color)))
    return 0


def cmd_watch(args: argparse.Namespace, client: MegalinkClient) -> int:
    stopping = stop_flag()  # before the watcher thread; see stop_flag
    source = client.resolve(args.host, args.range)
    watcher = RangeWatcher(source, client)
    state = watcher.start()
    color = _use_color(args.color, sys.stdout)
    out = sys.stdout
    # Cursor control only makes sense on a terminal. Piped or redirected, frames
    # are just written one after another so the output stays readable.
    interactive = bool(getattr(out, "isatty", lambda: False)())

    if interactive:
        out.write(HIDE_CURSOR + CLEAR_SCREEN)
        out.flush()
    try:
        watcher.wait_for_data(timeout=max(5.0, args.interval * 30))
        while not stopping():
            view = state.lane_view(args.lane)
            width, height = _terminal_size(args.width, args.height)
            out.write(frame(view, width, height, state.age(), color=color, interactive=interactive))
            out.flush()
            time.sleep(args.interval)
    finally:
        watcher.stop()
        if interactive:
            out.write(SHOW_CURSOR + "\n")
            out.flush()
    return 0


def cmd_gui(args: argparse.Namespace, client: MegalinkClient) -> int:
    """Open the windowed display."""
    from .gui import LaneWindow

    source = client.resolve(args.host, args.range)
    watcher = RangeWatcher(source, client)
    state = watcher.start()
    # Wait briefly so the window opens with a card rather than placeholders.
    watcher.wait_for_data(timeout=10.0)

    lane = str(args.lane)
    available = state.lanes()
    if available and lane not in available:
        print(
            f"megalink: lane {lane!r} is not on {args.host}/{source.range_key} "
            f"(firing points: {', '.join(available)})",
            file=sys.stderr,
        )
        watcher.stop()
        return 1

    window = LaneWindow(
        state,
        lane,
        interval_ms=int(args.interval * 1000),
        fullscreen=args.fullscreen,
    )
    try:
        window.run()
    finally:
        watcher.stop()
    return 0


class StopFlag:
    """Whether the process has been asked to stop, and what asked it."""

    def __init__(self) -> None:
        self._stopped = False
        #: What tripped the flag, for the log.
        self.reason = ""
        #: Signals that were already pending at startup and were thrown away.
        self.discarded: list[str] = []

    def __call__(self) -> bool:
        return self._stopped

    def trip(self, reason: str) -> None:
        self.reason = reason
        self._stopped = True


def stop_flag(*signals: int) -> StopFlag:
    """A flag that becomes true when the process is asked to stop.

    A Python signal handler only runs when the interpreter regains control, and
    Tk's main loop does not reliably give it back -- on macOS it never does. A
    handler alone therefore leaves a windowed display ignoring systemd's SIGTERM
    until it gets killed. Blocking the signals and waiting for them in a
    dedicated thread works whatever the main thread is busy with.

    Signals already *pending* when this is called are discarded rather than
    obeyed. A pending signal survives ``exec``, so one aimed at whatever ran
    before -- the launcher shell, say, which ``xinit`` signals on the way out --
    would otherwise stop this program before it drew a single frame, instantly
    and with a clean exit status that made it look deliberate.

    **Call this before starting any threads.** Blocking a signal is per-thread
    and inherited at creation, so a thread started beforehand still has it
    unblocked -- and the signal then gets delivered there, where the default
    action terminates the process before anything can react.

    Falls back to an ordinary handler where the POSIX calls are unavailable.
    """
    wanted = set(signals) or {signal.SIGINT, signal.SIGTERM}
    flag = StopFlag()

    block = getattr(signal, "pthread_sigmask", None)
    wait = getattr(signal, "sigwait", None)
    pending = getattr(signal, "sigpending", None)
    if block is None or wait is None:  # pragma: no cover - not POSIX

        def handle(signum: object, frame: object) -> None:
            flag.trip(f"signal {signum}")

        for number in wanted:
            signal.signal(number, handle)
        return flag

    # Threads started after this inherit the mask, so only the waiter below
    # will be handed these signals.
    block(signal.SIG_BLOCK, wanted)

    if pending is not None:
        # Consume anything already queued. sigwait returns at once for a signal
        # that is pending, so this cannot block.
        for number in sorted(set(pending()) & wanted):
            wait({number})
            flag.discarded.append(_signal_name(number))

    def waiter() -> None:
        number = wait(wanted)
        flag.trip(f"signal {_signal_name(number)}")

    threading.Thread(target=waiter, name="megalink-signals", daemon=True).start()
    return flag


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:  # pragma: no cover - unknown signal
        return str(number)


# -- an installed display ---------------------------------------------------


def _screen_size(controller: Any) -> tuple[int | None, int | None]:
    """The configured screen size, or nothing to let the toolkit decide."""
    settings = controller.config.display
    return settings.width, settings.height


def two_screens(
    lane2: str,
    report: Callable[[str], None],
    find: Callable[[], list[Any]] | None = None,
) -> tuple[Any, Any] | None:
    """The two outputs to put two firing points on, or ``None`` for one screen.

    ``None`` both when no second firing point is configured and when one is but
    only a single screen is attached -- a Pi Zero, or a Pi 4 with one cable in.
    The second case is said out loud, because a setting that silently does
    nothing looks exactly like a broken one.
    """
    if not (lane2 or "").strip():
        return None
    from .outputs import describe, screens

    found = (find or screens)()
    report(describe(found))
    if not found:
        report("display.lane2 is set but no screens were reported; showing one")
        return None
    if len(found) < 2:
        report("display.lane2 is set but only one screen is attached; showing one")
        return None
    return found[0], found[1]


def restart_reason(config: Any, startup_mode: str, startup_two: bool) -> str:
    """Why the display must start again to apply a setting, or empty if not.

    The way the scores are shown, and whether there is a second screen, are
    both fixed when the display starts: the launcher picks the mode, and the
    screens are laid out and the windows made once.
    """
    if config.display.mode != startup_mode:
        return f"mode changed to {config.display.mode}"
    if bool(config.display.lane2.strip()) != startup_two:
        return "second screen switched " + ("off" if startup_two else "on")
    return ""


def cmd_display(args: argparse.Namespace, client: MegalinkClient) -> int:
    """Run the display this machine is configured to be.

    Everything comes from the configuration file: what to show, how to show it,
    whether to serve a configuration page, whether to announce itself. This is
    what the systemd unit runs, and it is deliberately argument-free so that
    changing the display never means editing the unit.
    """
    from .beacon import Announcer, Listener, payload_for
    from .config import default_path, load
    from .controller import Controller, run_terminal
    from .webconfig import ConfigServer

    path = Path(args.config) if args.config else default_path()
    try:
        config = load(path)
    except ConfigError as exc:
        print(f"megalink: {exc}", file=sys.stderr)
        return 2

    # Before any thread starts; see stop_flag.
    stopping = stop_flag()
    for name in stopping.discarded:
        # Worth saying out loud: a stray pending signal used to stop the display
        # before it drew anything, and looked like a clean exit while doing it.
        print(f"discarded a {name} left pending at startup", file=sys.stderr)

    # Progress is logged as it happens. A display that dies during startup is
    # otherwise indistinguishable from one that never started, and the journal
    # should say how far it got rather than leaving it to be inferred.
    print(
        f"megalink {__version__} starting: config {path}, mode {config.display.mode}",
        file=sys.stderr,
    )

    controller = Controller(config=config, path=path, client=client)
    controller.start()
    print(f"showing: {config.describe()}", file=sys.stderr)

    # Every display hears every other one already, so each can serve the fleet
    # dashboard at /fleet: there is no separate service to run, and no one
    # machine whose loss takes the dashboard with it.
    peers: Any = None
    if config.beacon.enabled and config.web.enabled:
        peers = Listener(config.beacon.port).start()

    web: Any = None
    if config.web.enabled:
        try:
            web = ConfigServer(controller, listener=peers).start()
        except OSError as exc:
            print(f"megalink: configuration page not started: {exc}", file=sys.stderr)
        else:
            print(
                f"configuration page on port {web.port}"
                + (f", fleet dashboard on {web.port}/fleet" if peers is not None else ""),
                file=sys.stderr,
            )
            # The beacon has to advertise the port actually bound, which differs
            # from the configured one whenever that was 0.
            controller.config.web.port = web.port

    beacon: Any = None
    if config.beacon.enabled:
        beacon = Announcer(
            lambda: payload_for(controller),
            port=config.beacon.port,
            interval=config.beacon.interval,
            address=config.beacon.address,
        ).start()

    if not config.configured:
        print(
            "megalink: no club or firing point configured yet; "
            "open the configuration page to set one",
            file=sys.stderr,
        )

    # How the scores are shown is decided by the launcher before this process
    # starts, so a change of mode cannot be applied in place. Exiting lets
    # systemd start us again, and the launcher then reads the new mode -- which
    # is what makes "switch that Pi to console mode" work from the dashboard on
    # a display whose X server is broken.
    startup_mode = config.display.mode
    # The same for a second screen switched on or off: the screens are laid out
    # and the windows made once, at the start. Changing which firing point the
    # second screen shows needs no restart; the window follows the setting.
    startup_two = bool(config.display.lane2.strip())

    def should_stop() -> bool:
        if stopping():
            return True
        reason = restart_reason(controller.config, startup_mode, startup_two)
        if reason:
            print(f"{reason}; restarting", file=sys.stderr)
            return True
        return False

    try:
        # A short pause so a healthy display opens with a card rather than a
        # flash of placeholders -- but short, because when the feed is slow or
        # absent this is time spent showing nothing at all, and a screen saying
        # "connecting" beats a screen saying nothing.
        controller.wait_for_data(timeout=2.0)
        status = controller.status()
        if status["connected"]:
            print(f"feed: connected via protocol v{status['protocol']}", file=sys.stderr)
        elif status["connecting"]:
            # Drawing starts now regardless; the feed catches up when it can.
            print("feed: still connecting, showing the display anyway", file=sys.stderr)
        else:
            print(f"feed: not connected ({status['error'] or 'waiting'})", file=sys.stderr)
        if config.display.mode == "terminal":
            # The configured width and height are pixel overrides for the
            # window; a console is measured in characters, so ask the terminal.
            width, height = _terminal_size(None, None)
            # On a real console the frame is drawn over the previous one. Left
            # to scroll, a display that refreshes twice a second is unreadable.
            interactive = bool(getattr(sys.stdout, "isatty", lambda: False)())
            print(
                f"console display: {width}x{height}, "
                + ("redrawing in place" if interactive else "writing a frame at a time"),
                file=sys.stderr,
            )
            run_terminal(
                controller,
                sys.stdout.write,
                interval=config.display.interval,
                stop=should_stop,
                width=width,
                height=height,
                interactive=interactive,
            )
            print(_why_stopped(stopping, "the display loop ended"), file=sys.stderr)
            return 0

        from . import outputs

        # What is plugged in, for the settings page to offer a second screen.
        controller.screens = [screen.name for screen in outputs.connected()]
        if startup_two:
            # X starts both outputs mirrored; two firing points need them apart.
            print(f"megalink: {outputs.arrange()}", file=sys.stderr)

        if startup_mode == "browser":
            from .browser import BrowserDisplay, find_browser, low_memory_note, run_panes

            def note(message: str) -> None:
                print(f"megalink: {message}", file=sys.stderr)

            browser = find_browser()
            if browser is None:
                print(
                    "megalink: no browser installed; install one with:\n"
                    "  sudo apt install --no-install-recommends chromium-browser",
                    file=sys.stderr,
                )
                return 2
            print(
                f"using {browser} on DISPLAY={os.environ.get('DISPLAY', '(unset)')}",
                file=sys.stderr,
            )
            short = low_memory_note()
            if short:
                note(short)

            def pane(**extra: Any) -> BrowserDisplay:
                return BrowserDisplay(
                    controller,
                    should_stop=should_stop,
                    browser=browser,
                    report=note,
                    **extra,
                )

            panes = [pane()]
            pair = two_screens(config.display.lane2, note)
            if pair is not None:
                # Separate profiles: Chromium puts a second window into the
                # first one's session otherwise, and it lands on one screen.
                panes = [
                    pane(screen=pair[0], profile="/tmp/megalink-browser-1", label="screen 1"),
                    pane(
                        screen=pair[1],
                        profile="/tmp/megalink-browser-2",
                        lane_source=lambda: controller.config.display.lane2,
                        label="screen 2",
                    ),
                ]
            run_panes(panes, should_stop)
            print(_why_stopped(stopping, "the browser was closed"), file=sys.stderr)
            return 0

        from .gui import LaneWindow

        print(
            f"opening a window on DISPLAY={os.environ.get('DISPLAY', '(unset)')}", file=sys.stderr
        )
        # A Pi 4 Model B and a Pi 5 each have two HDMI sockets. With a second
        # firing point configured, and a second screen actually attached, each
        # gets a window of its own on it -- told which before it is made, so
        # the window manager never takes it and stretches it over both.
        pair = two_screens(
            config.display.lane2, lambda message: print(f"megalink: {message}", file=sys.stderr)
        )
        window = LaneWindow(
            controller,
            controller.config.lane or "1",
            interval_ms=int(config.display.interval * 1000),
            fullscreen=config.display.fullscreen,
            on_lane_change=controller.set_lane,
            should_stop=should_stop,
            lane_source=lambda: controller.config.lane,
            screen=pair[0] if pair is not None else None,
        )
        second = None
        if pair is not None:
            second = LaneWindow(
                controller,
                config.display.lane2.strip(),
                interval_ms=int(config.display.interval * 1000),
                should_stop=should_stop,
                lane_source=lambda: controller.config.display.lane2,
                parent=window.root,
                screen=pair[1],
            )
            window.screen_label, second.screen_label = "screen 1", "screen 2"
            print(
                f"two screens: {pair[0].name} showing lane {window.lane}, "
                f"{pair[1].name} showing lane {second.lane}",
                file=sys.stderr,
            )

        if second is None:
            width, height = _screen_size(controller)
            if width and height:
                window.root.geometry(f"{width}x{height}")
        print("window open", file=sys.stderr)
        print(window.describe(), file=sys.stderr)
        if second is not None:
            # Its own redraw loop; the main loop below drives both.
            second.refresh()
        window.run()
        print(_why_stopped(stopping, "the window was closed"), file=sys.stderr)
        return 0
    finally:
        if beacon is not None:
            beacon.stop()
        if web is not None:
            web.stop()
        if peers is not None:
            peers.stop()
        controller.stop()


def _why_stopped(stopping: StopFlag, otherwise: str) -> str:
    """A line for the journal saying why the display gave up."""
    return f"stopping: {stopping.reason}" if stopping.reason else f"stopping: {otherwise}"


def cmd_netwatch(args: argparse.Namespace, client: MegalinkClient) -> int:
    """Fall back to a Wi-Fi hotspot when there is no network. Runs as root.

    Its own service rather than part of the display, so the display never has
    the privileges changing the network needs.
    """
    from . import network
    from .config import default_path, load

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("megalink: netwatch changes the network, so it must run as root", file=sys.stderr)
        return 2
    path = Path(args.config) if args.config else default_path()
    try:
        name = load(path).name
    except ConfigError:
        name = ""
    stopping = stop_flag()
    # Beside the display's own configuration, which is where its settings page
    # leaves a network to join and the one directory both services can reach.
    network.run(
        network.NetworkManager(interface=args.interface),
        stop=stopping,
        name=name,
        report=lambda message: print(f"netwatch: {message}", file=sys.stderr, flush=True),
        request_path=path.parent / "wifi-request.json",
        secret_path=path.parent / "hotspot.json",
    )
    return 0


def cmd_banner(args: argparse.Namespace, client: MegalinkClient) -> int:
    """Say what is running, for a text console: an SSH login, or the login screen."""
    from . import address, banner
    from .config import default_path, load

    path = Path(args.config) if args.config else default_path()
    try:
        config = load(path)
    except ConfigError:
        config = None
    port = config.web.port if config is not None and config.web.enabled else 0
    if args.issue:
        sys.stdout.write(banner.issue(port or 8080))
        return 0
    reach = address.find(port) if port else None
    url = reach.url() if reach is not None else None
    width = shutil.get_terminal_size((100, 24)).columns
    name = config.name if config is not None else ""
    for line in banner.banner(width, address=url, color=not args.plain, name=name):
        print(line)
    return 0


def cmd_identify_overlay(args: argparse.Namespace, client: MegalinkClient) -> int:
    """Flash a display's name over its browser for a few seconds."""
    from .overlay import show

    show(args.text, args.seconds, args.geometry)
    return 0


def cmd_fleet(args: argparse.Namespace, client: MegalinkClient) -> int:
    """Serve the dashboard that lists every display on this network."""
    from .fleet import FleetServer

    stopping = stop_flag()  # before the server's threads; see stop_flag
    server = FleetServer(
        port=args.port,
        bind=args.bind,
        beacon_port=args.beacon_port,
        token=args.token or "",
    )
    try:
        server.start()
    except OSError as exc:
        print(f"megalink: {exc}", file=sys.stderr)
        return 2

    shown = args.bind if args.bind not in ("0.0.0.0", "") else "127.0.0.1"
    url = f"http://{shown}:{server.port}/"
    print(f"fleet dashboard on {url}", file=sys.stderr)
    if args.open:
        import webbrowser

        webbrowser.open(url)

    try:
        while not stopping():
            time.sleep(0.25)
    finally:
        server.stop()
    return 0


def cmd_config(args: argparse.Namespace, client: MegalinkClient) -> int:
    """Show or edit the configuration file, for setting a display up by hand."""
    from .config import default_path, load, save

    path = Path(args.config) if args.config else default_path()
    try:
        config = load(path)
    except ConfigError as exc:
        print(f"megalink: {exc}", file=sys.stderr)
        return 2

    changes: dict[str, Any] = {}
    for name in ("host", "range", "lane"):
        value = getattr(args, name)
        if value is not None:
            changes[name] = value
    display: dict[str, Any] = {}
    if args.mode is not None:
        display["mode"] = args.mode
    if args.url is not None:
        display["url"] = args.url
    if args.lane2 is not None:
        display["lane2"] = args.lane2
    if args.fullscreen is not None:
        display["fullscreen"] = args.fullscreen
    if display:
        changes["display"] = display
    web: dict[str, Any] = {}
    if args.web_port is not None:
        web["port"] = args.web_port
    if args.token is not None:
        web["token"] = args.token
    if web:
        changes["web"] = web
    beacon: dict[str, Any] = {}
    if args.name is not None:
        beacon["name"] = args.name
    if beacon:
        changes["beacon"] = beacon

    if changes:
        try:
            config = config.merged(changes)
        except ConfigError as exc:
            print(f"megalink: {exc}", file=sys.stderr)
            return 2
        save(config, path)
        print(f"wrote {path}", file=sys.stderr)

    print(json.dumps(config.public_dict(), indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megalink",
        description="Show Megalink Live shooting scores for one firing point.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colourise output (default: auto)",
    )

    # Repeated on every subcommand so `megalink show ... --color never` works as
    # readily as `megalink --color never show ...`. SUPPRESS stops the
    # subcommand's default from overwriting a value given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )

    sub = parser.add_subparsers(dest="command", required=True)

    hosts = sub.add_parser("hosts", parents=[common], help="list the clubs streaming now")
    hosts.add_argument("-q", "--quiet", action="store_true", help="slugs only")
    hosts.set_defaults(func=cmd_hosts)

    ranges = sub.add_parser("ranges", parents=[common], help="list a club's live ranges")
    ranges.add_argument("host", help="host slug, e.g. nidaros-skl")
    ranges.set_defaults(func=cmd_ranges)

    lanes = sub.add_parser("lanes", parents=[common], help="list a range's firing points")
    lanes.add_argument("host")
    lanes.add_argument("range", help="range key, name, or the slug from a Live URL")
    lanes.set_defaults(func=cmd_lanes)

    show = sub.add_parser("show", parents=[common], help="print one firing point once")
    show.add_argument("host")
    show.add_argument("range")
    show.add_argument("lane")
    show.add_argument("--width", type=int, help="override the terminal width")
    show.set_defaults(func=cmd_show)

    watch = sub.add_parser("watch", parents=[common], help="live per-position display")
    watch.add_argument("host")
    watch.add_argument("range")
    watch.add_argument("lane")
    watch.add_argument(
        "--interval", type=float, default=1.0, help="seconds between redraws (default: 1.0)"
    )
    watch.add_argument("--width", type=int, help="override the terminal width")
    watch.add_argument("--height", type=int, help="override the terminal height")
    watch.set_defaults(func=cmd_watch)

    gui = sub.add_parser(
        "gui", parents=[common], help="windowed display with the target and score table"
    )
    gui.add_argument("host")
    gui.add_argument("range")
    gui.add_argument("lane")
    gui.add_argument(
        "--interval", type=float, default=0.5, help="seconds between redraws (default: 0.5)"
    )
    gui.add_argument(
        "--fullscreen", action="store_true", help="start fullscreen (for a Pi with a screen)"
    )
    gui.set_defaults(func=cmd_gui)

    display = sub.add_parser(
        "display",
        parents=[common],
        help="run the display this machine is configured to be (for a Pi)",
    )
    display.add_argument("--config", help="path to the configuration file")
    display.set_defaults(func=cmd_display)

    netwatch = sub.add_parser(
        "netwatch",
        parents=[common],
        help="start a Wi-Fi hotspot when there is no network (runs as root)",
    )
    netwatch.add_argument("--config", help="path to the display's configuration file")
    netwatch.add_argument("--interface", default="wlan0", help="the Wi-Fi interface")
    netwatch.set_defaults(func=cmd_netwatch)

    shown = sub.add_parser(
        "banner", help="say what is running, for a console login (logo, address, project)"
    )
    shown.add_argument("--config", help="path to the display's configuration file")
    shown.add_argument(
        "--issue", action="store_true", help="for /etc/issue.d: the console's login screen"
    )
    shown.add_argument("--plain", action="store_true", help="no colour and no logo")
    shown.set_defaults(func=cmd_banner)

    overlay = sub.add_parser(
        "identify-overlay",
        help="flash a display's name over its browser (used by browser mode)",
    )
    overlay.add_argument("--text", required=True)
    overlay.add_argument("--seconds", type=float, default=8.0)
    overlay.add_argument("--geometry", help="the screen, as WIDTHxHEIGHT+X+Y")
    overlay.set_defaults(func=cmd_identify_overlay)

    fleet = sub.add_parser(
        "fleet", parents=[common], help="dashboard listing every display on this network"
    )
    fleet.add_argument("--port", type=int, default=8081, help="listening port (default: 8081)")
    fleet.add_argument("--bind", default="127.0.0.1", help="listening address (default: 127.0.0.1)")
    fleet.add_argument(
        "--beacon-port",
        type=int,
        default=DEFAULT_BEACON_PORT,
        help=f"port the displays announce on (default: {DEFAULT_BEACON_PORT})",
    )
    fleet.add_argument("--token", help="shared secret the displays require")
    fleet.add_argument("--open", action="store_true", help="open a browser at the dashboard")
    fleet.set_defaults(func=cmd_fleet)

    config = sub.add_parser(
        "config", parents=[common], help="show or edit this machine's configuration"
    )
    config.add_argument("--config", help="path to the configuration file")
    config.add_argument("--host", help="club slug to display")
    config.add_argument("--range", help="range key, name, or Live URL slug")
    config.add_argument("--lane", help="firing point to display")
    config.add_argument("--name", help="what to call this display")
    config.add_argument("--mode", choices=MODES, help="how to show the scores")
    config.add_argument(
        "--url", help="page for browser mode (default: Megalink's own, for this firing point)"
    )
    config.add_argument("--lane2", help="firing point for a second HDMI output (Pi 4 and Pi 5)")
    config.add_argument(
        "--fullscreen",
        dest="fullscreen",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="start the window fullscreen",
    )
    config.add_argument("--web-port", type=int, help="port for the configuration page")
    config.add_argument("--token", help="shared secret required to change anything")
    config.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = MegalinkClient()
    try:
        return int(args.func(args, client))
    except MegalinkError as exc:
        print(f"megalink: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
