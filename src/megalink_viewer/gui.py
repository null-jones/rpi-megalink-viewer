"""A windowed per-position display: the target face and the score table.

Tkinter, because it is in the standard library and its canvas is enough to draw
a target properly -- nothing here needs a compiler, which is what keeps this
viable on a Pi Zero. On Raspberry Pi OS the toolkit itself comes from
``apt install python3-tk``.

The window is laid out the way the shooter wants to read it: the target face on
the left with the current series plotted on it, the running total and clock top
right, the shots of the current series listed beneath, and every series
summarised along the bottom.

The feed is followed in a background thread; Tk is only ever touched from the
main thread, which polls the shared state on a timer.
"""

from __future__ import annotations

import contextlib
import math
import time
from typing import Any

from . import address as reach_module
from . import network, qr
from .models import LaneResult, LaneView, Series, rank_on_relay
from .render import format_clock
from .targets import TargetFace

# Colours chosen to sit close to Megalink's own rendering.
PAPER = "#f4f2ea"
INK = "#1b1b1b"
CHROME = "#2b2b2b"
CHROME_TEXT = "#f2f2f2"
MUTED = "#9a9a9a"
SHOT = "#2fbf4f"
SHOT_EDGE = "#12511f"
LATEST = "#e03a2f"
LATEST_EDGE = "#6d150f"
ACCENT = "#4aa3df"
STALE = "#e0a02f"
#: The colour the grid rules show through as.
RULE = "#5a5e63"
PANEL = "#232527"

#: Shots smaller than this on screen are still drawn at this radius, so a
#: 300m target's 8mm holes stay visible.
MIN_HOLE_PX = 3.0

#: Ring lines stay hairlines however far the view zooms in.
MAX_RING_LINE_PX = 2

#: A ring number's point size as a fraction of the gap it sits in. Points are
#: three quarters of a pixel, so this fills roughly two thirds of the gap.
RING_NUMBER_SHARE = 0.5

#: How many series totals the strip across the top can show. Six, which is an
#: ISSF 10m card: six series of ten.
STRIP_CELLS = 6
#: The shot grid, holding one series rather than the whole card -- ten shots in
#: two columns of five, so an ISSF series fills the grid exactly and the shots
#: stop moving between cells as the series goes on.
SHOT_COLUMNS = 2
SHOT_ROWS = 5

#: The window size the type sizes below are quoted at. Everything scales from
#: the ratio between the real window and this, so enlarging the window enlarges
#: the score sheet instead of leaving small text in a big frame.
REFERENCE_SIZE = (940, 620)
#: How far the type is allowed to scale. The floor keeps a 480x320 Pi screen
#: legible; the ceiling stops a big monitor from turning the total into wallpaper.
SCALE_RANGE = (0.8, 2.4)

#: Type sizes at scale 1.0, by role.
TYPE = {
    "shooter": 21,
    "subtitle": 12,
    "badge": 17,
    "total": 54,
    "note": 13,
    "clock": 17,
    # The clock gets its own box on the card, at a size to be read from the
    # firing point -- unlike the strip captions, which share the "clock" role.
    "clock_box": 34,
    "section": 10,
    "table": 15,
    "value": 19,
    "heading": 11,
    "status": 11,
}


#: How much of a cell's width fitted type may fill, so it never touches the rule.
FIT_FILL = 0.9
#: The card panel's own left and right margins, in pixels.
CARD_PAD = 15
#: A grid cell's internal padding, both sides together.
CELL_PAD = 22

#: How often the address shown for setting a display up is looked up again. It
#: runs a command, and an address changes on the scale of minutes, not frames.
REACH_INTERVAL_S = 5.0
#: How much of the shorter side of the screen the setup code takes. Big enough
#: to scan from the far side of a firing point, small enough to leave room for
#: the address written out beside it.
SETUP_CODE_SHARE = 0.42
#: The same, when there are two codes side by side: one to join the display's
#: own Wi-Fi, one to open its settings once joined.
HOTSPOT_CODE_SHARE = 0.30

#: How often the relay standing is recomputed. Working it out means reading
#: every other firing point on the range, and it need not keep up with the shots.
STANDING_INTERVAL_MS = 2000


#: The colour of the mean-point-of-impact cross: not a shot colour, so it can
#: never be counted as one.
MPI = "#69b7ff"
#: Half the length of each arm of that cross, in pixels.
MPI_ARM_PX = 9
#: Shots needed before a mean point of impact means anything.
MPI_MIN_SHOTS = 2

#: Below this the group is called centred rather than given coordinates, because
#: a millimetre of offset is not a sight adjustment anybody can make.
MPI_DEADBAND_MM = 1.0


def describe_group(series: Series | None) -> str:
    """How tight the group is and where it sits, in millimetres.

    Two figures for the spread -- the mean distance from the group's own centre
    and the standard deviation of that distance -- and the centre itself as an
    offset from the middle of the target, signed the way the plot is: x to the
    right, y upwards. A pair of coordinates says which way to move a sight
    without anyone having to agree on what "low left" means.
    """
    if series is None:
        return ""
    centre = series.mpi_mm()
    if centre is None:
        return ""
    parts = []
    spread = series.radial_stats()
    if spread is not None:
        mean, deviation = spread
        parts.append(f"\u03bc {mean:.1f}mm")
        parts.append(f"\u03c3 {deviation:.1f}mm")
    x_mm, y_mm = centre
    if math.hypot(x_mm, y_mm) < MPI_DEADBAND_MM:
        parts.append("centred")
    else:
        parts.append(f"centre ({x_mm:+.1f}, {y_mm:+.1f}) mm")
    return " \u00b7 ".join(parts)


def _sighting(series: Series | None) -> bool:
    return series is not None and series.sight


def _ordinal(place: int) -> str:
    """1st, 2nd, 3rd -- including the teens, which do not follow the pattern."""
    if 11 <= place % 100 <= 13:
        return f"{place}th"
    return f"{place}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(place % 10, 'th') }"


#: How much of an idle screen the badge may take, across and down. The rest is
#: left for the message and the lane line underneath it.
LOGO_BOX = (0.72, 0.58)
#: Tk enlarges an image by whole numbers only, so a fractional scale means
#: zooming and then subsampling. This caps the zoom, and with it the size of the
#: intermediate copy -- a Pi Zero has 512MB and every pixel of it costs four bytes.
LOGO_ZOOM_MAX = 16
LOGO_SHRINK_MAX = 32
#: The most pixels the intermediate zoomed copy may reach.
LOGO_PIXELS_MAX = 4_000_000


def logo_scale(size: tuple[int, int], box: tuple[float, float]) -> tuple[int, int]:
    """The ``(zoom, subsample)`` pair that best fills ``box`` without spilling.

    Tk scales an image by whole numbers in either direction, so the way to get
    a badge to 1.8x is to zoom it 9 and subsample it 5. Zooming first is what
    costs the memory, so the zoom is capped and the pair that fills the most of
    the box within that cap wins. A badge smaller than its box gets enlarged --
    only ever shrinking it left a small file looking lost on a big screen.
    """
    width, height = size
    box_width, box_height = box
    if width <= 0 or height <= 0:
        return 1, 1
    best = (1, LOGO_SHRINK_MAX)
    best_area = 0
    for zoom in range(1, LOGO_ZOOM_MAX + 1):
        # The cap is on the zoomed copy, so it must never rule out zoom 1 --
        # doing that left a badge too big for the cap with no candidate at all,
        # and it fell back to the smallest thumbnail on offer.
        if zoom > 1 and width * zoom * height * zoom > LOGO_PIXELS_MAX:
            break
        for shrink in range(1, LOGO_SHRINK_MAX + 1):
            scaled_width = width * zoom // shrink
            scaled_height = height * zoom // shrink
            if scaled_width > box_width or scaled_height > box_height:
                continue
            area = scaled_width * scaled_height
            if area > best_area:
                best_area, best = area, (zoom, shrink)
    return best


