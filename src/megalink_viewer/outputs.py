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
from typing import Any, Callable

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


def describe(found: list[Screen]) -> str:
    """A line for the journal saying what was found."""
    if not found:
        return "no screens reported by xrandr"
    return "screens: " + ", ".join(f"{s.name} {s.geometry}" for s in found)
