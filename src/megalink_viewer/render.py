"""Text rendering of one firing point.

The output is built as a list of plain strings so it can be asserted on in
tests, with colour applied as a separate, optional pass. The layout adapts to
the terminal width and puts the running total in large block digits, on the
assumption that the display sits beside the firing point and is read at a
glance rather than studied.
"""

from __future__ import annotations

import re
import textwrap
import time
from typing import Any

from .models import LaneResult, LaneView, Series

#: 5-row block font, wide enough to read across a firing point.
_GLYPHS = {
    "0": ("███", "█ █", "█ █", "█ █", "███"),
    "1": ("  █", "  █", "  █", "  █", "  █"),
    "2": ("███", "  █", "███", "█  ", "███"),
    "3": ("███", "  █", "███", "  █", "███"),
    "4": ("█ █", "█ █", "███", "  █", "  █"),
    "5": ("███", "█  ", "███", "  █", "███"),
    "6": ("███", "█  ", "███", "█ █", "███"),
    "7": ("███", "  █", "  █", "  █", "  █"),
    "8": ("███", "█ █", "███", "█ █", "███"),
    "9": ("███", "█ █", "███", "  █", "███"),
    ".": ("   ", "   ", "   ", "   ", " █ "),
    ",": ("   ", "   ", "   ", "   ", " █ "),
    "-": ("   ", "   ", "███", "   ", "   "),
    ":": ("   ", " █ ", "   ", " █ ", "   "),
    "x": ("   ", "█ █", " █ ", "█ █", "   "),
    "X": ("   ", "█ █", " █ ", "█ █", "   "),
    " ": ("   ", "   ", "   ", "   ", "   "),
}
GLYPH_HEIGHT = 5

RESET = "\x1b[0m"
_COLORS = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "cyan": "\x1b[36m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "red": "\x1b[31m",
    "white": "\x1b[97m",
}


def colorize(text: str, *styles: str, enabled: bool = True) -> str:
    """Wrap *text* in ANSI styles, or return it unchanged when disabled."""
    if not enabled or not styles:
        return text
    prefix = "".join(_COLORS[s] for s in styles if s in _COLORS)
    return f"{prefix}{text}{RESET}" if prefix else text


def big_text(text: str, gap: int = 1) -> list[str]:
    """Render *text* in the block font, as :data:`GLYPH_HEIGHT` lines."""
    rows = [""] * GLYPH_HEIGHT
    separator = " " * gap
    for index, char in enumerate(text):
        glyph = _GLYPHS.get(char) or _GLYPHS.get(char.lower())
        if glyph is None:
            glyph = _GLYPHS[" "]
        for row in range(GLYPH_HEIGHT):
            rows[row] += ("" if index == 0 else separator) + glyph[row]
    return rows