class FittedType:
    """A font kept at the largest size that still fits the text set in it.

    The card total is why this exists. Its text runs from ``84`` to
    ``594.8 (23x)`` depending on the discipline, while the cell it sits in is a
    fixed half of the panel -- so a size chosen from the window alone either
    clips the long total or wastes the box on the short one. Megalink's own
    display sizes that figure to its box, and so does this.

    The box is passed in rather than measured off the widgets: an unmapped
    window reports every widget as one pixel wide, and a display that only sized
    its type correctly once shown would be a poor thing to have to test.
    """

    #: Never smaller than this, whatever it takes -- clipped beats invisible.
    MINIMUM = 8

    def __init__(self, font: Any, role: str) -> None:
        self.font = font
        #: Which entry in :data:`TYPE` gives this font its upper limit.
        self.role = role
        self.widgets: list[Any] = []

    def fit(self, ceiling: int, width: float) -> None:
        """Set the font as large as ``ceiling`` allows, and small enough to fit."""
        ceiling = max(self.MINIMUM, ceiling)
        self.font.configure(size=ceiling)
        room = width * FIT_FILL
        texts = [text for text in (w.cget("text") for w in self.widgets) if text]
        if not texts or room <= 0:
            return
        # Text width goes very nearly linearly with type size, so dividing by
        # the worst overrun lands on the answer in a pass or two -- rather than
        # stepping down a point at a time and re-laying-out the window at each.
        for _ in range(6):
            worst = max(self.font.measure(text) for text in texts) / room
            if worst <= 1.0:
                return
            size = max(self.MINIMUM, int(self.font.cget("size") / worst))
            if size == self.font.cget("size"):
                return
            self.font.configure(size=size)


def _import_tk() -> tuple[Any, Any, Any]:
    """Import Tkinter, with a message that says how to install it if missing."""
    try:
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import ttk
    except ImportError as exc:  # pragma: no cover - depends on the OS build
        raise RuntimeError(
            "Tkinter is not available in this Python build. On Raspberry Pi OS "
            "or Debian install it with: sudo apt install python3-tk"
        ) from exc
    return tk, ttk, tkfont


def scale_for(width: int, height: int) -> float:
    """How much to enlarge the type for a window of this size."""
    reference_width, reference_height = REFERENCE_SIZE
    raw = min(width / reference_width, height / reference_height)
    low, high = SCALE_RANGE
    return max(low, min(high, raw))


def type_sizes(scale: float) -> dict[str, int]:
    """Type size per role at a given scale, never below something readable."""
    return {role: max(8, round(size * scale)) for role, size in TYPE.items()}


class TargetCanvas:
    """Draws a target face and the shots on it.

    Painted in the order Megalink paints a face -- paper, aiming area, centre --
    then the shots, and then the ring lines *over* the shots. That last step is
    what keeps a tight group readable: the rings stay unbroken across the
    markers, so a shot can still be judged against them, while each marker stays
    a solid colour rather than a washed-out one.

    The shots are not numbered here. The order they were fired in is in the grid
    beside the target; repeating it over the plot buries the group under digits,
    which is the one thing the plot is for.
    """

    def __init__(self, tk: Any, parent: Any, background: str = CHROME) -> None:
        self._tk = tk
        self.widget = tk.Canvas(parent, bg=background, highlightthickness=0, width=420, height=420)
        self._background = background
        self._font_cache: dict[tuple[int, bool], Any] = {}
        self._family: str | None = None

    def font(self, size: int, bold: bool = False) -> Any:
        """A real font at this size, cached.

        Built from Tk's own default font: "TkDefaultFont" names a font, not a
        family, and passing it as a family leaves the text unrendered wherever
        the platform has nothing to fall back to.
        """
        key = (size, bold)
        cached = self._font_cache.get(key)
        if cached is not None:
            return cached
        from tkinter import font as tkfont

        if self._family is None:
            self._family = tkfont.nametofont("TkDefaultFont", root=self.widget).actual("family")
        made = tkfont.Font(
            root=self.widget,
            family=self._family,
            size=size,
            weight="bold" if bold else "normal",
        )
        self._font_cache[key] = made
        return made

    # -- geometry ----------------------------------------------------------

    def size(self) -> tuple[int, int]:
        """The widget's size in pixels.

        Before the window is mapped Tk reports 1x1, so fall back to whatever
        size the canvas was configured with -- the first frame is drawn before
        the layout settles.
        """
        width = self.widget.winfo_width()
        height = self.widget.winfo_height()
        if width <= 1:
            width = int(self.widget["width"])
        if height <= 1:
            height = int(self.widget["height"])
        return width, height

    def _viewport(self, extent_mm: float) -> tuple[float, float, float]:
        """Centre point and pixels-per-millimetre for the current widget size."""
        width, height = self.size()
        size = max(40, min(width, height))
        # A small margin keeps the outermost ring and its numbers inside.
        scale = (size / 2.0 - 6) / max(extent_mm, 0.001)
        return width / 2.0, height / 2.0, scale

    def draw(self, series: Series | None) -> None:
        canvas = self.widget
        canvas.delete("all")

        if series is None:
            self._message("waiting for a series…")
            return

        face = series.face
        shots = series.positions_mm()
        if face is None:
            self._plotless(series, shots)
            return

        extent = self._plot_extent(face, shots)
        cx, cy, scale = self._viewport(extent)
        self._face_fills(face, cx, cy, scale)
        # Ring numbers belong to the paper: a shot lands on top of them, the way
        # it does on a real target.
        self._ring_numbers(face, cx, cy, scale)
        self._markers(series, shots, cx, cy, scale)
        # The ring lines go over everything, so no ring is ever hidden by a
        # group of shots and a shot can still be judged against them.
        self._ring_lines(face, cx, cy, scale)
        self._mpi(series, shots, cx, cy, scale)

    def _mpi(self, series: Series, shots: list, cx: float, cy: float, scale: float) -> None:
        """A cross at the mean point of impact.

        The number under the target says how far the group has gone and which
        way; this says it where the eye already is. Drawn as an open cross so it
        cannot be mistaken for a shot, and only once there is a group to have a
        middle -- with one shot on the paper the MPI is just that shot.
        """
        if len(shots) < MPI_MIN_SHOTS:
            return
        centre = series.mpi_mm()
        if centre is None:
            return
        x = cx + centre[0] * scale
        y = cy - centre[1] * scale
        arm = MPI_ARM_PX
        self.widget.create_line(x - arm, y, x + arm, y, fill=MPI, width=1)
        self.widget.create_line(x, y - arm, x, y + arm, fill=MPI, width=1)

    def _plot_extent(self, face: TargetFace, shots: list) -> float:
        """How far out to draw: Megalink's auto-zoom.

        A tight group is shown close up and the view opens as the group spreads,
        within the zoom range the target table gives for this face. A stray shot
        beyond even the widest view still gets included rather than falling off
        the edge -- better a small target than a shot you cannot see.
        """
        reach = 0.0
        for _, x, y in shots:
            reach = max(reach, math.hypot(x, y))
        if not reach:
            # Nothing shot yet: show the whole face rather than zooming into an
            # empty middle.
            return face.extent
        # Never crop a shot that landed outside the widest zoom.
        return max(face.view_radius(reach), reach * 1.05)

    def _face_fills(self, face: TargetFace, cx: float, cy: float, scale: float) -> None:
        """The paper, the black aiming area and any centre disc."""
        canvas = self.widget

        def disc(radius_mm: float, fill: str) -> None:
            r = radius_mm * scale
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=fill)

        backing = face.backing
        disc(
            backing[0] if backing else face.outer_radius,
            _color(backing[1]) if backing else PAPER,
        )
        aim = face.aiming_mark
        if aim:
            disc(aim[0], _color(aim[1]))
        centre = face.centre
        if centre:
            disc(centre[0], _color(centre[1]))

    def _ring_lines(self, face: TargetFace, cx: float, cy: float, scale: float) -> None:
        """The ring lines, in the colours the face specifies."""
        canvas = self.widget
        # Megalink draws these as hairlines whatever the zoom. Converting the
        # face's millimetre width faithfully gives a 7px stripe at close zoom,
        # which swamps the target.
        width = max(1, min(MAX_RING_LINE_PX, round(face.line_width * scale)))
        for ring in face.rings():
            if not ring.drawn:
                continue
            r = ring.radius * scale
            if r < 1.5:
                continue
            canvas.create_oval(
                cx - r, cy - r, cx + r, cy + r, outline=_color(ring.color), width=width
            )

    def _ring_numbers(self, face: TargetFace, cx: float, cy: float, scale: float) -> None:
        """Print ring numbers where the face says they belong.

        Megalink prints them midway between a ring and the next one in, on the
        axes the face specifies. They are dropped entirely when the rings are
        drawn too close together to fit a legible digit -- an air rifle face
        squeezed into a small pane is better bare than illegible.
        """
        canvas = self.widget
        radii = {ring.number: ring.radius for ring in face.rings()}
        offsets = face.number_offsets()
        for ring in face.rings():
            if not ring.numbered:
                continue
            inner = radii.get(ring.number + 1, 0.0)
            gap_px = (ring.radius - inner) * scale
            at = (ring.radius + inner) / 2.0 * scale
            # Needs room for the digit both radially and away from the centre.
            if gap_px < 11 or at < 10:
                continue
            # Sized to the gap it sits in, as on the paper. The old cap of 13
            # points left them unreadable across a firing point.
            size = max(7, int(gap_px * RING_NUMBER_SHARE))
            font = self.font(size)
            for dx, dy in offsets:
                canvas.create_text(
                    cx + dx * at,
                    cy + dy * at,
                    text=str(ring.number),
                    fill=_color(ring.number_color),
                    font=font,
                )

    def _hole_radius(self, series: Series, scale: float) -> float:
        return max(MIN_HOLE_PX, series.hole_radius * scale)

    def _markers(self, series: Series, shots: list, cx: float, cy: float, scale: float) -> None:
        """One solid marker per shot, newest in its own colour."""
        if not shots:
            return
        radius = self._hole_radius(series, scale)
        for index, (_shot, x_mm, y_mm) in enumerate(shots):
            latest = index == len(shots) - 1
            fill, edge = (LATEST, LATEST_EDGE) if latest else (SHOT, SHOT_EDGE)
            # A target's y grows upwards; a canvas's grows downwards.
            self._marker(cx + x_mm * scale, cy - y_mm * scale, radius, fill, edge)

    def _marker(self, x: float, y: float, radius: float, fill: str, edge: str) -> None:
        self.widget.create_oval(
            x - radius, y - radius, x + radius, y + radius, fill=fill, outline=edge, width=1
        )

    def _plotless(self, series: Series, shots: list) -> None:
        """Fallback for a face this package does not carry.

        The hunting and field silhouettes are arbitrary artwork rather than
        rings, so their shots are plotted on a plain grid instead of a face --
        better than refusing to show anything.
        """
        canvas = self.widget
        cx, cy, _ = self._viewport(1.0)
        span = max((math.hypot(x, y) for _, x, y in shots), default=1.0) or 1.0
        width, height = self.size()
        scale = (max(40, min(width, height)) / 2.0 - 10) / (span * 1.1)
        canvas.create_line(cx - 200, cy, cx + 200, cy, fill=MUTED, dash=(2, 4))
        canvas.create_line(cx, cy - 200, cx, cy + 200, fill=MUTED, dash=(2, 4))
        for index, (_, x_mm, y_mm) in enumerate(shots):
            latest = index == len(shots) - 1
            fill, edge = (LATEST, LATEST_EDGE) if latest else (SHOT, SHOT_EDGE)
            self._marker(cx + x_mm * scale, cy - y_mm * scale, 5.0, fill, edge)
        label = series.target_name or series.target_id or "unknown target"
        canvas.create_text(cx, 14, text=f"no face for {label}", fill=STALE, font=self.font(9))

    def _message(self, text: str) -> None:
        cx, cy, _ = self._viewport(1.0)
        self.widget.create_text(cx, cy, text=text, fill=MUTED, font=self.font(11))


