"""What is running, for a text console: the logo, the name, and where it is from.

A display's own screen says what it is. The text console, which is what is on
the screen while a display boots, while it restarts, and for anyone who logs in,
said nothing: a login prompt and a hostname. This is what goes there instead,
and on the console display's own screens.

The logo is the project's icon drawn in background colours, two columns to a
cell, the way the console draws a QR code. Every terminal there has ever been
can set a background colour, and the Linux console's font may have no block
characters at all. It is worked out from the icon's shapes rather than drawn by
hand, so the two stay the same picture.
"""

from __future__ import annotations

import math
import re

NAME = "Megalink viewer"
TAGLINE = "A score display for Megalink Live"
PROJECT_URL = "https://github.com/null-jones/rpi-megalink-viewer"

#: The icon's colours, as the eight background colours a console is sure to
#: have: the blue arcs are closest to cyan, and the dark ground is the
#: console's own, so the icon's rounded square is left out.
_BACKGROUNDS = {"arc": 46, "face": 40, "ring": 47, "shot": 41}
RESET = "\x1b[0m"

#: Cells across (and down) the logo, and samples across each cell. Small enough
#: to sit beside a few lines of text, big enough for the rings to read as rings.
LOGO_CELLS = 18
_SAMPLES = 5
#: How much of a cell a shape must cover to colour it.
_COVERAGE = 0.35
#: Where the shapes are in the icon's 64-unit square: the arcs reach further
#: out across than the target does down, so the picture is wider than tall.
_ACROSS = (8.0, 56.0)
_DOWN = (14.0, 50.0)


def _layers(x: float, y: float) -> str | None:
    """The topmost of the icon's shapes at a point of its 64-unit square.

    In the icon's own painting order: the arcs, the black face, its outer ring,
    the shot, and the inner ring drawn back over the shot.
    """
    r = math.hypot(x - 32, y - 32)
    top = None
    # The two arcs of the broadcast ring, radius 22, each 100 degrees wide
    # about the horizontal -- a target, and a signal on a second look.
    if abs(r - 22) <= 1.5 and abs(math.degrees(math.atan2(y - 32, abs(x - 32)))) <= 50:
        top = "arc"
    if r <= 16.5 + 1.2:
        top = "face"
    if abs(r - 16.5) <= 1.2:
        top = "ring"
    if math.hypot(x - 38.4, y - 25.6) <= 6:
        top = "shot"
    if abs(r - 9) <= 1.2:
        top = "ring"
    return top


def logo(cells: int = LOGO_CELLS) -> list[list[str | None]]:
    """The icon as rows of cells, each the name of its colour or ``None``."""
    grid: list[list[str | None]] = []
    # Square cells, cropped to the shapes: the icon's ground is dropped.
    left, top = _ACROSS[0], _DOWN[0]
    step = (_ACROSS[1] - _ACROSS[0]) / cells
    rows = int((_DOWN[1] - _DOWN[0]) / step)
    for row in range(rows):
        line: list[str | None] = []
        for column in range(cells):
            counts: dict[str, int] = {}
            for i in range(_SAMPLES):
                for j in range(_SAMPLES):
                    x = left + (column + (i + 0.5) / _SAMPLES) * step
                    y = top + (row + (j + 0.5) / _SAMPLES) * step
                    shape = _layers(x, y)
                    if shape is not None:
                        counts[shape] = counts.get(shape, 0) + 1
            chosen = None
            # Thin lines win over what they cross, as they are painted over it.
            for shape in ("ring", "shot", "arc", "face"):
                if counts.get(shape, 0) >= _COVERAGE * _SAMPLES * _SAMPLES:
                    chosen = shape
                    break
            line.append(chosen)
        grid.append(line)
    return grid


def logo_lines(cells: int = LOGO_CELLS) -> list[str]:
    """The logo in background colours, two columns to a cell, each line reset."""
    lines = []
    for row in logo(cells):
        text = ""
        for shape in row:
            text += f"\x1b[{_BACKGROUNDS[shape]}m  " if shape else f"{RESET}  "
        lines.append(text + RESET)
    return lines


def banner(
    width: int,
    address: str | None = None,
    color: bool = True,
    name: str = "",
) -> list[str]:
    """The logo with what this is beside it, for a console ``width`` wide.

    ``address`` is where the settings page is, if known. Without colour -- in
    a log, where colour codes are noise -- or on a console too narrow for it,
    there is no logo and only the words.
    """
    words = [
        "\x1b[1m" + NAME + RESET if color else NAME,
        TAGLINE,
        "",
    ]
    if name:
        words.append(f"This display: {name}")
    if address:
        words.append(f"Settings:     {address}")
    words.append(f"Project:      {PROJECT_URL}")
    widest = max(len(_plain(line)) for line in words)
    drawing = logo_lines() if color else []
    logo_width = 2 * LOGO_CELLS
    if not drawing or logo_width + 4 + widest > width:
        return ["  " + line for line in words]
    top = max(0, (len(drawing) - len(words)) // 2)
    lines = []
    for index, row in enumerate(drawing):
        text = words[index - top] if 0 <= index - top < len(words) else ""
        lines.append("  " + row + "  " + text)
    return lines


def footer(width: int) -> str:
    """One line saying what this is, for the foot of a console screen."""
    line = f"{NAME} · {PROJECT_URL.removeprefix('https://')}"
    return line if len(line) <= width else NAME


def issue(port: int = 8080) -> str:
    """The banner for the text console's login screen, ``/etc/issue.d``.

    What is on the screen while a display boots, and whenever it is not drawing.
    The login prompt fills in ``\\4`` with the machine's address and ``\\n``
    with its name when it shows it, so both stay right after the file is written.
    The screen is cleared first, of boot messages nobody at a range can use.
    """
    suffix = "" if port == 80 else f":{port}"
    lines = banner(120, address=f"http://\\4{suffix}/", name="\\n")
    return "\x1b[H\x1b[2J\n" + "\n".join(lines) + "\n\n"


def _plain(text: str) -> str:
    """Text without its colour codes, for measuring."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)