def format_clock(remaining_ms: int | None) -> str:
    """Format a clock reading the way a range display shows it."""
    if remaining_ms is None:
        return "--:--"
    seconds = max(0, int(remaining_ms) // 1000)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_len(text: str) -> int:
    """Length of *text* as displayed, ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", text))


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _pad_between(left: str, right: str, width: int) -> str:
    """Left- and right-align two fragments on one line.

    Measures on visible width so that already-coloured fragments still line up.
    """
    left_width, right_width = visible_len(left), visible_len(right)
    if left_width + right_width + 1 > width and left_width == len(left):
        left = _truncate(left, max(0, width - right_width - 1))
        left_width = visible_len(left)
    return left + " " * max(1, width - left_width - right_width) + right


def _fit_shots(shots: list[str], room: int) -> str:
    """Join as many of the most recent shots as fit in *room* columns.

    Drops whole shots from the front rather than cutting a value in half -- a
    half-printed score is worse than a visible ellipsis.
    """
    if room <= 0 or not shots:
        return ""
    joined = "  ".join(shots)
    if len(joined) <= room:
        return joined
    kept: list[str] = []
    for shot in reversed(shots):
        candidate = "  ".join([shot, *kept])
        if len("… " + candidate) > room:
            break
        kept.insert(0, shot)
    return "… " + "  ".join(kept) if kept else ""


def _series_line(series: Series, active: bool, width: int) -> str:
    marker = "▸" if active else " "
    tag = " (sight)" if series.sight else ""
    label = f"{marker} {series.name or f'Series {series.index}'}{tag}"
    right = series.total.display()
    if series.total.inner is None and series.inner_count:
        # The range did not report an inner count for this series, so show the
        # one we counted from the shots rather than nothing.
        right += f" ({series.inner_count}x)"

    room = width - len(label) - len(right) - 4
    shots = _fit_shots(series.shot_display(), room)
    if shots:
        pad = max(1, width - len(label) - 2 - len(shots) - len(right))
        return f"{label}  {shots}" + " " * pad + right
    return _pad_between(label, right, width)


def render_lane(
    view: LaneView,
    width: int = 80,
    now: float | None = None,
    age: float | None = None,
    color: bool = False,
    max_series: int = 8,
) -> list[str]:
    """Render one firing point as a list of lines.

    *age* is the number of seconds since the feed last sent anything, which the
    footer shows so a stalled connection is visible rather than silent.
    """
    now = time.time() if now is None else now
    width = max(28, width)
    lines: list[str] = []
    c = lambda text, *styles: colorize(text, *styles, enabled=color)  # noqa: E731

    range_ = view.range
    result: LaneResult | None = view.result

    # -- header ---------------------------------------------------------
    if range_ is not None:
        header = range_.name
        if range_.host_name:
            header += f" · {range_.host_name}"
        if range_.relay:
            header += f" · relay {range_.relay}"
        if range_.practice:
            header += " · practice"
    else:
        header = "waiting for range…"
    lane_tag = c(f"LANE {view.lane}", "bold", "white")
    lines.append(_pad_between(c(_truncate(header, width - 10), "cyan"), lane_tag, width))
    lines.append(c("─" * width, "dim"))

    # -- who is on the point --------------------------------------------
    lines.append(c(_truncate(view.shooter_name, width), "bold", "white"))
    details = []
    shooter = view.shooter
    if shooter is not None:
        if shooter.club and shooter.club != view.shooter_name:
            details.append(shooter.club)
        if shooter.class_name:
            details.append(f"class {shooter.class_name}")
        if shooter.category:
            details.append(shooter.category)
    if range_ is not None:
        if range_.title:
            details.append(range_.title)
        elif range_.event:
            details.append(range_.event)
        if (shooter.gun if shooter else "") or (range_.gun_type if range_ else ""):
            details.append(((shooter.gun if shooter else "") or range_.gun_type).lower())
    lines.append(c(_truncate(" · ".join(details), width), "dim") if details else "")

    # -- the number that matters ----------------------------------------
    lines.append("")
    total = result.total if result is not None else None
    total_text = total.display() if total is not None else "--"
    # The parenthesised inner count does not fit the block font; show it beside.
    if total is not None and total.inner:
        total_text = f"{total.value:g}" if total.value is not None else "--"
    for row in big_text(total_text):
        lines.append(c(_truncate(row, width), "green" if result is not None else "dim"))

    caption = "TOTAL"
    if total is not None and total.inner:
        caption += f"   {total.inner} inner"
    if result is not None:
        caption += f"   {result.shot_count} shots"
    lines.append(c(caption, "dim"))

    # -- series ----------------------------------------------------------
    lines.append("")
    if result is not None and result.series:
        active = result.active_series
        active_index = active.index if active is not None else None
        series = result.series[-max_series:]
        for entry in series:
            is_active = entry.index == active_index
            text = _series_line(entry, is_active, width)
            lines.append(c(text, "yellow") if is_active else text)
    else:
        lines.append(c("no series yet", "dim"))

    # -- footer ----------------------------------------------------------
    lines.append("")
    lines.append(c("─" * width, "dim"))
    clock = result.clock if result is not None else None
    remaining = clock.remaining_ms(now * 1000) if clock is not None else None
    left = f"clock {format_clock(remaining)}"
    if remaining is not None:
        left += " running"
    elif clock is not None and clock.running:
        # The range reports a clock that is on but was never started.
        left += " standby"
    if age is None:
        right = "no data"
    elif age < 5:
        right = "live"
    else:
        right = f"{int(age)}s ago"
    style = "dim" if age is not None and age < 5 else "red"
    lines.append(_pad_between(c(left, "bold"), c(right, style), width))
    return lines


def render_lane_screen(
    view: LaneView,
    width: int = 80,
    height: int = 24,
    now: float | None = None,
    age: float | None = None,
    color: bool = False,
) -> str:
    """Render a full screen, padded and clipped to *height*."""
    lines = render_lane(view, width=width, now=now, age=age, color=color)
    if len(lines) > height:
        lines = lines[:height]
    lines += [""] * (height - len(lines))
    return "\n".join(lines)


#: Terminal control used to redraw a frame over the previous one.
HOME = "\x1b[H"
CLEAR_TO_EOL = "\x1b[K"
CLEAR_BELOW = "\x1b[J"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_SCREEN = "\x1b[2J\x1b[H"


def frame(
    view: LaneView,
    width: int,
    height: int,
    age: float | None = None,
    color: bool = False,
    interactive: bool = False,
    extra: str = "",
) -> str:
    """One frame of a live display, ready to write.

    On a terminal the frame is padded to the full height and drawn over the
    previous one -- redrawing in place rather than clearing first avoids the
    flicker a full clear causes on a slow framebuffer console, and stops the
    screen scrolling away every time it updates. Piped or redirected, frames are
    written one after another with no cursor control, so the output stays
    readable in a log.
    """
    lines = render_lane(view, width=width, age=age, color=color)
    if extra:
        lines.append(extra)
    return compose(lines, height, interactive)


def compose(lines: list[str], height: int, interactive: bool) -> str:
    """Lines as one frame: drawn in place on a terminal, appended in a log."""
    if not interactive:
        return "\n".join(lines) + "\n\n"
    if len(lines) > height:
        lines = lines[:height]
    lines += [""] * (height - len(lines))
    screen = "\n".join(lines)
    return HOME + screen.replace("\n", CLEAR_TO_EOL + "\n") + CLEAR_BELOW


def render_setup(
    reach: Any,
    width: int,
    height: int,
    name: str = "",
    interactive: bool = False,
    hotspot: dict[str, Any] | None = None,
    network_status: dict[str, Any] | None = None,
) -> list[str]:
    """What a console display with nothing configured shows instead.

    The address, and on a real terminal a QR code of it underneath when there
    is room -- drawn with background colours, which work on the Linux console
    whatever font it has. Not in a log, where the colour codes would be noise.
    With no address yet, what the hotspot fallback is about to do, from
    ``network_status``.
    """
    from . import network, qr

    if hotspot:
        return _render_hotspot(hotspot, reach, width, height, interactive)
    url = reach.url() if reach is not None else None
    lines = ["", "  SET UP THIS DISPLAY", ""]
    if url is None:
        lines += ["  Waiting for a network…", ""]
        for note in (network.waiting_note(network_status), "", network.CABLE_NOTE):
            lines += ["  " + part for part in textwrap.wrap(note, max(20, width - 4))] or [""]
        return [_truncate(line, width) for line in lines]
    lines += [f"  On a phone or laptop on the same network, open  {url}"]
    local = reach.local_url()
    if local:
        lines.append(f"  (or {local})")
    if name:
        lines += ["", f"  This display is called {name}."]
    lines = [_truncate(line, width) for line in lines]
    if interactive:
        code = qr.terminal(url)
        columns = 2 * len(qr.modules(url))
        if len(lines) + 1 + len(code) <= height and columns + 2 <= width:
            margin = " " * max(2, (width - columns) // 2)
            lines += [""] + [margin + row for row in code]
    return lines


def _render_hotspot(
    spot: dict[str, Any], reach: Any, width: int, height: int, interactive: bool
) -> list[str]:
    """The console's set-up screen on the display's own Wi-Fi.

    Only one code fits on a console, so it is the one for joining the network:
    that is the hard step, and the address after it is short enough to type.
    """
    from . import qr

    ssid, password = str(spot.get("ssid", "")), str(spot.get("password", ""))
    port = reach.port if reach is not None else 8080
    url = f"http://{spot.get('address') or '10.42.0.1'}{'' if port == 80 else f':{port}'}/"
    lines = [
        "",
        "  CONNECT TO THIS DISPLAY",
        "",
        f"  1. Join its Wi-Fi:  {ssid}",
        f"     password:        {password}" if password else "     (no password)",
        f"  2. Then open        {url}",
        "",
        "  Your phone may say there is no internet; stay connected anyway.",
    ]
    lines = [_truncate(line, width) for line in lines]
    if interactive:
        text = qr.wifi(ssid, password)
        code = qr.terminal(text)
        columns = 2 * len(qr.modules(text))
        if len(lines) + 1 + len(code) <= height and columns + 2 <= width:
            margin = " " * max(2, (width - columns) // 2)
            lines += [""] + [margin + row for row in code]
    return lines
