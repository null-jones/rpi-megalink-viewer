"""Take the guide's screenshots from the software itself.

Run by screenshots.sh inside a Debian container with an X server, Tk and
Chromium. Every picture is the real window or page: the display's screens drawn
by its own window code against a captured Megalink feed, and the web pages
served by the real servers and photographed by a headless Chromium. So when a
screen changes, running this again brings the guide up to date.

    python3 docs/guide/tools/screens.py OUT_DIR
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import conftest  # noqa: E402 - the captured feeds the test suite uses

from megalink_viewer import gui, network, v2  # noqa: E402
from megalink_viewer.address import Reach  # noqa: E402
from megalink_viewer.beacon import encode, payload_for  # noqa: E402
from megalink_viewer.client import ARENA_DB, Source  # noqa: E402
from megalink_viewer.config import Config, save  # noqa: E402
from megalink_viewer.controller import Controller  # noqa: E402
from megalink_viewer.fleet import FleetServer  # noqa: E402
from megalink_viewer.outputs import Screen  # noqa: E402
from megalink_viewer.webconfig import ConfigServer  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/guide/images")
NAME = "megalink-a199"
ADDRESS = "192.168.1.23"
WIFI = "RangeNet"
HOTSPOT = {"ssid": NAME, "password": "k7mq-x4wp-9ht2", "address": network.HOTSPOT_ADDRESS}


def xserver(width: int, height: int) -> subprocess.Popen:
    """A fresh X server the size of the screen being photographed."""
    display = ":7"
    server = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = display
    time.sleep(1.5)
    return server


def shoot(name: str, size: str = "1280x720+0+0") -> None:
    subprocess.run(
        ["import", "-window", "root", "-crop", size, "+repage", str(OUT / f"{name}.png")],
        check=True,
    )
    print("  ", name, flush=True)


def state(configured: bool = True):
    # Imported here, once an X server is up: the test module checks at import
    # that Tk can open a window.
    import test_gui

    tree = conftest.load("v2_stord_pk")
    lanes = v2.lanes(tree)
    source = Source(2, ARENA_DB, "data/stord-pk/1-10", "stord-pk", "1-10")
    fake = test_gui.FakeState(tree, source, lanes)
    fake.config.beacon.name = NAME
    if not configured:
        fake.config.host = fake.config.lane = ""
    return fake


def reach(address: str | None = ADDRESS, interface: str = "wlan0") -> Reach:
    return Reach(NAME, [(interface, address)] if address else [], 8080, address)


def window(fake, lane: str, status=None, where=None, size=(1280, 720), **extra):
    win = gui.LaneWindow(
        fake,
        lane,
        interval_ms=500,
        find_reach=lambda port: where if where is not None else reach(),
        read_network=lambda: status,
        screen=Screen("HDMI-1", size[0], size[1], 0, 0),
        **extra,
    )
    return win


def display_screens() -> None:
    """The display's own screens, at 1280x720."""
    cases = [
        ("screen-setup", state(False), None, reach(), {}),
        (
            "screen-waiting",
            state(False),
            {"mode": "waiting", "hotspot_in": 24.4, "updated": time.time()},
            reach(None),
            {},
        ),
        (
            "screen-hotspot",
            state(False),
            {"mode": "hotspot", "hotspot": HOTSPOT},
            reach(network.HOTSPOT_ADDRESS),
            {},
        ),
        ("screen-scores-startup", state(), {"mode": "client", "wifi": WIFI}, reach(), {}),
        ("screen-scores", state(), {"mode": "client", "wifi": WIFI}, reach(), {"quiet": True}),
        (
            "screen-identify",
            state(),
            {"mode": "client"},
            reach(),
            {"identify": True, "quiet": True},
        ),
    ]
    for name, fake, status, where, options in cases:
        if options.get("identify"):
            fake.identifying = lambda: True
        win = window(fake, "9", status, where)
        if options.get("quiet"):
            win._first_address.done = True

        def take(n=name, w=win, lit=bool(options.get("identify"))) -> None:
            # The name blinks; wait for it to be lit, the way people see it.
            if lit and int(time.time() * 2) % 2 != 0:
                w.root.after(50, take)
                return
            shoot(n)
            w.close()

        win.root.after(2200, take)
        win.run()