def _color(name: str) -> str:
    """Map the table's colour names onto the palette used here."""
    if name == "white":
        return PAPER
    if name == "black":
        return INK
    return name


class LaneWindow:
    """The window: header, target face, shot list and series table."""

    def __init__(
        self,
        state: Any,
        lane: str,
        title: str = "Megalink",
        interval_ms: int = 500,
        fullscreen: bool = False,
        on_lane_change: Any = None,
        should_stop: Any = None,
        lane_source: Any = None,
        parent: Any = None,
        find_reach: Any = None,
        read_network: Any = None,
    ) -> None:
        tk, ttk, tkfont = _import_tk()
        self._tk = tk
        self._tkfont = tkfont
        self._state = state
        self._lane = str(lane)
        self._interval_ms = max(100, interval_ms)
        self._series_choice: int | None = None
        # Set when the display is configured centrally: changing the firing
        # point at the screen should be remembered, not lost on the next redraw.
        self._on_lane_change = on_lane_change
        # Checked on every redraw. A signal handler cannot safely touch Tk from
        # inside the main loop, so shutting down means setting a flag and
        # letting the next frame notice -- which is how systemd's SIGTERM gets
        # this window closed instead of being ignored until it is killed.
        self._should_stop = should_stop
        #: Where this window's firing point is configured, so a change made from
        #: the fleet dashboard reaches a window that is already open. Without it
        #: the dashboard writes the file, reports success, and the screen carries
        #: on showing the firing point it started with.
        self._lane_source = lane_source
        self._configured_lane = str(lane)

        # A second window shares the first one's interpreter and main loop
        # rather than starting a second process: one feed, one beacon, one entry
        # in the fleet, two screens.
        self.root = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.root.title(title)
        self.root.configure(bg=CHROME)
        self.root.minsize(640, 420)
        self._fullscreen = False
        if fullscreen:
            self.toggle_fullscreen()

        self._style(ttk)
        self._fonts = self._make_fonts(tkfont)
        self._fitted = {
            # The shot numbers fill the height of their row, and the two totals
            # fill the width of theirs.
            "value": FittedType(self._clone(tkfont, "value"), "value"),
            # One font across both totals: fitting them separately would size
            # them differently whenever their texts differed in length, and two
            # neighbouring figures at two sizes reads as a mistake.
            "totals": FittedType(self._clone(tkfont, "total"), "total"),
            "clock_box": FittedType(self._clone(tkfont, "clock_box"), "clock_box"),
        }
        self._scale = 1.0
        self._standing_at = float("-inf")
        self._standing_text = ""
        #: How to work out this display's address; injectable for tests, which
        #: should not depend on the network of whichever machine runs them.
        self._find_reach = find_reach or reach_module.find
        self._reach: Any = None
        self._reach_at = float("-inf")
        #: What the hotspot fallback says it is doing, read from its status file.
        self._read_network = read_network or network.read_status
        #: "screen 1" or "screen 2" on a Pi showing a firing point on each of
        #: its HDMI outputs, said when the display is asked to identify itself.
        self.screen_label = ""
        self._network: dict[str, Any] | None = None
        self._codes_drawn: dict[str, tuple[str, int]] = {}
        self._last_size = (0, 0)
        self._build(tk, ttk)
        self._bind()
        self.rescale(force=True)

    # -- construction ------------------------------------------------------

    def _make_fonts(self, tkfont: Any) -> dict[str, Any]:
        """One font object per role, resized in place when the window changes.

        Tk widgets that are given a font *object* follow it, so resizing these
        re-lays-out the whole score sheet without touching a single widget.
        """
        sizes = type_sizes(1.0)
        weights = {
            "shooter": "bold",
            "badge": "bold",
            "total": "bold",
            "clock": "bold",
            "clock_box": "bold",
            "section": "bold",
            "value": "bold",
        }
        roles = dict(sizes)

        # Copy Tk's own default font rather than asking for a family called
        # "TkDefaultFont". That is the name of a font, not of a family, and
        # asking for it by family gets whatever the platform falls back to --
        # which on macOS is something sensible and on a bare X11 install can be
        # nothing at all, leaving every widget drawn but empty.
        base = tkfont.nametofont("TkDefaultFont", root=self.root)
        family = base.actual("family")
        return {
            role: tkfont.Font(
                root=self.root,
                family=family,
                size=size,
                weight=weights.get(role, "normal"),
            )
            for role, size in roles.items()
        }

    def _clone(self, tkfont: Any, role: str) -> Any:
        """A second font object matching one of the roles, resized on its own."""
        source = self._fonts[role]
        return tkfont.Font(
            root=self.root,
            family=source.cget("family"),
            size=source.cget("size"),
            weight=source.cget("weight"),
        )

    def _window_size(self) -> tuple[int, int]:
        """The window's size, falling back before it has ever been laid out."""
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        if width <= 1 or height <= 1:
            return REFERENCE_SIZE
        return width, height

    def rescale(self, force: bool = False) -> None:
        """Resize the type to suit the window.

        Called on every resize, but only does the work when the size has really
        changed -- Tk emits ``<Configure>`` freely, and re-laying-out the tables
        on each one would make dragging a window edge feel like treacle.
        """
        width, height = self._window_size()
        if not force and (width, height) == self._last_size:
            return
        self._last_size = (width, height)
        self._scale = scale_for(width, height)
        sizes = type_sizes(self._scale)
        for role, font in self._fonts.items():
            font.configure(size=sizes[role])
        self._fit_type()
        # Rows have to grow with the type or the table clips it.
        row_height = round(sizes["table"] * 1.9)
        self._ttk_style.configure("Megalink.Treeview", rowheight=row_height)
        self._ttk_style.configure("Megalink.Treeview", font=self._fonts["table"])
        self._ttk_style.configure("Megalink.Treeview.Heading", font=self._fonts["heading"])

    def _fit_type(self) -> None:
        """Re-fit the type that is sized to its box rather than to the window."""
        sizes = type_sizes(self._scale)
        panel = max(1.0, self._window_size()[0] / 2 - CARD_PAD)
        boxes = {
            # Two totals side by side across the card panel.
            "totals": panel / 2,
            "clock_box": panel,
            "value": panel / SHOT_COLUMNS - CELL_PAD,
        }
        for name, fitted in self._fitted.items():
            fitted.fit(sizes[fitted.role], boxes[name])

    def _style(self, ttk: Any) -> None:
        style = ttk.Style()
        self._ttk_style = style
        # "clam" is present everywhere and takes colour settings, which the
        # native themes on some platforms quietly ignore.
        with_clam = "clam" in style.theme_names()
        if with_clam:
            style.theme_use("clam")
        style.configure(
            "Megalink.Treeview",
            background=CHROME,
            fieldbackground=CHROME,
            foreground=CHROME_TEXT,
            borderwidth=0,
            rowheight=22,
        )
        style.configure(
            "Megalink.Treeview.Heading",
            background="#3a3a3a",
            foreground=MUTED,
            borderwidth=0,
        )
        style.map("Megalink.Treeview", background=[("selected", "#454545")])

    def _build(self, tk: Any, ttk: Any) -> None:
        root = self.root
        root.columnconfigure(0, weight=1, uniform="halves", minsize=280)
        root.columnconfigure(1, weight=1, uniform="halves", minsize=280)
        root.rowconfigure(2, weight=1)

        self._build_header(tk)
        self._build_series_strip(tk)

        # -- target ---------------------------------------------------------
        left = tk.Frame(root, bg=CHROME)
        left.grid(row=2, column=0, sticky="nsew", padx=(10, 5), pady=6)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self._canvas = TargetCanvas(tk, left)
        self._canvas.widget.grid(row=0, column=0, sticky="nsew")
        self._canvas.widget.bind("<Configure>", lambda _event: self.refresh(poll=False))
        self._target_label = tk.Label(
            left, text="", bg=CHROME, fg=MUTED, font=self._fonts["subtitle"], anchor="w"
        )
        self._target_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        # What the group is doing, which the card cannot say: a tight group in
        # the wrong place scores exactly like a loose one in the right place.
        self._group_label = tk.Label(
            left, text="", bg=CHROME, fg=CHROME_TEXT, font=self._fonts["note"], anchor="w"
        )
        self._group_label.grid(row=2, column=0, sticky="w")

        # -- the card -------------------------------------------------------
        right = tk.Frame(root, bg=CHROME)
        right.grid(row=2, column=1, sticky="nsew", padx=(5, 10), pady=6)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self._build_shot_table(tk, right)
        self._build_totals(tk, right)

        self._build_overlays(tk)

        # -- footer ---------------------------------------------------------
        self._status = tk.Label(
            root,
            text="connecting…",
            bg=CHROME,
            fg=MUTED,
            font=self._fonts["status"],
            anchor="w",
        )
        self._status.grid(row=3, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 8))

    def _build_overlays(self, tk: Any) -> None:
        """Two things that cover the card rather than sit in it.

        Placed over the layout instead of gridded into it, so showing or hiding
        either never reflows anything underneath.
        """
        root = self.root

        # Nobody on this firing point. Better than an empty card, which reads
        # as a broken display.
        self._idle = tk.Frame(root, bg=CHROME)
        # The badge and the message go in a block of their own, placed at the
        # middle of the cover. Packed straight into the cover they would sit
        # against its top edge, which on a full screen leaves the badge stranded
        # in the upper third.
        self._idle_stack = tk.Frame(self._idle, bg=CHROME)
        self._idle_stack.place(relx=0.5, rely=0.5, anchor="center")
        self._idle_logo = tk.Label(self._idle_stack, bg=CHROME, borderwidth=0)
        self._idle_text = tk.Label(
            self._idle_stack, text="", bg=CHROME, fg=MUTED, font=self._fonts["shooter"]
        )
        self._idle_lane = tk.Label(
            self._idle_stack, text="", bg=CHROME, fg=MUTED, font=self._fonts["subtitle"]
        )
        self._logo_image: Any = None
        self._logo_source: tuple[str, float, int, int] | None = None

        # Where to find this display's settings, small, for anyone who comes
        # to change it. Only on the idle cover, where it is in nobody's way.
        self._idle_reach = tk.Label(
            self._idle, text="", bg=CHROME, fg=MUTED, font=self._fonts["subtitle"]
        )

        # A display that has not been told what to show. The whole screen goes
        # to saying how to set it up, because the person looking at it has
        # probably never done this before and has no keyboard to try things.
        self._setup = tk.Frame(root, bg=CHROME)
        stack = tk.Frame(self._setup, bg=CHROME)
        stack.place(relx=0.5, rely=0.5, anchor="center")
        self._setup_title = tk.Label(
            stack, text="Set up this display", bg=CHROME, fg=CHROME_TEXT, font=self._fonts["total"]
        )
        self._setup_title.pack(pady=(0, 18))
        row = tk.Frame(stack, bg=CHROME)
        row.pack()
        # Gridded rather than packed, so either code can be taken away and put
        # back in its own place: grid_remove remembers where a widget went.
        # White, and square: a scanner needs the light quiet zone round a code.
        self._setup_code = tk.Canvas(row, bg="#ffffff", highlightthickness=0, width=1, height=1)
        self._setup_code.grid(row=0, column=0, padx=(0, 28))
        words = tk.Frame(row, bg=CHROME)
        words.grid(row=0, column=1, sticky="w")
        self._setup_words = words
        self._setup_lead = tk.Label(
            words,
            text="",
            bg=CHROME,
            fg=CHROME_TEXT,
            font=self._fonts["shooter"],
            justify="left",
            anchor="w",
        )
        self._setup_lead.pack(anchor="w")
        self._setup_url = tk.Label(
            words, text="", bg=CHROME, fg=ACCENT, font=self._fonts["badge"], anchor="w"
        )
        self._setup_url.pack(anchor="w", pady=(10, 2))
        self._setup_local = tk.Label(
            words, text="", bg=CHROME, fg=MUTED, font=self._fonts["subtitle"], anchor="w"
        )
        self._setup_local.pack(anchor="w")
        self._setup_note = tk.Label(
            words,
            text="",
            bg=CHROME,
            fg=MUTED,
            font=self._fonts["subtitle"],
            justify="left",
            anchor="w",
        )
        self._setup_note.pack(anchor="w", pady=(16, 0))
        # The second step, shown only on the display's own hotspot: once a
        # phone has joined it, the code to open the settings page.
        self._setup_code2 = tk.Canvas(row, bg="#ffffff", highlightthickness=0, width=1, height=1)
        self._setup_code2.grid(row=0, column=2, padx=(48, 28))
        self._setup_step2 = tk.Label(
            row,
            text="",
            bg=CHROME,
            fg=CHROME_TEXT,
            font=self._fonts["shooter"],
            justify="left",
            anchor="w",
        )
        self._setup_step2.grid(row=0, column=3, sticky="w")
        self._setup_code2.grid_remove()
        self._setup_step2.grid_remove()

        # Twenty screens in a row look alike; this is how the fleet dashboard
        # points at one of them.
        self._identify = tk.Label(
            root,
            text="",
            bg=ACCENT,
            fg="#0d1b24",
            font=self._fonts["shooter"],
            padx=16,
            pady=8,
        )

    def _build_header(self, tk: Any) -> None:
        """Range and shooter on the left, club in the middle, lane on the right."""
        header = tk.Frame(self.root, bg=CHROME)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4))
        header.columnconfigure(1, weight=1)

        left = tk.Frame(header, bg=CHROME)
        left.grid(row=0, column=0, sticky="w")
        self._range_line = tk.Label(
            left, text="", bg=CHROME, fg=MUTED, font=self._fonts["subtitle"], anchor="w"
        )
        self._range_line.pack(anchor="w")
        self._shooter = tk.Label(
            left, text="", bg=CHROME, fg=CHROME_TEXT, font=self._fonts["shooter"], anchor="w"
        )
        self._shooter.pack(anchor="w")

        self._host = tk.Label(
            header, text="", bg=CHROME, fg=CHROME_TEXT, font=self._fonts["shooter"]
        )
        self._host.grid(row=0, column=1, sticky="ew")

        right = tk.Frame(header, bg=CHROME)
        right.grid(row=0, column=2, sticky="e")
        self._lane_badge = tk.Label(
            right, text="", bg=ACCENT, fg="#0d1b24", font=self._fonts["badge"], padx=14, pady=2
        )
        self._lane_badge.pack(anchor="e")
        # Where the clock used to sit. The clock has its own box on the card
        # now, and a placing is the sort of thing that belongs up here beside
        # the lane number rather than among the scores.
        self._standing = tk.Label(
            right, text="", bg=CHROME, fg=MUTED, font=self._fonts["subtitle"], anchor="e"
        )
        self._standing.pack(anchor="e")

    def _build_series_strip(self, tk: Any) -> None:
        """The card's series and their totals, across the top as Megalink has it."""
        # The frame's own colour shows through the one-pixel gaps between cells,
        # which is what draws the rules -- widget borders will not line up
        # across a grid, and this always does.
        strip = tk.Frame(self.root, bg=RULE)
        strip.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(2, 6))
        self._series_strip = strip
        self._strip_name = tk.Label(
            strip,
            text="",
            bg=PANEL,
            fg=CHROME_TEXT,
            font=self._fonts["clock"],
            anchor="w",
            padx=10,
            pady=2,
        )
        self._strip_name.grid(row=0, column=0, sticky="nsew", padx=(0, 1), pady=(0, 1))
        strip.columnconfigure(0, weight=3, uniform="strip")
        self._strip_cells: list[Any] = []
        for column in range(1, STRIP_CELLS + 1):
            cell = tk.Label(
                strip,
                text="",
                bg=PANEL,
                fg=CHROME_TEXT,
                font=self._fonts["clock"],
                anchor="e",
                padx=10,
                pady=2,
            )
            cell.grid(row=0, column=column, sticky="nsew", padx=(0, 1), pady=(0, 1))
            strip.columnconfigure(column, weight=1, uniform="strip")
            self._strip_cells.append(cell)

    def _build_shot_table(self, tk: Any, parent: Any) -> None:
        """A ruled grid of shots, filled column by column."""
        table = tk.Frame(parent, bg=RULE)
        table.grid(row=0, column=0, sticky="nsew")
        self._shot_table = table
        self._shot_cells: list[list[Any]] = []
        for column in range(SHOT_COLUMNS):
            table.columnconfigure(column, weight=1, uniform="cells")
            cells = []
            for row in range(SHOT_ROWS):
                cell = tk.Label(
                    table,
                    text="",
                    bg=CHROME,
                    fg=CHROME_TEXT,
                    font=self._fitted["value"].font,
                    anchor="w",
                    width=1,
                    padx=10,
                    pady=2,
                )
                cell.grid(row=row, column=column, sticky="nsew", padx=(0, 1), pady=(0, 1))
                self._fitted["value"].widgets.append(cell)
                cells.append(cell)
            self._shot_cells.append(cells)
        for row in range(SHOT_ROWS):
            table.rowconfigure(row, weight=1, uniform="cells")

    def _build_totals(self, tk: Any, parent: Any) -> None:
        """The two figures a shooter looks for: this series, and the card."""
        totals = tk.Frame(parent, bg=RULE)
        totals.grid(row=1, column=0, sticky="ew", pady=(1, 0))
        totals.columnconfigure(0, weight=1, uniform="totals")
        totals.columnconfigure(1, weight=1, uniform="totals")
        self._series_total = tk.Label(
            totals,
            text="",
            bg=PANEL,
            fg=CHROME_TEXT,
            font=self._fitted["totals"].font,
            anchor="center",
            width=1,
            pady=2,
        )
        self._fitted["totals"].widgets.append(self._series_total)
        self._series_total.grid(row=0, column=0, sticky="nsew", padx=(0, 1))
        # Reversed, as Megalink reverses it: the card total is the one that
        # matters most and it is the one picked out.
        self._card_total = tk.Label(
            totals,
            text="",
            bg=CHROME_TEXT,
            fg=INK,
            font=self._fitted["totals"].font,
            anchor="center",
            width=1,
            pady=2,
        )
        self._fitted["totals"].widgets.append(self._card_total)
        self._card_total.grid(row=0, column=1, sticky="nsew")

        # The clock, given its own box. During a timed match it is the second
        # thing a shooter looks for after the score, and the one thing they
        # cannot get by looking downrange through a scope.
        self._clock = tk.Label(
            parent,
            text="",
            bg=PANEL,
            fg=MUTED,
            font=self._fitted["clock_box"].font,
            anchor="center",
            width=1,
            pady=2,
        )
        self._fitted["clock_box"].widgets.append(self._clock)
        self._clock.grid(row=2, column=0, sticky="ew", pady=(1, 0))

    def _bind(self) -> None:
        self.root.bind("<Escape>", lambda _e: self.close())
        self.root.bind("q", lambda _e: self.close())
        self.root.bind("f", lambda _e: self.toggle_fullscreen())
        self.root.bind("<F11>", lambda _e: self.toggle_fullscreen())
        self.root.bind("<Left>", lambda _e: self.step_lane(-1))
        self.root.bind("<Right>", lambda _e: self.step_lane(1))
        self.root.bind("<Up>", lambda _e: self.step_series(-1))
        self.root.bind("<Down>", lambda _e: self.step_series(1))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Configure>", self._on_configure)

    def _on_configure(self, event: Any) -> None:
        # Only the toplevel's own resizes matter; child widgets emit this too.
        if event.widget is self.root:
            self.rescale()

    # -- interaction -------------------------------------------------------

    def place_on(self, screen: Any) -> None:
        """Fill one output exactly.

        Set directly rather than through a window manager, because a kiosk
        window manager makes a window fill the whole X screen -- and across two
        HDMI sockets that is *both* monitors. Both firing points would land on
        one screen with the other left blank.
        """
        self._fullscreen = False
        with contextlib.suppress(Exception):
            self.root.attributes("-fullscreen", False)
        with contextlib.suppress(Exception):
            self.root.overrideredirect(True)
        self.root.geometry(screen.geometry)
        with contextlib.suppress(Exception):
            self.root.update_idletasks()
        self.rescale(force=True)

    def toggle_fullscreen(self) -> None:
        self._fullscreen = not getattr(self, "_fullscreen", False)
        with contextlib.suppress(Exception):
            # A hint to the window manager, and nothing more.
            self.root.attributes("-fullscreen", self._fullscreen)
        if not self._fullscreen:
            return
        # A display beside a firing point runs on a bare X server with no window
        # manager at all, and there is nothing there to act on that hint. Size
        # and place the window ourselves so it covers the screen either way.
        with contextlib.suppress(Exception):
            width = self.root.winfo_screenwidth()
            height = self.root.winfo_screenheight()
            self.root.geometry(f"{width}x{height}+0+0")
            # Nothing is going to map or raise this for us either, so do it.
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()

    def describe(self) -> str:
        """Where the window actually ended up, for the log.

        A display that says it opened a window and shows nothing has usually put
        one somewhere unhelpful -- unmapped, one pixel across, off the side of
        the screen. Reporting the geometry turns that from a guess into a fact.
        """
        try:
            self.root.update_idletasks()
            self.root.update()
            return (
                f"window {self.root.winfo_geometry()} "
                f"mapped={bool(self.root.winfo_ismapped())} "
                f"viewable={bool(self.root.winfo_viewable())} "
                f"screen={self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()} "
                f"fullscreen={self._fullscreen}"
            )
        except Exception as exc:  # pragma: no cover - defensive
            return f"window state unavailable: {exc}"

    def step_lane(self, delta: int) -> None:
        lanes = self._state.lanes()
        if self._lane not in lanes:
            return
        index = (lanes.index(self._lane) + delta) % len(lanes)
        self.set_lane(lanes[index])

    def set_lane(self, lane: str) -> None:
        if self._on_lane_change is not None and str(lane) != self._lane:
            with contextlib.suppress(Exception):
                self._on_lane_change(str(lane))
        self._lane = str(lane)
        self._lane_badge.configure(text=f" {self._lane} ")
        # A different shooter's card has its own series; follow the active one.
        self._series_choice = None
        self.refresh(poll=False)

    def step_series(self, delta: int) -> None:
        """Look back through the card's series, or return to the live one.

        The series are no longer a clickable list -- Megalink's layout has no
        room for one -- so this is how you get at an earlier series on a display
        with no mouse.
        """
        view = self._state.lane_view(self._lane)
        result = view.result
        if result is None:
            return
        groups = result.display_series
        if not groups:
            return
        live = self._live_position(result, groups)
        position = self._series_choice if self._series_choice is not None else live
        position = max(0, min(len(groups) - 1, position + delta))
        # Landing back on the group being shot means following it again.
        self._series_choice = None if position == live else position
        self.refresh(poll=False)

    @staticmethod
    def _live_position(result: LaneResult, groups: list[Series]) -> int:
        """Which of the display groups is the one being shot."""
        active = result.active_display_group
        if active is not None:
            for position, group in enumerate(groups):
                # Matched on where it sits in the card, not on identity: each
                # call to groups() builds fresh objects, so the active group is
                # never the same object as the one in this list.
                if group.index == active.index and group.offset == active.offset:
                    return position
        return len(groups) - 1

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.root.destroy()

    # -- updating ----------------------------------------------------------

    def _chosen_series(self, view: LaneView) -> Series | None:
        """The group of shots on show: the live one, or one stepped back to.

        Groups, not series: a range that publishes a 60-shot card as one series
        is showing six groups of ten on its own screen, and a display that piled
        all sixty onto one target would be unreadable long before the card was
        finished.
        """
        result = view.result
        if result is None:
            return None
        groups = result.display_series
        if not groups:
            return None
        if self._series_choice is not None and 0 <= self._series_choice < len(groups):
            return groups[self._series_choice]
        return result.active_display_group or groups[-1]

    @property
    def lane(self) -> str:
        """The firing point this window is showing."""
        return self._lane

    def follow_config(self) -> None:
        """Adopt a firing point that was changed somewhere else.

        Compared against the last *configured* value rather than against what is
        on screen, so changing the lane at the screen itself is not undone again
        by the next redraw.
        """
        if self._lane_source is None:
            return
        try:
            wanted = str(self._lane_source() or "").strip()
        except Exception:  # pragma: no cover - defensive
            return
        if not wanted or wanted == self._configured_lane:
            return
        self._configured_lane = wanted
        if wanted != self._lane:
            self._lane = wanted
            self._series_choice = None

    def refresh(self, poll: bool = True) -> None:
        """Redraw from the shared state, and schedule the next redraw."""
        if self._should_stop is not None and self._should_stop():
            self.close()
            return
        self.follow_config()
        try:
            view = self._state.lane_view(self._lane)
            self._apply(view)
        except Exception as exc:  # pragma: no cover - defensive
            self._status.configure(text=f"error: {exc}", fg=LATEST)
        if poll:
            self.root.after(self._interval_ms, self.refresh)

    def _apply(self, view: LaneView) -> None:
        range_ = view.range
        result = view.result

        # -- header ---------------------------------------------------------
        parts = []
        if range_ is not None:
            if range_.title or range_.name:
                parts.append(range_.title or range_.name)
            if range_.relay:
                parts.append(f"Relay: {range_.relay}")
        self._range_line.configure(text=" · ".join(parts))
        self._host.configure(text=range_.host_name if range_ is not None else "")
        self._lane_badge.configure(text=f" {view.lane} ")

        who = view.shooter_name
        shooter = view.shooter
        if shooter is not None and shooter.club and shooter.club != who:
            who = f"{who} · {shooter.club}"
        if shooter is not None and shooter.class_name:
            who = f"{who} · {shooter.class_name}"
        self._shooter.configure(text=who)

        if range_ is not None:
            self.root.title(f"{view.shooter_name} — lane {view.lane} — {range_.host_name}")

        lanes = self._state.lanes()
        if lanes and self._lane not in lanes:
            self._lane = lanes[0]

        self._fill_clock(result)
        self._fill_standing(view)

        series = self._chosen_series(view)
        self._fill_series_strip(result, series)
        self._canvas.draw(series)
        if series is not None:
            label = series.target_name or series.target_id or ""
            face = series.face
            if face is not None and not label:
                label = face.name
            self._target_label.configure(
                text=f"{series.name or f'Series {series.index}'}"
                + (f" · {label}" if label else "")
                + ("" if face is not None else "  (no face available)")
            )
        else:
            self._target_label.configure(text="")
        self._group_label.configure(text=describe_group(series))

        self._fill_shot_table(series)
        self._fill_totals(result, series)
        # The totals and the shot numbers are sized to their text, so they have
        # to be re-fitted whenever that text changes -- not only on a resize.
        self.root.update_idletasks()
        self._fit_type()
        self._fill_status()
        # Set-up covers everything, idle included: a display with no club or
        # firing point has no position to be idle on.
        if not self._show_setup():
            self._show_idle(view)
        self._show_identify()

    def _fill_clock(self, result: LaneResult | None) -> None:
        """The range clock, or what it is doing instead of running."""
        clock = result.clock if result is not None else None
        remaining = clock.remaining_ms(_now_ms()) if clock is not None else None
        if remaining is not None:
            self._clock.configure(text=format_clock(remaining), fg=CHROME_TEXT)
        elif clock is not None and clock.running:
            self._clock.configure(text="STANDBY", fg=MUTED)
        else:
            self._clock.configure(text="—", fg=MUTED)

    def _fill_standing(self, view: LaneView) -> None:
        """This firing point's place on the relay.

        Recomputed on a timer rather than on every frame: it means reading every
        other firing point on the range, and a placing changing half a second
        late costs nobody anything.
        """
        # Monotonic: an interval, not a time of day, and a wall clock that NTP
        # steps backwards would otherwise freeze the placing until it caught up.
        now = time.monotonic() * 1000.0
        if now - self._standing_at >= STANDING_INTERVAL_MS:
            self._standing_at = now
            self._standing_text = self._relay_standing(view.lane)
        self._standing.configure(text=self._standing_text)

    def _relay_standing(self, lane: str) -> str:
        views = {}
        for other in self._state.lanes():
            try:
                views[other] = self._state.lane_view(other)
            except Exception:  # pragma: no cover - defensive
                continue
        placing = rank_on_relay(lane, views)
        if placing is None:
            return ""
        place, field = placing
        return f"{_ordinal(place)} of {field}"

    def _fill_series_strip(self, result: LaneResult | None, series: Series | None) -> None:
        """The current series' name, then the card's series totals beside it."""
        name = (series.name or f"Series {series.index}") if series is not None else ""
        if series is not None and series.sight:
            # Sighters do not go on the card, and a total in the same type and
            # the same box as a counting one is read as though it did.
            name = f"{name} · SIGHTERS" if name else "SIGHTERS"
        self._strip_name.configure(text=name, fg=STALE if _sighting(series) else CHROME_TEXT)
        totals: list[str] = []
        if series is not None and series.parent is not None:
            # A series the range divides into groups: the strip shows that
            # division, which is what its own display puts there -- six tens of
            # an ISSF card rather than one figure for the whole sixty.
            totals = [value.display() for value in series.parent.splits]
        elif result is not None:
            if result.series_summary:
                # v2 publishes the card's own summary; it is what Megalink shows.
                totals = [value.display() for _name, value in result.series_summary]
            else:
                totals = [entry.total.display() for entry in result.counting_series]
        # Keep the most recent when there are more series than cells.
        if len(totals) > STRIP_CELLS:
            totals = totals[-STRIP_CELLS:]
        for index, cell in enumerate(self._strip_cells):
            cell.configure(text=totals[index] if index < len(totals) else "")

    def _fill_shot_table(self, series: Series | None) -> None:
        """The current group of shots, numbered, down each column in turn.

        Every cell is the same size and every row lines up, which a run of text
        never manages once the values differ in width.
        """
        capacity = SHOT_COLUMNS * SHOT_ROWS
        shots = series.shot_display() if series is not None else []
        first = max(0, len(shots) - capacity)
        visible = shots[first:]

        self._series_total.configure(text="")
        for column in range(SHOT_COLUMNS):
            for row in range(SHOT_ROWS):
                index = column * SHOT_ROWS + row
                cell = self._shot_cells[column][row]
                if index >= len(visible):
                    cell.configure(text="", bg=CHROME, fg=CHROME_TEXT)
                    continue
                # Numbered as the card numbers them: a group of ten that starts
                # at shot 11 runs 11 to 20, not 1 to 10.
                number = series.offset + first + index + 1
                cell.configure(
                    text=f"{number:>3}:  {visible[index]}",
                    bg=CHROME,
                    # The shot just fired, picked out as it is on the target.
                    fg=LATEST if index == len(visible) - 1 else CHROME_TEXT,
                )

    def _fill_totals(self, result: LaneResult | None, series: Series | None) -> None:
        if series is not None:
            # The whole series the range defines, not the group on show: the
            # group totals are in the strip, and this box is the running score
            # for what is being shot -- which is how Megalink reads it.
            whole = series.parent or series
            total = whole.total.display()
            if whole.total.inner is None and whole.inner_count:
                total += f" ({whole.inner_count}x)"
            # Amber, matching the caption, for a total that is not going on the
            # card -- the card total beside it deliberately excludes these.
            self._series_total.configure(text=total, fg=STALE if series.sight else CHROME_TEXT)
        else:
            self._series_total.configure(text="", fg=CHROME_TEXT)
        self._card_total.configure(text=result.total.display() if result is not None else "")

    def _logo_for(self, width: int, height: int) -> Any:
        """The configured badge, scaled to fill its share of the screen.

        Cached against the file's timestamp and the window size, so a redraw
        twice a second does not re-read and re-scale the image.
        """
        finder = getattr(self._state, "logo_path", None)
        path = finder() if callable(finder) else None
        if path is None or not width or not height:
            return None
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return None
        key = (str(path), stamp, width, height)
        if self._logo_source == key:
            return self._logo_image
        self._logo_source = key
        try:
            image = self._tk.PhotoImage(master=self.root, file=str(path))
        except Exception:
            # An image Tk cannot read; the message stands in for it.
            self._logo_image = None
            return None
        across, down = LOGO_BOX
        zoom, shrink = logo_scale((image.width(), image.height()), (width * across, height * down))
        if zoom > 1:
            image = image.zoom(zoom, zoom)
        if shrink > 1:
            image = image.subsample(shrink, shrink)
        self._logo_image = image
        return image

    def _show_idle(self, view: LaneView) -> None:
        """Cover the card when nobody is on this firing point."""
        if not view.idle:
            self._idle.place_forget()
            return

        settings = getattr(getattr(self._state, "config", None), "display", None)
        message = getattr(settings, "idle_text", "") or "POSITION NOT IN USE"
        width, height = self.root.winfo_width(), self.root.winfo_height()

        logo = self._logo_for(width, height)
        if logo is not None:
            self._idle_logo.configure(image=logo)
            self._idle_logo.image = logo
            self._idle_logo.pack(pady=(0, 12))
        else:
            self._idle_logo.pack_forget()

        self._idle_text.configure(text=message)
        self._idle_text.pack()
        range_ = view.range
        where = range_.host_name if range_ is not None else ""
        self._idle_lane.configure(text=f"lane {view.lane}" + (f" · {where}" if where else ""))
        self._idle_lane.pack(pady=(8, 0))
        url = self._current_reach().url() if self._web_port() else None
        self._idle_reach.configure(text=f"Settings: {url}" if url else "")
        self._idle_reach.place(relx=0.5, rely=0.97, anchor="s")
        self._idle.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._idle.lift()

    def _web_port(self) -> int:
        """The configuration page's port, or 0 when it is switched off."""
        web = getattr(getattr(self._state, "config", None), "web", None)
        if web is None or not getattr(web, "enabled", True):
            return 0
        return int(getattr(web, "port", 0) or 0)

    def _current_reach(self) -> Any:
        """Where this display can be reached, looked up at most every few seconds.

        The hotspot fallback's status is read at the same time: both are about
        how to get at the display, and neither changes from frame to frame.
        """
        now = time.monotonic()
        if self._reach is None or now - self._reach_at >= REACH_INTERVAL_S:
            self._reach_at = now
            try:
                self._reach = self._find_reach(self._web_port())
            except Exception:  # pragma: no cover - defensive
                self._reach = reach_module.Reach("", [], self._web_port())
            try:
                self._network = self._read_network()
            except Exception:  # pragma: no cover - defensive
                self._network = None
        return self._reach

    def _hotspot(self) -> dict[str, Any] | None:
        """The display's own Wi-Fi, if it has fallen back to one."""
        self._current_reach()
        status = self._network or {}
        spot = status.get("hotspot") if status.get("mode") == "hotspot" else None
        return spot if isinstance(spot, dict) and spot.get("ssid") else None

    def _show_setup(self) -> bool:
        """Cover the screen with how to set it up, when there is nothing to show.

        Shown when nothing is configured, and also when the display has fallen
        back to its own hotspot -- it cannot show scores while it is off the
        network, and the person in front of it needs to know how to fix that.
        Returns whether it is showing.
        """
        config = getattr(self._state, "config", None)
        if config is None:
            self._setup.place_forget()
            return False
        spot = self._hotspot()
        if getattr(config, "configured", True) and spot is None:
            self._setup.place_forget()
            return False

        port = self._web_port()
        reach = self._current_reach()
        name = getattr(config, "name", "") or reach.hostname

        if spot is not None and port:
            self._show_hotspot(spot, port, configured=getattr(config, "configured", False))
        else:
            self._show_one_code(reach, port, name)

        self._setup.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._setup.lift()
        return True

    def _lay_out_setup(self, two: bool) -> None:
        """One code beside its words, or two steps side by side, each code over
        its words.

        Not four things in a row: code, words, code, words is wider than a
        1280-pixel screen, and the block then hangs off both edges -- which cut
        the Wi-Fi code in half the first time this was tried on a real screen.
        """
        width = self.root.winfo_width() if self.root.winfo_width() > 1 else 1280
        # Wrap the words to their column, so a long network name cannot push
        # the layout off the screen either.
        wrap = int(width * (0.36 if two else 0.46))
        for label in (self._setup_lead, self._setup_url, self._setup_local, self._setup_note):
            label.configure(wraplength=wrap)
        self._setup_step2.configure(wraplength=wrap)
        if two:
            self._setup_code.grid(row=0, column=0, padx=24, pady=(0, 16))
            self._setup_words.grid(row=1, column=0, sticky="n", padx=24)
            self._setup_code2.grid(row=0, column=1, padx=24, pady=(0, 16))
            self._setup_step2.grid(row=1, column=1, sticky="n", padx=24)
        else:
            self._setup_code.grid(row=0, column=0, padx=(0, 28), pady=0)
            self._setup_words.grid(row=0, column=1, sticky="w", padx=0)
            self._setup_code2.grid_remove()
            self._setup_step2.grid_remove()

    def _show_one_code(self, reach: Any, port: int, name: str) -> None:
        """Set-up on a network: the address, and a code of it."""
        self._setup_title.configure(text="Set up this display")
        self._lay_out_setup(two=False)
        self._draw_code("second", self._setup_code2, None, HOTSPOT_CODE_SHARE)
        self._setup_step2.grid_remove()
        url = reach.url() if port else None
        if not port:
            self._setup_lead.configure(text="This display has no\nconfiguration page.")
            self._setup_url.configure(text="")
            self._setup_local.configure(text="")
            self._setup_note.configure(text="Turn it on with web.enabled in its settings file.")
            self._draw_code("first", self._setup_code, None, SETUP_CODE_SHARE)
        elif url is None:
            self._setup_lead.configure(text="Waiting for a network…")
            self._setup_url.configure(text="")
            self._setup_local.configure(text="")
            # Counted down every redraw, from a status read every few seconds:
            # a screen that only ever says "waiting" looks like a dead one.
            self._setup_note.configure(
                text=f"{network.waiting_note(self._network)}\n\n{network.CABLE_NOTE}"
            )
            self._draw_code("first", self._setup_code, None, SETUP_CODE_SHARE)
        else:
            self._setup_lead.configure(text="Scan with your phone,\nor open")
            self._setup_url.configure(text=url)
            local = reach.local_url()
            self._setup_local.configure(text=f"or {local}" if local else "")
            self._setup_note.configure(
                text=f"Your phone must be on the same network.\nThis display is called {name}."
            )
            self._draw_code("first", self._setup_code, url, SETUP_CODE_SHARE)

    def _show_hotspot(self, spot: dict[str, Any], port: int, configured: bool) -> None:
        """Set-up on the display's own Wi-Fi: join it, then open the settings.

        Two codes, because that is two steps -- and a phone's camera will join a
        network from a code, which saves typing a password off a screen.
        """
        ssid, password = str(spot.get("ssid", "")), str(spot.get("password", ""))
        address = str(spot.get("address") or network.HOTSPOT_ADDRESS)
        url = f"http://{address}{'' if port == 80 else f':{port}'}/"
        self._setup_title.configure(
            text="Connect to this display" if configured else "Set up this display"
        )
        self._lay_out_setup(two=True)
        self._setup_lead.configure(text="1. Join its Wi-Fi")
        self._setup_url.configure(text=ssid)
        self._setup_local.configure(text=f"password  {password}" if password else "no password")
        self._setup_note.configure(
            text=("It can't find the network it knows.\n" if configured else "")
            + "Your phone may say there is no internet;\nstay connected anyway."
        )
        self._draw_code("first", self._setup_code, qr.wifi(ssid, password), HOTSPOT_CODE_SHARE)
        self._setup_step2.configure(text=f"2. Then open\n{url}")
        self._draw_code("second", self._setup_code2, url, HOTSPOT_CODE_SHARE)

    def _draw_code(self, key: str, canvas: Any, text: str | None, share: float) -> None:
        """Draw ``text`` as a QR code on ``canvas``, or take the code away.

        Whole pixels to a module, so every module comes out the same size -- a
        scanner copes with a code that is slightly too small much better than
        with one whose modules are uneven. Redrawn only when it changes.
        """
        if not text:
            canvas.grid_remove()
            self._codes_drawn.pop(key, None)
            return
        width, height = self.root.winfo_width(), self.root.winfo_height()
        if width > 1 and height > 1:
            # Two codes share the width, so a wide-but-short screen and a
            # narrow one both leave room for the words.
            side = int(min(min(width, height) * share, width * share * 0.9))
        else:
            side = 300
        if self._codes_drawn.get(key) != (text, side):
            grid = qr.modules(text)
            cell = max(2, side // len(grid))
            span = cell * len(grid)
            canvas.delete("all")
            canvas.configure(width=span, height=span)
            for y, row in enumerate(grid):
                for x, dark in enumerate(row):
                    if dark:
                        canvas.create_rectangle(
                            x * cell,
                            y * cell,
                            (x + 1) * cell,
                            (y + 1) * cell,
                            fill="#000000",
                            outline="",
                        )
            self._codes_drawn[key] = (text, side)

    def _show_identify(self) -> None:
        """Flash the display's name when it has been asked to identify itself."""
        asking = getattr(self._state, "identifying", None)
        wanted = bool(asking()) if callable(asking) else False
        if not wanted:
            self._identify.place_forget()
            return
        name = getattr(getattr(self._state, "config", None), "name", "") or "this display"
        # Which screen, on a Pi with two: that is the question being asked.
        label = f"{name} · {self.screen_label}" if self.screen_label else name
        # Blink, so it catches the eye from down the line.
        on = int(time.time() * 2) % 2 == 0
        self._identify.configure(
            text=f"▶ {label}", bg=ACCENT if on else "#0d1b24", fg="#0d1b24" if on else ACCENT
        )
        self._identify.place(relx=0.5, rely=0.5, anchor="center")
        self._identify.lift()

    def _fill_status(self) -> None:
        age = self._state.age()
        if age is None:
            self._status.configure(text="waiting for the feed…", fg=STALE)
            return
        source = getattr(self._state, "source", None)
        protocol = f"protocol v{source.protocol}" if source is not None else ""
        view = self._state.lane_view(self._lane)
        result = view.result
        counted = []
        if result is not None:
            counted.append(f"{result.shot_count} shots")
            if result.total.inner:
                counted.append(f"{result.total.inner} inner")
        detail = " · ".join([*counted, protocol]) if protocol else " · ".join(counted)
        if age < 5:
            self._status.configure(text=f"live · {detail}", fg=MUTED)
        else:
            self._status.configure(text=f"last update {int(age)}s ago · {detail}", fg=STALE)

    def run(self) -> None:
        self.refresh()
        self.root.mainloop()


def _now_ms() -> float:
    return time.time() * 1000.0
