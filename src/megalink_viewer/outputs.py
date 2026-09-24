"""Finding the screens attached to a machine.

A Raspberry Pi 4 Model B and a Pi 5 both have two HDMI sockets, and a range
often wants two firing points on one Pi rather than two Pis. Raspberry Pi OS
joins both outputs into a single X screen side by side, so showing a different
firing point on each is a matter of putting each window on the right part of it
-- which means knowing where each output starts and how big it is.

``xrandr --listmonitors`` is what says so. It has been in xrandr since 1.5 and
its output is far easier to read than ``--query``::

    Monitors: 2
     0: +*HDMI-1 1920/530x1080/300+0+0  HDMI-1
     1: +HDMI-2 1920/530x1080/300+1920+0  HDMI-2

The parsing is kept apart from the running so it can be tested against real
output from real machines without either a Pi or an X server.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from typing import Any

#: ``index: [+][*]name WIDTH[/mm]xHEIGHT[/mm]+X+Y  output``
_MONITOR = re.compile(
    r"^\s*(?P<index>\d+):\s+"
    r"(?P<flags>[+*]*)"
    r"(?P<name>\S+)\s+"
    r"(?P<width>\d+)(?:/\d+)?x(?P<height>\d+)(?:/\d+)?"
    r"(?P<x>[-+]\d+)(?P<y>[-+]\d+)"
)


class Screen:
    """One output, and where it sits in the X screen the outputs share."""

    __slots__ = ("height", "name", "primary", "width", "x", "y")

    def __init__(
        self,
        name: str,
        width: int,
        height: int,
        x: int = 0,
        y: int = 0,
        primary: bool = False,
    ) -> None:
        self.name = name
        self.width = int(width)
        self.height = int(height)
        self.x = int(x)
        self.y = int(y)
        self.primary = bool(primary)

    @property
    def geometry(self) -> str:
        """As Tk wants it: ``WIDTHxHEIGHT+X+Y``."""
        return f"{self.width}x{self.height}+{self.x}+{self.y}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Screen):
            return NotImplemented
        return (self.name, self.width, self.height, self.x, self.y) == (
            other.name,
            other.width,
            other.height,
            other.x,
            other.y,
        )

    def __repr__(self) -> str:
        return f"Screen({self.name!r}, {self.geometry})"


def parse_monitors(text: str) -> list[Screen]:
    """Decode ``xrandr --listmonitors``.

    Ordered left to right rather than in the order xrandr happens to list them,
    so "the first screen" means the one on the left of the bench and stays
    meaning that when a cable is moved between sockets.
    """
    found = []
    for line in text.splitlines():
        match = _MONITOR.match(line)
        if match is None:
            continue
        found.append(
            Screen(
                name=match["name"],
                width=int(match["width"]),
                height=int(match["height"]),
                x=int(match["x"]),
                y=int(match["y"]),
                primary="*" in match["flags"],
            )
        )
    found.sort(key=lambda screen: (screen.x, screen.y, screen.name))
    return found


def screens(run: Callable[..., Any] | None = None) -> list[Screen]:
    """The screens attached now, or an empty list if we cannot tell.

    Not being able to ask is not an error: a console display has no X server to
    ask, and a machine with one screen needs none of this. The caller decides
    what to do about an answer it did not get.
    """
    runner = run or _run_xrandr
    try:
        text = runner()
    except Exception:
        return []
    return parse_monitors(text or "")


def _run_xrandr() -> str:  # pragma: no cover - needs an X server
    result = subprocess.run(
        ["xrandr", "--listmonitors"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


# -- laying the outputs out ----------------------------------------------------

#: ``NAME connected [primary] [WIDTHxHEIGHT+X+Y] ...`` in ``xrandr --query``.
#: An output that is plugged in but has no mode set has no geometry.
_OUTPUT = re.compile(
    r"^(?P<name>\S+) connected(?: primary)?"
    r"(?: (?P<width>\d+)x(?P<height>\d+)(?P<x>[-+]\d+)(?P<y>[-+]\d+))?"
)


def _socket_order(name: str) -> tuple[Any, ...]:
    """HDMI-1 before HDMI-2 before HDMI-10: numbers compared as numbers."""
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name))


def parse_query(text: str) -> list[Screen]:
    """The outputs with something plugged into them, from ``xrandr --query``.

    In socket order rather than by position, because this is what decides the
    positions. An output with nothing showing on it yet is 0x0 at the origin.
    """
    found = []
    for line in (text or "").splitlines():
        match = _OUTPUT.match(line)
        if match is None:
            continue
        found.append(
            Screen(
                name=match["name"],
                width=int(match["width"] or 0),
                height=int(match["height"] or 0),
                x=int(match["x"] or 0),
                y=int(match["y"] or 0),
                primary=" connected primary" in line,
            )
        )
    found.sort(key=lambda screen: _socket_order(screen.name))
    return found


def _overlap(a: Screen, b: Screen) -> bool:
    """Whether two outputs show some of the same part of the X screen."""
    if not (a.width and a.height and b.width and b.height):
        return True  # not showing anything yet: it will need placing
    return (
        a.x < b.x + b.width
        and b.x < a.x + a.width
        and a.y < b.y + b.height
        and b.y < a.y + a.height
    )


def side_by_side(connected: list[Screen]) -> list[str] | None:
    """The ``xrandr`` command that puts the first two outputs side by side.

    ``None`` when there is nothing to do: fewer than two plugged in, or two
    already apart. With no configuration, X starts every output at the same
    place -- mirrored -- and the Lite image has none of the desktop's tools for
    arranging them, so both firing points' windows landed in the same place and
    both screens showed the same thing. The first socket goes on the left: on a
    Pi 4 and a Pi 5 that is HDMI 0, the one next to the power.
    """
    if len(connected) < 2:
        return None
    left, right = connected[0], connected[1]
    if not _overlap(left, right):
        return None
    return [
        "xrandr",
        "--output",
        left.name,
        "--auto",
        "--pos",
        "0x0",
        "--output",
        right.name,
        "--auto",
        "--right-of",
        left.name,
    ]


def connected(run: Callable[..., Any] | None = None) -> list[Screen]:
    """The outputs with something plugged in, or an empty list if we cannot tell."""
    try:
        return parse_query((run or _run_query)() or "")
    except Exception:
        return []


def arrange(
    run_query: Callable[[], str] | None = None,
    run: Callable[[list[str]], bool] | None = None,
) -> str:
    """Put two screens side by side if they are not already. A line for the log."""
    found = connected(run_query)
    command = side_by_side(found)
    if command is None:
        if len(found) < 2:
            return f"{len(found)} screen(s) plugged in; nothing to arrange"
        return f"{found[0].name} and {found[1].name} are already side by side"
    ok = (run or _run_command)(command)
    placed = f"{found[0].name} on the left, {found[1].name} on the right"
    return placed if ok else f"could not arrange the screens ({' '.join(command)} failed)"


def _run_query() -> str:  # pragma: no cover - needs an X server
    result = subprocess.run(
        ["xrandr", "--query"], capture_output=True, text=True, timeout=10, check=False
    )
    return result.stdout if result.returncode == 0 else ""


def _run_command(command: list[str]) -> bool:  # pragma: no cover - needs an X server
    try:
        return subprocess.run(command, capture_output=True, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def describe(found: list[Screen]) -> str:
    """A line for the journal saying what was found."""
    if not found:
        return "no screens reported by xrandr"
    return "screens: " + ", ".join(f"{s.name} {s.geometry}" for s in found)