def two_screens() -> None:
    """A Pi 4 with a 1080p screen and a 720p one beside it."""
    fake = state()
    fake.config.display.lane2 = "9"
    first = window(fake, "7", {"mode": "client"}, size=(1920, 1080))
    first._first_address.done = True
    second = gui.LaneWindow(
        fake,
        "9",
        interval_ms=500,
        parent=first.root,
        find_reach=lambda port: reach(),
        read_network=lambda: {"mode": "client"},
        screen=Screen("HDMI-2", 1280, 720, 1920, 0),
    )
    second._first_address.done = True
    second.refresh()

    def done() -> None:
        shoot("screens-two", "3200x1080+0+0")
        # The black below the smaller screen is not on either monitor.
        subprocess.run(
            [
                "convert",
                str(OUT / "screens-two.png"),
                "-fill",
                "#1d1f21",
                "-draw",
                "rectangle 1920,720 3200,1080",
                str(OUT / "screens-two.png"),
            ],
            check=True,
        )
        first.close()

    first.root.after(2500, done)
    first.run()


def client():
    return conftest.FakeClient(
        trees={("stord-pk", "1-10"): conftest.load("v2_stord_pk")},
        active={
            "stord-pk": {"1-10": {"hostName": "Stord PK", "rangeName": "1-10", "eventName": ""}}
        },
    )


def chromium(url: str, name: str, width: int, height: int) -> None:
    subprocess.run(
        [
            "chromium",
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={width},{height}",
            "--virtual-time-budget=6000",
            "--force-device-scale-factor=2",
            f"--screenshot={OUT / (name + '.png')}",
            url,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("  ", name, flush=True)


def display_server(
    base: Path,
    index: int,
    lane: str,
    lane2: str = "",
    screens=1,
    mode="gui",
    name=NAME,
    status=None,
):
    path = base / f"d{index}" / "display.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    config = Config(host="stord-pk" if lane else "", range="1-10" if lane else "", lane=lane)
    config.beacon.name = name
    config.web.port = 8801 + index
    config.web.bind = "127.0.0.1"
    config.beacon.enabled = False
    config.display.lane2 = lane2
    config.display.mode = mode
    save(config, path)
    controller = Controller(path=path, client=client())
    controller.reload()
    controller.wait_for_data(timeout=5)
    controller.screens = ["HDMI-1", "HDMI-2"][:screens]
    ConfigServer(controller, read_network=(lambda: status) if status else None).start()
    return controller


def web_pages() -> None:
    base = Path(tempfile.mkdtemp())
    networks = [
        {"ssid": WIFI, "signal": 82, "secure": True},
        {"ssid": "Clubhouse", "signal": 64, "secure": True},
        {"ssid": "Guest", "signal": 40, "secure": False},
    ]
    # One display for its settings page, on its own Wi-Fi, so the Wi-Fi
    # section shows the networks it found.
    display_server(
        base,
        0,
        "9",
        status={"mode": "hotspot", "hotspot": HOTSPOT, "networks": networks, "country": ""},
    )
    chromium("http://127.0.0.1:8801/", "page-settings", 430, 1500)
    # Two parts of it, for the two steps that use them: choosing what to show,
    # and choosing the Wi-Fi.
    whole = OUT / "page-settings.png"
    for part, crop in (
        ("page-settings-top", "860x1368+0+0"),
        ("page-settings-wifi", "860x800+0+2095"),
    ):
        subprocess.run(
            ["convert", str(whole), "-crop", crop, "+repage", str(OUT / f"{part}.png")], check=True
        )
    whole.unlink()
    # A Pi with two screens, for the second screen's firing point.
    display_server(base, 1, "9", lane2="10", screens=2, name="bench-3")
    chromium("http://127.0.0.1:8802/", "page-settings-two", 430, 760)

    # A range of four, for the range page.
    fleet = FleetServer(port=8800, bind="127.0.0.1", beacon_port=0, client=client()).start()
    controllers = [
        display_server(base, 2, "3", name="megalink-3f2a"),
        display_server(base, 3, "5", lane2="6", screens=2, name="megalink-a199"),
        display_server(base, 4, "7", name="megalink-c341", mode="browser"),
        display_server(base, 5, "", name="megalink-d7cb"),
    ]
    for controller in controllers:
        fleet.listener.accept(encode(payload_for(controller)), "127.0.0.1")
    chromium("http://127.0.0.1:8800/", "page-range", 1200, 900)
    fleet.stop()


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    # One X server for every shot, big enough for two screens: Tk keeps its
    # connection to the first one it met, so they cannot be started afresh.
    x = xserver(3200, 1080)
    print("display screens", flush=True)
    try:
        display_screens()
        two_screens()
    finally:
        x.terminate()
    print("web pages", flush=True)
    web_pages()
