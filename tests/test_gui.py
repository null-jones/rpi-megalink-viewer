"""The windowed display.

These drive a real Tk instance and then read back what was drawn, which checks
the geometry actually reaches the screen rather than just the model. The whole
module skips where Tk cannot open a display, so a headless CI still passes.
"""

from __future__ import annotations

import gc
import math
import subprocess
import sys
import time
from typing import Any, ClassVar

import pytest

from megalink_viewer import v1, v2
from megalink_viewer.client import ARENA_DB, Source
from megalink_viewer.models import LaneView, rank_on_relay

tk = pytest.importorskip("tkinter", reason="Tkinter is not installed")


def _tk_usable() -> bool:
    """Whether a Tk window can actually be created, checked out-of-process.

    Some builds do not raise when Tk is unusable -- they abort the interpreter,
    which no ``try`` here could catch.
    Probing in a subprocess keeps that crash out of this test run.
    """
    probe = "import tkinter; tkinter.Tk().destroy()"
    try:
        return (
            subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                timeout=60,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return False


if not _tk_usable():  # pragma: no cover - depends on the platform's Tk
    pytest.skip("Tk cannot open a display here", allow_module_level=True)


@pytest.fixture(scope="module")
def root():
    widget = tk.Tk()
    widget.withdraw()
    yield widget
    widget.destroy()
    del widget
    gc.collect()


@pytest.fixture(autouse=True)
def collect_tk_on_the_main_thread():
    """Free each test's Tk interpreters here, before anything else can.

    A closed window's interpreter is left in a reference cycle, so it is freed
    by the garbage collector -- which runs on whichever thread happens to be
    allocating at the time. Later tests start background threads of their own
    (every configuration-page test runs a controller), and when one of those
    triggered the collection, Tcl found its interpreter being deleted from a
    thread that did not create it and aborted the whole process:

        Tcl_AsyncDelete: async handler deleted by the wrong thread

    Collecting straight after each test, on the main thread, leaves nothing Tk
    lying around for another thread to find.
    """
    yield
    gc.collect()


def fonts_scale(widget) -> bool:
    """Whether this Tk can draw type at the size it is asked for.

    Not every Tk can. One built without Xft uses only the X server's core
    bitmap fonts, and a bare X server has one: ``fixed``, at nine points,
    whatever size is requested. uv's own Python builds are like that on Linux,
    with or without fonts installed. Debian's ``python3-tk`` is not -- it brings
    DejaVu with it and scales exactly -- and that is what a Pi runs.
    """
    import tkinter.font as tkfont

    family = tkfont.nametofont("TkDefaultFont", root=widget).actual("family")
    text = "594.8 (23x)"
    small = tkfont.Font(root=widget, family=family, size=10).measure(text)
    large = tkfont.Font(root=widget, family=family, size=40).measure(text)
    return large > small * 2


@pytest.fixture
def scalable_fonts(root):
    """Skip a test that is about fitting type, on a Tk that cannot resize type.

    A total cannot be set smaller to fit its box if every size comes out the
    same. The tests still run wherever it matters -- CI runs them in Debian
    containers with the Tk a Pi actually has.
    """
    if not fonts_scale(root):
        pytest.skip("this Tk cannot scale fonts (no Xft), so type fitting cannot be tested")


@pytest.fixture
def gui():
    from megalink_viewer import gui as module

    return module


class FakeState:
    """Stands in for a RangeState without touching the network."""

    def __init__(self, tree, source, lanes, age=0.0):
        from megalink_viewer.config import Config

        self._tree = tree
        self.source = source
        self._lanes = lanes
        self._age = age
        self.config = Config(host="stord-pk", range="1-10", lane="9")

    def logo_path(self):
        return None

    def lane_view(self, lane):
        return self.source.lane_view(self._tree, lane)

    def lanes(self):
        return self._lanes

    def age(self):
        return self._age


@pytest.fixture
def v2_state(v2_pistol):
    source = Source(2, ARENA_DB, "data/stord-pk/1-10", "stord-pk", "1-10")
    return FakeState(v2_pistol, source, v2.lanes(v2_pistol))


def a_series(view):
    return next(s for s in view.result.series if s.shots)


class TestTargetCanvas:
    def _canvas(self, gui, root, width=400, height=400):
        canvas = gui.TargetCanvas(tk, root)
        canvas.widget.configure(width=width, height=height)
        canvas.widget.update_idletasks()
        return canvas

    def _items(self, canvas, kind):
        return [i for i in canvas.widget.find_all() if canvas.widget.type(i) == kind]

    def test_a_face_draws_its_rings(self, gui, root, v2_pistol):
        series = a_series(v2.lane_view(v2_pistol, "1-10", "9"))
        canvas = self._canvas(gui, root)
        canvas.draw(series)
        # One oval per drawn ring, plus the fills and the shots.
        assert len(self._items(canvas, "oval")) >= len([r for r in series.face.rings() if r.drawn])

    def test_rings_are_concentric_and_correctly_scaled(self, gui, root, v1_dfs):
        series = a_series(v1.lane_view(v1_dfs, "15m", "2"))
        face = series.face
        canvas = self._canvas(gui, root, 400, 400)
        canvas.draw(series)

        widget = canvas.widget
        # Recover each oval's centre and radius in pixels.
        found = []
        for item in self._items(canvas, "oval"):
            x0, y0, x1, y1 = widget.coords(item)
            found.append((((x0 + x1) / 2, (y0 + y1) / 2), (x1 - x0) / 2))
        centres = {(round(cx, 1), round(cy, 1)) for (cx, cy), _ in found}
        assert len(centres) == 1 or len(centres) <= 1 + len(series.shots), (
            "rings must share one centre"
        )

        # The largest oval is the outermost ring, and the ratio between ring
        # radii on screen must match the ratio in millimetres.
        radii_px = sorted((r for _, r in found), reverse=True)
        scale = radii_px[0] / face.extent
        for ring in face.rings():
            if not ring.drawn or ring.radius * scale < 1.5:
                continue
            expected = ring.radius * scale
            assert any(abs(r - expected) < 1.5 for r in radii_px), (
                f"ring {ring.number} at {expected:.1f}px missing"
            )

    def test_shots_are_plotted_where_the_feed_puts_them(self, gui, root, v1_dfs):
        series = a_series(v1.lane_view(v1_dfs, "15m", "2"))
        canvas = self._canvas(gui, root, 400, 400)
        canvas.draw(series)
        widget = canvas.widget

        centres = []
        radii = []
        for item in self._items(canvas, "oval"):
            x0, y0, x1, y1 = widget.coords(item)
            centres.append(((x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2))
            radii.append((x1 - x0) / 2)
        scale = max(radii) / series.face.extent
        width, height = canvas.size()
        cx, cy = width / 2.0, height / 2.0

        for _shot, x_mm, y_mm in series.positions_mm():
            # Canvas y grows downwards; a target's does not.
            want = (cx + x_mm * scale, cy - y_mm * scale)
            assert any(math.hypot(x - want[0], y - want[1]) < 1.0 for x, y, _r in centres), (
                f"no marker near {want}"
            )

    def test_one_marker_per_plotted_shot(self, gui, root, v1_dfs):
        series = a_series(v1.lane_view(v1_dfs, "15m", "2"))
        canvas = self._canvas(gui, root)
        canvas.draw(series)
        widget = canvas.widget
        markers = [
            item
            for item in self._items(canvas, "oval")
            if widget.itemcget(item, "fill") in (gui.SHOT, gui.LATEST)
        ]
        assert len(markers) == len(series.positions_mm())

    def test_ring_numbers_follow_the_faces_axes(self, gui, root):
        from megalink_viewer.models import Series
        from megalink_viewer.parse import Total

        # The rapid-fire face numbers only the vertical axis.
        series = Series(
            index=1,
            name="s",
            kind="MATCH",
            decimal=False,
            complete=False,
            shots=[],
            total=Total("", 0),
            target_id="ISSF25P_SMALL_RF",
            protocol=2,
        )
        canvas = self._canvas(gui, root, 520, 520)
        canvas.draw(series)
        widget = canvas.widget
        centre_x = canvas.size()[0] / 2.0
        texts = [i for i in widget.find_all() if widget.type(i) == "text"]
        assert texts, "expected ring numbers"
        for item in texts:
            x, _y = widget.coords(item)
            assert abs(x - centre_x) < 1.0, "numbers should sit on the vertical axis"

    def test_crowded_numbers_are_dropped_rather_than_overlapped(self, gui, root):
        """A wide view on a small pane leaves no room for ring numbers."""
        from megalink_viewer.models import Series, Shot
        from megalink_viewer.parse import Total, parse_shot_value

        # Shots spread to the edge force the view all the way out, which is when
        # the rings crowd together.
        spread = [
            Shot(parse_shot_value("2.0"), x=20.0, y=0.0),
            Shot(parse_shot_value("2.0"), x=-20.0, y=0.0),
        ]
        series = Series(
            index=1,
            name="s",
            kind="MATCH",
            decimal=False,
            complete=False,
            shots=spread,
            total=Total("", 0),
            target_id="ISSF10R",
            protocol=2,
        )
        big = self._canvas(gui, root, 600, 600)
        big.draw(series)
        small = self._canvas(gui, root, 90, 90)
        small.draw(series)

        def count_text(canvas):
            widget = canvas.widget
            return len([i for i in widget.find_all() if widget.type(i) == "text"])

        assert count_text(small) < count_text(big)

    def test_an_empty_series_shows_the_whole_face(self, gui, root):
        """Zooming into an empty middle would show a blank circle."""
        from megalink_viewer.models import Series
        from megalink_viewer.parse import Total

        series = Series(
            index=1,
            name="s",
            kind="MATCH",
            decimal=False,
            complete=False,
            shots=[],
            total=Total("", 0),
            target_id="ISSF10R",
            protocol=2,
        )
        canvas = self._canvas(gui, root, 400, 400)
        assert canvas._plot_extent(series.face, []) == series.face.extent

    def test_the_view_tightens_around_a_close_group(self, gui, root):
        """Megalink's auto-zoom: a tight group is shown close up."""
        from megalink_viewer.models import Series, Shot
        from megalink_viewer.parse import Total, parse_shot_value

        face_id = "ISSF10R"
        canvas = self._canvas(gui, root, 400, 400)
        tight = Series(
            index=1,
            name="s",
            kind="MATCH",
            decimal=False,
            complete=False,
            shots=[Shot(parse_shot_value("10.4"), x=1.0, y=1.0)],
            total=Total("", 0),
            target_id=face_id,
            protocol=2,
        )
        wide = Series(
            index=1,
            name="s",
            kind="MATCH",
            decimal=False,
            complete=False,
            shots=[Shot(parse_shot_value("3.0"), x=18.0, y=0.0)],
            total=Total("", 0),
            target_id=face_id,
            protocol=2,
        )
        close_view = canvas._plot_extent(tight.face, tight.positions_mm())
        wide_view = canvas._plot_extent(wide.face, wide.positions_mm())
        assert close_view < wide_view <= tight.face.extent * 1.3

    def test_a_face_this_package_lacks_still_plots(self, gui, root):
        from megalink_viewer.models import Series, Shot
        from megalink_viewer.parse import Total, parse_shot_value

        series = Series(
            index=1,
            name="Elk",
            kind="MATCH",
            decimal=False,
            complete=False,
            shots=[Shot(parse_shot_value("9.5"), x=10.0, y=-4.0)],
            total=Total("", 9),
            target_id="NO_NJFF_MOOSE_RUNNING",
            protocol=2,
        )
        canvas = self._canvas(gui, root)
        canvas.draw(series)
        assert series.face is None
        # A marker and a note, rather than an empty pane.
        assert self._items(canvas, "oval")
        notes = [
            canvas.widget.itemcget(i, "text")
            for i in canvas.widget.find_all()
            if canvas.widget.type(i) == "text"
        ]
        assert any("no face" in n for n in notes)

    def test_no_series_yet(self, gui, root):
        canvas = self._canvas(gui, root)
        canvas.draw(None)
        texts = [
            canvas.widget.itemcget(i, "text")
            for i in canvas.widget.find_all()
            if canvas.widget.type(i) == "text"
        ]
        assert any("waiting" in t for t in texts)


class TestDrawOrder:
    """Ring lines are drawn over the shots, so a group never hides a ring.

    Tk's canvas is a display list: items are painted in creation order, and
    ``find_all`` returns them in that order. So the ordering can be asserted
    directly rather than inferred from a screenshot.
    """

    def _canvas(self, gui, root, width=420, height=420):
        canvas = gui.TargetCanvas(tk, root)
        canvas.widget.configure(width=width, height=height)
        canvas.widget.update_idletasks()
        return canvas

    def _indexed(self, canvas):
        """Every canvas item with its position in the display list."""
        widget = canvas.widget
        return [(order, item, widget.type(item)) for order, item in enumerate(widget.find_all())]

    def _split(self, canvas):
        """Display-list positions of the face discs, the markers and the rings.

        Told apart by colour, which is exact: a ring line has no fill, a marker
        is drawn in one of the two shot colours, and everything else filled is
        part of the face.
        """
        widget = canvas.widget
        from megalink_viewer import gui as module

        shot_colours = {module.SHOT, module.LATEST}
        fills, markers, lines = [], [], []
        for order, item, kind in self._indexed(canvas):
            if kind != "oval":
                continue
            colour = widget.itemcget(item, "fill")
            if not colour:
                lines.append(order)
            elif colour in shot_colours:
                markers.append(order)
            else:
                fills.append(order)
        return fills, markers, lines

    def test_every_ring_line_is_above_every_marker(self, gui, root, v1_dfs):
        series = a_series(v1.lane_view(v1_dfs, "15m", "2"))
        canvas = self._canvas(gui, root)
        canvas.draw(series)
        _fills, markers, lines = self._split(canvas)
        assert markers, "expected shot markers"
        assert lines, "expected ring lines"
        assert min(lines) > max(markers), (
            "ring lines must be drawn after the shots so they stay unbroken"
        )

    def test_markers_are_above_the_face_fills(self, gui, root, v1_dfs):
        series = a_series(v1.lane_view(v1_dfs, "15m", "2"))
        canvas = self._canvas(gui, root)
        canvas.draw(series)
        fills, markers, _lines = self._split(canvas)
        assert fills and markers
        assert min(markers) > max(fills), "shots must sit on top of the paper"

    def test_the_shots_are_not_numbered_on_the_plot(self, gui, root, v1_dfs):
        """The firing order is in the grid; over the plot it buries the group."""
        series = a_series(v1.lane_view(v1_dfs, "15m", "2"))
        # Stacked on one spot, so the zoom -- and with it the ring numbering --
        # is identical however many shots there are. Any difference in the text
        # drawn is then the shots labelling themselves.
        for shot in series.shots:
            shot.x, shot.y = 0.05, 0.05

        def labels(count):
            series.shots = series.shots[:count]
            canvas = self._canvas(gui, root)
            canvas.draw(series)
            return [
                canvas.widget.itemcget(item, "text")
                for item in canvas.widget.find_all()
                if canvas.widget.type(item) == "text"
            ]

        many = labels(5)
        assert labels(1) == many
        assert many, "the ring numbers are still drawn"

    def test_markers_are_fully_opaque(self, gui, root, v1_dfs):
        """No stipple and no transparency: a marker is a solid colour."""
        series = a_series(v1.lane_view(v1_dfs, "15m", "2"))
        canvas = self._canvas(gui, root)
        canvas.draw(series)
        widget = canvas.widget
        _fills, markers, _lines = self._split(canvas)
        items = widget.find_all()
        for order in markers:
            item = items[order]
            assert widget.itemcget(item, "fill") in (gui.SHOT, gui.LATEST)
            assert not widget.itemcget(item, "stipple")

    def test_the_newest_shot_has_its_own_colour(self, gui, root, v2_pistol):
        series = a_series(v2.lane_view(v2_pistol, "1-10", "9"))
        canvas = self._canvas(gui, root)
        canvas.draw(series)
        widget = canvas.widget
        items = widget.find_all()
        _fills, markers, _lines = self._split(canvas)
        colours = [widget.itemcget(items[order], "fill") for order in markers]
        assert colours[-1] == gui.LATEST
        if len(colours) > 1:
            assert set(colours[:-1]) == {gui.SHOT}

    def test_ring_line_width_follows_the_face(self, gui, root, v1_dfs):
        series = a_series(v1.lane_view(v1_dfs, "15m", "2"))
        canvas = self._canvas(gui, root, 700, 700)
        canvas.draw(series)
        widget = canvas.widget
        items = widget.find_all()
        _fills, _markers, lines = self._split(canvas)
        for order in lines:
            assert int(float(widget.itemcget(items[order], "width"))) >= 1


class TestIdleOverlay:
    """A firing point with nobody on it should say so."""

    def _window(self, gui, state, lane):
        win = gui.LaneWindow(state, lane, interval_ms=10_000)
        win.root.withdraw()
        win.refresh(poll=False)
        return win

    def test_an_empty_point_is_covered(self, gui, root, v2_state):
        # Lane 3 has nobody on it and nothing shot.
        assert v2_state.lane_view("3").idle
        win = self._window(gui, v2_state, "3")
        try:
            assert win._idle.winfo_manager() == "place"
            assert "NOT IN USE" in win._idle_text.cget("text")
        finally:
            win.close()

    def test_a_point_being_shot_is_not_covered(self, gui, root, v2_state):
        assert not v2_state.lane_view("9").idle
        win = self._window(gui, v2_state, "9")
        try:
            assert win._idle.winfo_manager() == ""
        finally:
            win.close()

    def test_the_cover_names_the_lane(self, gui, root, v2_state):
        win = self._window(gui, v2_state, "3")
        try:
            assert "lane 3" in win._idle_lane.cget("text")
        finally:
            win.close()

    def test_it_uncovers_when_somebody_starts(self, gui, root, v2_state):
        win = self._window(gui, v2_state, "3")
        try:
            assert win._idle.winfo_manager() == "place"
            win.set_lane("9")
            assert win._idle.winfo_manager() == ""
        finally:
            win.close()

    def test_the_message_can_be_changed(self, gui, root, v2_state, tmp_path):
        from megalink_viewer.config import Config

        config = Config(host="stord-pk", range="1-10", lane="3")
        config.display.idle_text = "Ganløse Skytteforening"
        v2_state.config = config
        win = self._window(gui, v2_state, "3")
        try:
            assert win._idle_text.cget("text") == "Ganløse Skytteforening"
        finally:
            win.close()

    def test_a_logo_is_shown_instead_of_the_message(self, gui, root, v2_state, tmp_path):
        from conftest import make_png

        logo = tmp_path / "logo.png"
        logo.write_bytes(make_png(60, 30))
        v2_state.logo_path = lambda: logo
        win = self._window(gui, v2_state, "3")
        try:
            assert win._idle_logo.winfo_manager() == "pack"
            assert win._logo_image is not None
        finally:
            win.close()

    def test_a_logo_too_big_for_the_screen_is_shrunk(self, gui, root, v2_state, tmp_path):
        """Tk only scales by whole numbers, so a badge is subsampled to fit."""
        from conftest import make_png

        logo = tmp_path / "logo.png"
        logo.write_bytes(make_png(1200, 900))
        v2_state.logo_path = lambda: logo
        win = self._window(gui, v2_state, "3")
        win.root.geometry("640x480")
        win.root.update_idletasks()
        win.refresh(poll=False)
        try:
            image = win._logo_image
            assert image is not None
            assert image.width() < 1200
        finally:
            win.close()

    def test_a_logo_smaller_than_the_screen_is_enlarged(self, gui, root, v2_state, tmp_path):
        """Only ever shrinking left a small badge stranded on a big screen."""
        from conftest import make_png

        logo = tmp_path / "logo.png"
        logo.write_bytes(make_png(80, 60))
        v2_state.logo_path = lambda: logo
        win = self._window(gui, v2_state, "3")
        try:
            win.root.geometry("1280x720")
            win.root.update_idletasks()
            win.refresh(poll=False)
            image = win._logo_image
            assert image is not None
            assert image.width() > 80
        finally:
            win.close()

    def test_the_badge_and_message_sit_at_the_middle(self, gui, root, v2_state):
        win = self._window(gui, v2_state, "3")
        try:
            info = win._idle_stack.place_info()
            assert float(info["relx"]) == 0.5
            assert float(info["rely"]) == 0.5
            assert info["anchor"] == "center"
            # Everything on the cover has to be in the centred block, or it
            # sits against the top edge on its own.
            for widget in (win._idle_logo, win._idle_text, win._idle_lane):
                assert widget.winfo_parent() == str(win._idle_stack)
        finally:
            win.close()

    def test_an_unreadable_logo_falls_back_to_the_message(self, gui, root, v2_state, tmp_path):
        bad = tmp_path / "logo.png"
        bad.write_bytes(b"not really a png")
        v2_state.logo_path = lambda: bad
        win = self._window(gui, v2_state, "3")
        try:
            assert win._idle_logo.winfo_manager() == ""
            assert win._idle_text.cget("text")
        finally:
            win.close()


class TestGroupAndImpact:
    """What the card cannot say: how tight the group is and where it sits."""

    def series(self, v2_pistol, lane="9"):
        return a_series(v2.lane_view(v2_pistol, "1-10", lane))

    def test_the_spread_is_measured_from_the_groups_own_centre(self, gui, v2_pistol):
        series = self.series(v2_pistol)
        placed = series.positions_mm()
        centre_x, centre_y = series.mpi_mm()
        radii = [math.hypot(x - centre_x, y - centre_y) for _s, x, y in placed]
        mean, deviation = series.radial_stats()
        assert mean == pytest.approx(sum(radii) / len(radii))
        expected = math.sqrt(sum((r - mean) ** 2 for r in radii) / len(radii))
        # Divided by n, not n-1: these shots are the group, not a sample of it.
        assert deviation == pytest.approx(expected)

    def test_a_perfect_group_has_no_spread(self, gui, v2_pistol):
        series = self.series(v2_pistol)
        for shot in series.shots:
            shot.x, shot.y = 4.0, 7.0
        mean, deviation = series.radial_stats()
        assert mean == pytest.approx(0.0)
        assert deviation == pytest.approx(0.0)

    def test_the_spread_ignores_where_the_group_sits(self, gui, v2_pistol):
        # A tight group off to one side is still a tight group.
        series = self.series(v2_pistol)
        before = series.radial_stats()
        for shot in series.shots:
            shot.x += 25.0
            shot.y -= 40.0
        assert series.radial_stats() == pytest.approx(before)

    def test_a_single_shot_has_no_spread(self, gui, v2_pistol):
        series = self.series(v2_pistol)
        series.shots = series.shots[:1]
        assert series.radial_stats() is None

    def test_the_impact_point_is_the_mean_of_the_shots(self, gui, v2_pistol):
        series = self.series(v2_pistol)
        placed = series.positions_mm()
        x, y = series.mpi_mm()
        assert x == pytest.approx(sum(p[1] for p in placed) / len(placed))
        assert y == pytest.approx(sum(p[2] for p in placed) / len(placed))

    def test_an_unplotted_series_has_no_impact_point(self, gui, v2_pistol):
        series = self.series(v2_pistol)
        series.shots = []
        assert series.mpi_mm() is None

    def test_the_readout_names_the_spread_with_its_symbols(self, gui, v2_pistol):
        text = gui.describe_group(self.series(v2_pistol))
        assert "\u03bc" in text and "\u03c3" in text
        assert "mm" in text

    def test_the_readout_gives_the_centre_as_signed_coordinates(self, gui, v2_pistol):
        series = self.series(v2_pistol)
        for shot in series.shots:
            shot.x, shot.y = 12.0, -5.0
        text = gui.describe_group(series)
        # Signed, and in the sense the plot is drawn: x right, y up.
        assert "(+12.0, -5.0) mm" in text

    def test_a_group_on_the_middle_is_called_centred(self, gui, v2_pistol):
        series = self.series(v2_pistol)
        for shot in series.shots:
            shot.x = shot.y = 0.0
        assert "centred" in gui.describe_group(series)
        assert "(" not in gui.describe_group(series)

    def test_nothing_is_said_about_nothing(self, gui):
        assert gui.describe_group(None) == ""

    def test_the_impact_point_is_marked_on_the_target(self, gui, root, v2_pistol):
        canvas = gui.TargetCanvas(tk, root)
        canvas.widget.configure(width=400, height=400)
        canvas.widget.update_idletasks()
        canvas.draw(self.series(v2_pistol))
        crosses = [
            item
            for item in canvas.widget.find_all()
            if canvas.widget.type(item) == "line"
            and canvas.widget.itemcget(item, "fill") == gui.MPI
        ]
        # Two arms, and in a colour no shot uses, so it cannot be counted as one.
        assert len(crosses) == 2
        assert gui.MPI not in (gui.SHOT, gui.LATEST)

    def test_one_shot_is_its_own_impact_point_so_is_not_marked(self, gui, root, v2_pistol):
        series = self.series(v2_pistol)
        series.shots = series.shots[:1]
        canvas = gui.TargetCanvas(tk, root)
        canvas.widget.configure(width=400, height=400)
        canvas.widget.update_idletasks()
        canvas.draw(series)
        assert not [
            item
            for item in canvas.widget.find_all()
            if canvas.widget.type(item) == "line"
            and canvas.widget.itemcget(item, "fill") == gui.MPI
        ]


class TestRelayStanding:
    """Where this firing point stands, which the state already knows."""

    def views(self, totals):
        from megalink_viewer.models import LaneResult, LaneView, Shooter
        from megalink_viewer.parse import Total

        out = {}
        for lane, (value, inner) in totals.items():
            result = LaneResult(Total(str(value), value, inner), [])
            out[lane] = LaneView(lane, shooter=Shooter(name=f"shooter {lane}"), result=result)
        return out

    def test_the_best_card_leads(self):
        views = self.views({"1": (95, 2), "2": (99, 3), "3": (90, 0)})
        assert rank_on_relay("2", views) == (1, 3)
        assert rank_on_relay("1", views) == (2, 3)
        assert rank_on_relay("3", views) == (3, 3)

    def test_inner_tens_break_a_tie(self):
        views = self.views({"1": (95, 4), "2": (95, 1)})
        assert rank_on_relay("1", views) == (1, 2)
        assert rank_on_relay("2", views) == (2, 2)

    def test_a_true_tie_shares_a_place_and_consumes_the_next(self):
        views = self.views({"1": (95, 2), "2": (95, 2), "3": (90, 0)})
        assert rank_on_relay("1", views) == (1, 3)
        assert rank_on_relay("2", views) == (1, 3)
        # Two firsts are followed by a third, as a results list has it.
        assert rank_on_relay("3", views) == (3, 3)

    def test_an_empty_lane_is_not_somebody_in_last_place(self):
        from megalink_viewer.models import LaneView

        views = self.views({"1": (95, 0), "2": (90, 0)})
        views["3"] = LaneView("3")
        assert rank_on_relay("1", views) == (1, 2)
        assert rank_on_relay("3", views) is None

    def test_a_lone_shooter_gets_no_placing(self):
        # "1st of 1" says nothing anybody needs to read.
        assert rank_on_relay("1", self.views({"1": (95, 0)})) is None

    def test_a_card_without_a_total_is_not_ranked(self):
        from megalink_viewer.models import LaneResult, LaneView, Shooter
        from megalink_viewer.parse import Total

        views = self.views({"1": (95, 0), "2": (90, 0)})
        views["3"] = LaneView(
            "3", shooter=Shooter(name="new"), result=LaneResult(Total("", None), [])
        )
        assert rank_on_relay("3", views) is None
        assert rank_on_relay("1", views) == (1, 2)

    def test_the_ordinal_handles_the_teens(self, gui):
        assert [gui._ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21)] == [
            "1st",
            "2nd",
            "3rd",
            "4th",
            "11th",
            "12th",
            "13th",
            "21st",
        ]

    def test_the_screen_shows_the_standing(self, gui, root, v2_state):
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        try:
            win.refresh(poll=False)
            assert "of" in win._standing.cget("text")
        finally:
            win.close()

    def test_the_standing_is_not_recomputed_every_frame(self, gui, root, v2_state):
        # Working it out means reading every other firing point on the range.
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        try:
            win.refresh(poll=False)
            calls = []
            original = win._relay_standing
            win._relay_standing = lambda lane: calls.append(lane) or original(lane)
            for _ in range(5):
                win.refresh(poll=False)
            assert not calls
        finally:
            win.close()


class TestSplitCardOnScreen:
    """A 60-shot card published as one series, shown as the range shows it.

    Checked against Megalink Live's own display of this range: the strip reads
    ``60 Shots | 95 95 7 0 0 0``, the grid holds the current ten numbered from
    21, and both totals read 197.
    """

    @pytest.fixture
    def state(self, v2_split_card):
        source = Source(2, ARENA_DB, "data/us-naval-academy/airgun", "us-naval-academy", "airgun")
        return FakeState(v2_split_card, source, v2.lanes(v2_split_card))

    @pytest.fixture
    def window(self, gui, root, state):
        win = gui.LaneWindow(state, "28", interval_ms=10_000)
        win.root.withdraw()
        win.refresh(poll=False)
        yield win
        win.close()

    def cells(self, window):
        return [
            cell.cget("text")
            for column in window._shot_cells
            for cell in column
            if cell.cget("text")
        ]

    def test_the_strip_shows_the_groups_not_one_figure_for_the_card(self, window):
        assert [c.cget("text") for c in window._strip_cells] == ["95", "95", "7", "0", "0", "0"]

    def test_the_strip_names_what_is_being_shot(self, window):
        assert window._strip_name.cget("text") == "60 Shots"

    def test_only_the_current_group_is_on_the_target(self, window, state):
        series = window._chosen_series(state.lane_view("28"))
        assert len(series.shots) == 1
        assert len(series.parent.shots) == 21

    def test_the_grid_numbers_shots_as_the_card_does(self, window):
        # Shot 21, not shot 1 of the third group.
        assert self.cells(window) == [" 21:  7.6"]

    def test_the_series_total_is_the_whole_series_not_the_group(self, window):
        # Megalink shows 197 here -- the running score for the sixty -- with the
        # group totals in the strip. The group on its own is 7.
        assert window._series_total.cget("text") == "197 (9x)"
        assert window._card_total.cget("text") == "197 (9x)"

    def test_a_finished_group_fills_the_grid(self, gui, root, state):
        win = gui.LaneWindow(state, "27", interval_ms=10_000)
        win.root.withdraw()
        try:
            win.refresh(poll=False)
            numbers = [int(t.split(":")[0]) for t in self.cells(win)]
            assert numbers[0] == 11
            assert len(numbers) <= gui.SHOT_COLUMNS * gui.SHOT_ROWS
        finally:
            win.close()

    def test_stepping_back_walks_the_groups(self, window):
        first = window._chosen_series(window._state.lane_view("28"))
        window.step_series(-1)
        back = window._chosen_series(window._state.lane_view("28"))
        assert back.offset < first.offset
        assert len(back.shots) == 10

    def test_stepping_forward_again_follows_the_live_group(self, window):
        window.step_series(-1)
        assert window._series_choice is not None
        window.step_series(1)
        assert window._series_choice is None

    def test_stepping_stops_at_the_ends(self, window):
        for _ in range(40):
            window.step_series(-1)
        assert window._chosen_series(window._state.lane_view("28")).offset == 0
        for _ in range(80):
            window.step_series(1)
        # The last group of the card, and following the live one again.
        assert window._series_choice is None


class TestSighters:
    """A sighting series does not go on the card, and must not look as if it does."""

    def window(self, gui, state, lane="9"):
        win = gui.LaneWindow(state, lane, interval_ms=10_000)
        win.root.withdraw()
        win.refresh(poll=False)
        return win

    def sighting(self, window, v2_state, lane="9"):
        """Redraw the card with its current series marked as sighters."""
        from megalink_viewer.models import SIGHT

        result = v2_state.lane_view(lane).result
        series = result.active_series
        series.kind = SIGHT
        assert series.sight
        window._fill_series_strip(result, series)
        window._fill_totals(result, series)
        return series

    def test_a_counting_series_is_not_flagged(self, gui, root, v2_state):
        win = self.window(gui, v2_state)
        try:
            assert "SIGHTERS" not in win._strip_name.cget("text")
            assert win._series_total.cget("fg") == gui.CHROME_TEXT
            assert win._strip_name.cget("fg") == gui.CHROME_TEXT
        finally:
            win.close()

    def test_a_sighting_series_is_named_as_one(self, gui, root, v2_state):
        win = self.window(gui, v2_state)
        try:
            self.sighting(win, v2_state)
            assert "SIGHTERS" in win._strip_name.cget("text")
        finally:
            win.close()

    def test_a_sighting_total_is_set_apart_from_a_counting_one(self, gui, root, v2_state):
        win = self.window(gui, v2_state)
        try:
            self.sighting(win, v2_state)
            # The same box at the same size as a counting total reads as one.
            assert win._series_total.cget("fg") == gui.STALE
            assert win._strip_name.cget("fg") == gui.STALE
        finally:
            win.close()

    def test_the_sighting_total_is_still_shown(self, gui, root, v2_state):
        # Set apart, not hidden -- a shooter still wants to see their sighters.
        win = self.window(gui, v2_state)
        try:
            self.sighting(win, v2_state)
            assert win._series_total.cget("text")
        finally:
            win.close()

    def test_the_card_total_is_left_alone(self, gui, root, v2_state):
        # It already excludes sighters; it is the series box that misleads.
        win = self.window(gui, v2_state)
        try:
            self.sighting(win, v2_state)
            assert win._card_total.cget("fg") == gui.INK
        finally:
            win.close()

    def test_the_flag_clears_when_a_counting_series_follows(self, gui, root, v2_state):
        win = self.window(gui, v2_state)
        try:
            self.sighting(win, v2_state)
            win.refresh(poll=False)
            assert "SIGHTERS" not in win._strip_name.cget("text")
            assert win._series_total.cget("fg") == gui.CHROME_TEXT
        finally:
            win.close()


class TestFollowingTheConfig:
    """A firing point changed elsewhere has to reach a window already open.

    The fleet dashboard changes a display by writing its configuration. Nothing
    tells the window, so it has to notice -- and until it did, a bulk renumber
    reported success while every screen carried on showing the lane it started
    with.
    """

    def window(self, gui, state, lane, source):
        win = gui.LaneWindow(state, lane, interval_ms=10_000, lane_source=source)
        win.root.withdraw()
        win.refresh(poll=False)
        return win

    def test_a_change_to_the_configuration_reaches_the_screen(self, gui, root, v2_state):
        v2_state.config.lane = "9"
        win = self.window(gui, v2_state, "9", lambda: v2_state.config.lane)
        try:
            assert win.lane == "9"
            v2_state.config.lane = "3"
            win.refresh(poll=False)
            assert win.lane == "3"
        finally:
            win.close()

    def test_the_badge_follows_it(self, gui, root, v2_state):
        v2_state.config.lane = "9"
        win = self.window(gui, v2_state, "9", lambda: v2_state.config.lane)
        try:
            v2_state.config.lane = "3"
            win.refresh(poll=False)
            assert win._lane_badge.cget("text").strip() == "3"
        finally:
            win.close()

    def test_changing_it_at_the_screen_is_not_undone(self, gui, root, v2_state):
        # Compared against the last configured value, not against what is on
        # screen, so a redraw does not drag the lane back.
        v2_state.config.lane = "9"
        win = self.window(gui, v2_state, "9", lambda: v2_state.config.lane)
        try:
            win.set_lane("3")
            win.refresh(poll=False)
            assert win.lane == "3"
        finally:
            win.close()

    def test_a_window_with_no_source_is_left_alone(self, gui, root, v2_state):
        win = self.window(gui, v2_state, "9", None)
        try:
            v2_state.config.lane = "3"
            win.refresh(poll=False)
            assert win.lane == "9"
        finally:
            win.close()

    def test_a_source_that_raises_does_not_take_the_display_with_it(self, gui, root, v2_state):
        def explode():
            raise RuntimeError("no config")

        win = self.window(gui, v2_state, "9", explode)
        try:
            win.refresh(poll=False)
            assert win.lane == "9"
        finally:
            win.close()


class TestTwoScreens:
    """A Pi 4 or Pi 5 has two HDMI sockets; each gets its own firing point."""

    def test_a_second_window_shares_the_first_ones_interpreter(self, gui, root, v2_state):
        # One process, one feed, one beacon, one entry in the fleet.
        first = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        first.root.withdraw()
        second = gui.LaneWindow(v2_state, "3", interval_ms=10_000, parent=first.root)
        try:
            assert second.root.winfo_toplevel() is not first.root
            assert str(second.root.master) == str(first.root)
            second.refresh(poll=False)
            assert second.lane == "3"
            assert first.lane == "9"
        finally:
            second.close()
            first.close()

    def test_each_window_is_placed_on_its_own_output(self, gui, root, v2_state):
        from megalink_viewer.outputs import parse_monitors

        left, right = parse_monitors(
            "Monitors: 2\n"
            " 0: +*HDMI-1 1920/530x1080/300+0+0  HDMI-1\n"
            " 1: +HDMI-2 1920/530x1080/300+1920+0  HDMI-2\n"
        )
        first = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        first.root.withdraw()
        second = gui.LaneWindow(v2_state, "3", interval_ms=10_000, parent=first.root)
        try:
            first.place_on(left)
            second.place_on(right)
            first.root.update_idletasks()
            second.root.update_idletasks()
            # A kiosk window manager would make both fill the whole X screen,
            # which across two sockets is both monitors.
            assert not first._fullscreen
            assert "+1920+0" in second.root.geometry() or second.root.winfo_x() == 1920
        finally:
            second.close()
            first.close()

    def test_the_second_screen_follows_its_own_setting(self, gui, root, v2_state):
        v2_state.config.display.lane2 = "3"
        first = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        first.root.withdraw()
        second = gui.LaneWindow(
            v2_state,
            "3",
            interval_ms=10_000,
            parent=first.root,
            lane_source=lambda: v2_state.config.display.lane2,
        )
        try:
            v2_state.config.display.lane2 = "7"
            second.refresh(poll=False)
            first.refresh(poll=False)
            assert second.lane == "7"
            # ...and does not drag the first screen along with it.
            assert first.lane == "9"
        finally:
            second.close()
            first.close()


class TestLogoScale:
    """Tk scales by whole numbers, so filling a box means zoom-then-subsample."""

    def scale(self, gui, size, box):
        zoom, shrink = gui.logo_scale(size, box)
        return size[0] * zoom // shrink, size[1] * zoom // shrink

    def test_a_small_badge_is_enlarged(self, gui):
        assert self.scale(gui, (100, 100), (500, 500))[0] > 100

    def test_a_large_badge_is_shrunk(self, gui):
        assert self.scale(gui, (2000, 2000), (500, 500))[0] <= 500

    def test_it_never_spills_out_of_the_box(self, gui):
        for size in [(37, 900), (900, 37), (1, 1), (4000, 30), (640, 480)]:
            width, height = self.scale(gui, size, (500.0, 300.0))
            assert width <= 500 and height <= 300, size

    def test_the_aspect_ratio_is_kept(self, gui):
        zoom, shrink = gui.logo_scale((200, 100), (900, 900))
        # One factor for both axes, so the badge cannot be stretched.
        assert (200 * zoom // shrink) / (100 * zoom // shrink) == pytest.approx(2, abs=0.1)

    def test_it_fills_more_of_the_box_than_whole_steps_alone(self, gui):
        # 300 into 540 is 1.8x: a whole-number zoom can only manage 1x.
        assert self.scale(gui, (300, 300), (1152, 540))[0] > 300

    def test_a_badge_over_the_memory_cap_still_fills_its_box(self, gui):
        # The cap is on the zoomed copy. Letting it rule out zoom 1 as well left
        # an over-large badge with no candidate, falling back to a thumbnail.
        big = (2400, 2400)
        assert big[0] * big[1] > gui.LOGO_PIXELS_MAX
        assert self.scale(gui, big, (626.0, 626.0))[0] > 313

    def test_the_zoomed_copy_stays_within_reach_of_a_pi(self, gui):
        zoom, _shrink = gui.logo_scale((1500, 1500), (4000, 4000))
        assert 1500 * zoom * 1500 * zoom <= gui.LOGO_PIXELS_MAX

    def test_a_degenerate_image_is_left_alone(self, gui):
        assert gui.logo_scale((0, 0), (500, 500)) == (1, 1)


class TestTypeScaling:
    """Type has to grow with the window, or a big screen wastes its size."""

    def test_scale_grows_with_the_window(self, gui):
        small = gui.scale_for(640, 420)
        reference = gui.scale_for(*gui.REFERENCE_SIZE)
        large = gui.scale_for(1900, 1200)
        assert small < reference < large

    def test_scale_is_bounded_at_both_ends(self, gui):
        low, high = gui.SCALE_RANGE
        assert gui.scale_for(120, 80) == low
        assert gui.scale_for(9000, 9000) == high

    def test_scale_follows_the_tighter_dimension(self, gui):
        # A wide but short window must not scale as though it were huge.
        assert gui.scale_for(3000, 400) == gui.scale_for(1000, 400)

    def test_every_role_scales(self, gui):
        base = gui.type_sizes(1.0)
        bigger = gui.type_sizes(2.0)
        assert set(base) == set(gui.TYPE)
        for role in base:
            assert bigger[role] > base[role], role

    def test_type_never_gets_unreadably_small(self, gui):
        for size in gui.type_sizes(0.1).values():
            assert size >= 8

    def test_resizing_the_window_resizes_the_score_sheet(self, gui, root, v2_state):
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        try:
            win.root.geometry("700x460")
            win.root.update_idletasks()
            win.rescale(force=True)
            small = {role: font.cget("size") for role, font in win._fonts.items()}
            small_rows = win._ttk_style.lookup("Megalink.Treeview", "rowheight")

            win.root.geometry("1600x1000")
            win.root.update_idletasks()
            win.rescale(force=True)
            large = {role: font.cget("size") for role, font in win._fonts.items()}
            large_rows = win._ttk_style.lookup("Megalink.Treeview", "rowheight")

            for role in small:
                assert large[role] > small[role], role
            # Rows must grow too, or the bigger type is simply clipped.
            assert int(large_rows) > int(small_rows)
        finally:
            win.close()

    def test_rescale_is_skipped_when_the_size_is_unchanged(self, gui, root, v2_state):
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        try:
            win.rescale(force=True)
            before = win._scale
            win._scale = -1.0  # a value rescale() would overwrite if it ran
            win.rescale()
            assert win._scale == -1.0
            win.rescale(force=True)
            assert win._scale == before
        finally:
            win.close()

    def test_the_tables_use_the_scaling_fonts(self, gui, root, v2_state):
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        try:
            win.rescale(force=True)
            configured = win._ttk_style.lookup("Megalink.Treeview", "font")
            assert str(configured) == str(win._fonts["table"])
        finally:
            win.close()


@pytest.mark.usefixtures("scalable_fonts")
class TestFittedType:
    """Type that is sized to its box, not to the window.

    The card total is the case: the box is a fixed half-panel, and the text in
    it runs from "84" to "594.8 (23x)" depending on the discipline.
    """

    @pytest.fixture
    def window(self, gui, root, v2_state):
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        win.root.geometry("1280x720")
        win.root.update_idletasks()
        win.rescale(force=True)
        win.refresh(poll=False)
        yield win
        win.close()

    def box(self, window, name):
        """The room the fitted type has, by the same arithmetic the grid uses."""
        from megalink_viewer.gui import CARD_PAD, CELL_PAD, SHOT_COLUMNS

        panel = window._window_size()[0] / 2 - CARD_PAD
        return panel / 2 if name == "totals" else panel / SHOT_COLUMNS - CELL_PAD

    def test_a_long_total_still_fits_its_cell(self, window):
        window._card_total.configure(text="594.8 (23x)")
        window._fit_type()
        rendered = window._fitted["totals"].font.measure("594.8 (23x)")
        assert rendered <= self.box(window, "totals")

    def test_every_shot_value_fits_its_cell(self, window):
        window._fit_type()
        font = window._fitted["value"].font
        for column in window._shot_cells:
            for cell in column:
                assert font.measure(cell.cget("text")) <= self.box(window, "value")

    def test_a_long_total_is_set_smaller_than_a_short_one(self, window):
        window._card_total.configure(text="9")
        window._fit_type()
        short = window._fitted["totals"].font.cget("size")

        window._card_total.configure(text="594.8 (23x) 594.8 (23x)")
        window._fit_type()
        assert window._fitted["totals"].font.cget("size") < short

    def test_the_two_totals_are_set_at_one_size(self, window):
        # Fitting them separately would size neighbouring figures differently.
        assert window._series_total.cget("font") == window._card_total.cget("font")

    def test_a_short_total_is_not_enlarged_past_the_window(self, window):
        window._card_total.configure(text="8")
        window._fit_type()
        ceiling = gui_type_ceiling(window)
        assert window._fitted["totals"].font.cget("size") <= ceiling

    def test_shot_values_are_larger_than_the_table_type(self, window):
        # The grid is read from a firing point away, unlike a listing.
        assert gui_size(window, "value") > gui_size(window, "table")

    def test_the_shot_grid_uses_the_fitted_font(self, window):
        cell = window._shot_cells[0][0]
        assert cell.cget("font") == str(window._fitted["value"].font)

    def test_fitting_survives_an_unlaid_out_window(self, gui, root, v2_state):
        # Before the first layout the window reports no size at all, which must
        # not be read as "nothing fits" and collapse the type.
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        try:
            win._fit_type()
            assert win._fitted["totals"].font.cget("size") >= 8
        finally:
            win.close()

    def test_fitting_stops_at_a_readable_floor(self, gui, root, v2_state):
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        try:
            win.root.geometry("640x420")
            win.root.update_idletasks()
            win.rescale(force=True)
            win._card_total.configure(text="x" * 400)
            win._fit_type()
            assert win._fitted["totals"].font.cget("size") >= 8
        finally:
            win.close()


def gui_size(window, role):
    """The size a role's type is currently set at."""
    fitted = window._fitted.get(role)
    return (fitted.font if fitted else window._fonts[role]).cget("size")


def gui_type_ceiling(window):
    """The largest the totals may be set at this window size."""
    from megalink_viewer.gui import type_sizes

    return type_sizes(window._scale)["total"]


class TestLaneWindow:
    @pytest.fixture
    def window(self, gui, root, v2_state):
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        win.refresh(poll=False)
        yield win
        win.close()

    def cells(self, window):
        """The shot grid's text, column by column, blanks dropped."""
        return [
            cell.cget("text")
            for column in window._shot_cells
            for cell in column
            if cell.cget("text")
        ]

    def strip(self, window):
        return [cell.cget("text") for cell in window._strip_cells]

    # -- header ------------------------------------------------------------

    def test_the_shooter_is_named(self, window):
        assert "Etai Moredehi Bogen" in window._shooter.cget("text")

    def test_the_club_is_the_centrepiece(self, window):
        assert window._host.cget("text") == "Stord PK"

    def test_the_range_and_relay_are_shown(self, window):
        text = window._range_line.cget("text")
        assert "Relay:" in text

    def test_the_lane_is_badged(self, window):
        assert window._lane_badge.cget("text").strip() == "9"

    def test_the_clock_has_a_box_of_its_own(self, window):
        # In the card panel, not tucked into the header corner: during a timed
        # match it is the second thing a shooter looks for after the score.
        assert window._clock.winfo_parent() == str(window._shot_table.master)
        assert window._clock.cget("text")

    def test_a_running_clock_shows_its_reading(self, window, gui, v2_state):
        from megalink_viewer.models import Clock, Stage

        result = v2_state.lane_view("9").result
        result.clock = Clock(
            running=True,
            start_ms=int(gui._now_ms()) - 10_000,
            stages=[Stage("SHOOTING", counting_up=False, offset=0, start_count=60_000)],
        )
        window._fill_clock(result)
        assert ":" in window._clock.cget("text")

    def test_a_clock_that_is_not_running_says_so(self, window, v2_state):
        result = v2_state.lane_view("9").result
        result.clock = None
        window._fill_clock(result)
        assert window._clock.cget("text") == "—"

    # -- the series strip --------------------------------------------------

    def test_the_strip_names_the_current_series(self, window, v2_state):
        active = v2_state.lane_view("9").result.active_series
        assert window._strip_name.cget("text") == active.name

    def test_the_strip_shows_the_cards_series_totals(self, window, v2_state):
        """Megalink's own summary of the card, across the top."""
        summary = v2_state.lane_view("9").result.series_summary
        assert summary, "this fixture should carry a card summary"
        shown = [text for text in self.strip(window) if text]
        assert shown
        for text in shown:
            assert text in [value.display() for _name, value in summary]

    def test_the_strip_never_overflows_its_cells(self, window):
        assert len(window._strip_cells) == gui_strip_cells()

    # -- the shot grid -----------------------------------------------------

    def test_every_shot_is_numbered(self, window, v2_state):
        active = v2_state.lane_view("9").result.active_series
        texts = self.cells(window)
        assert len(texts) == len(active.shots)
        for text in texts:
            assert ":" in text

    def test_the_grid_is_filled_down_each_column_in_turn(self, window, v2_state):
        """Megalink fills a column before starting the next; so does this."""
        numbers = [int(text.split(":")[0]) for text in self.cells(window)]
        assert numbers == sorted(numbers)

    def test_every_cell_is_the_same_size(self, window):
        """Ragged cells were the reason rows did not line up."""
        window.root.update_idletasks()
        widths = {cell.winfo_reqwidth() > 0 for column in window._shot_cells for cell in column}
        assert widths == {True}
        assert len(window._shot_cells) == gui_shot_columns()
        assert all(len(column) == gui_shot_rows() for column in window._shot_cells)

    def test_the_grid_holds_exactly_an_issf_series(self, gui):
        # Ten shots to a series, six series to a 60-shot 10m card. A grid that
        # held eleven or nine would have the shots shifting between cells as
        # the series went on, which is what makes a table hard to read at a
        # glance from the firing point.
        assert gui.SHOT_COLUMNS * gui.SHOT_ROWS == 10
        assert gui.STRIP_CELLS * 10 == 60

    def test_a_full_series_needs_no_scrolling(self, gui, root, v2_state):
        view = v2_state.lane_view("9")
        series = view.result.active_series
        series.shots = series.shots[:1] * 10
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        try:
            win._fill_shot_table(series)
            numbers = [int(text.split(":")[0]) for text in self.cells(win)]
            assert numbers == list(range(1, 11))
        finally:
            win.close()

    def test_the_newest_shot_stands_out(self, window):
        colours = [
            cell.cget("fg") for column in window._shot_cells for cell in column if cell.cget("text")
        ]
        assert colours[-1] != colours[0] or len(colours) == 1

    def test_a_long_series_shows_the_most_recent_shots(self, gui, root, v2_state):
        """A 60-shot card cannot fit; the ones just fired are what matter."""
        view = v2_state.lane_view("9")
        longest = max(view.result.series, key=lambda s: len(s.shots))
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        win._series_choice = longest.index
        win.refresh(poll=False)
        try:
            numbers = [int(t.split(":")[0]) for t in self.cells(win)]
            if len(longest.shots) > gui_shot_columns() * gui_shot_rows():
                assert numbers[-1] == len(longest.shots)
                assert numbers[0] > 1
        finally:
            win.close()

    def test_an_empty_series_leaves_the_grid_blank(self, gui, root, v2_state):
        win = gui.LaneWindow(v2_state, "3", interval_ms=10_000)
        win.root.withdraw()
        win.refresh(poll=False)
        try:
            assert self.cells(win) == []
        finally:
            win.close()

    # -- totals ------------------------------------------------------------

    def test_the_card_total_is_shown(self, window):
        assert window._card_total.cget("text") == "84 (1x)"

    def test_the_series_total_is_shown(self, window, v2_state):
        active = v2_state.lane_view("9").result.active_series
        assert active.total.display() in window._series_total.cget("text")

    def test_the_card_total_is_the_one_picked_out(self, window):
        """Reversed out, as Megalink reverses it."""
        assert window._card_total.cget("bg") != window._series_total.cget("bg")

    # -- moving about ------------------------------------------------------

    def test_switching_lane_redraws(self, window, v2_state):
        other = next(lane for lane in v2_state.lanes() if lane != "9")
        window.set_lane(other)
        assert window._lane_badge.cget("text").strip() == other
        assert window._shooter.cget("text").startswith(v2_state.lane_view(other).shooter_name)

    def test_stepping_through_lanes_wraps(self, window, v2_state):
        lanes = v2_state.lanes()
        window.set_lane(lanes[-1])
        window.step_lane(1)
        assert window._lane == lanes[0]

    def test_stepping_back_through_series(self, window, v2_state):
        result = v2_state.lane_view("9").result
        if len(result.series) < 2:
            pytest.skip("this fixture has a single series")
        window.step_series(-1)
        assert window._series_choice is not None
        window.step_series(1)
        assert window._series_choice is None, "the last series follows the live one"

    def test_fresh_data_reads_live(self, window):
        assert "live" in window._status.cget("text")

    def test_the_status_counts_the_shots(self, window, v2_state):
        expected = v2_state.lane_view("9").result.shot_count
        assert f"{expected} shots" in window._status.cget("text")

    def test_stale_data_is_flagged(self, gui, root, v2_state):
        v2_state._age = 90.0
        win = gui.LaneWindow(v2_state, "9", interval_ms=10_000)
        win.root.withdraw()
        win.refresh(poll=False)
        try:
            assert "90s ago" in win._status.cget("text")
        finally:
            win.close()

    def test_an_empty_lane_renders(self, gui, root, v2_state):
        win = gui.LaneWindow(v2_state, "3", interval_ms=10_000)
        win.root.withdraw()
        win.refresh(poll=False)
        try:
            assert win._shooter.cget("text").startswith("Lane 3")
        finally:
            win.close()


def gui_strip_cells():
    from megalink_viewer import gui as module

    return module.STRIP_CELLS


def gui_shot_columns():
    from megalink_viewer import gui as module

    return module.SHOT_COLUMNS


def gui_shot_rows():
    from megalink_viewer import gui as module

    return module.SHOT_ROWS


class TestLegacyProtocolInTheWindow:
    def test_a_v1_range_renders(self, gui, root, v1_dfs):
        from megalink_viewer.client import LIVE_DB

        source = Source(1, LIVE_DB, "data/nidaros-skl", "nidaros-skl", "15m")
        state = FakeState(v1_dfs, source, v1.lanes(v1_dfs, "15m"))
        win = gui.LaneWindow(state, "2", interval_ms=10_000)
        win.root.withdraw()
        win.refresh(poll=False)
        try:
            assert win._card_total.cget("text") == "238 (5x)"
            assert win._shooter.cget("text").startswith("Lane 2")
            # The legacy feed publishes no card summary, so the strip falls back
            # to the counting series' own totals.
            shown = [c.cget("text") for c in win._strip_cells if c.cget("text")]
            assert shown
        finally:
            win.close()


class TestNoData:
    def test_a_view_with_nothing_in_it(self, gui, root):
        class Empty:
            source = None

            def lane_view(self, lane):
                return LaneView(str(lane))

            def lanes(self):
                return []

            def age(self):
                return None

        win = gui.LaneWindow(Empty(), "1", interval_ms=10_000)
        win.root.withdraw()
        win.refresh(poll=False)
        try:
            assert win._card_total.cget("text") == ""
            assert "waiting" in win._status.cget("text")
        finally:
            win.close()


class TestSetupScreen:
    """A display with nothing to show says how to set it up.

    The person in front of it has probably never done this, and has no keyboard
    to try anything with -- so the screen gives them an address and a code their
    phone can scan, and nothing else.
    """

    URL = "http://192.168.1.23:8080/"

    def reach(self, url_address="192.168.1.23", port=8080):
        from megalink_viewer.address import Reach

        addresses = [("wlan0", url_address)] if url_address else []
        return Reach("fp-09", addresses, port, primary_address=url_address)

    def window(self, gui, state, reach=None, calls=None):
        def find(port):
            if calls is not None:
                calls.append(port)
            return reach if reach is not None else self.reach(port=port)

        win = gui.LaneWindow(state, "9", interval_ms=10_000, find_reach=find)
        win.root.withdraw()
        win.root.geometry("1280x720")
        win.root.update_idletasks()
        win.refresh(poll=False)
        return win

    def unconfigured(self, state):
        state.config.host = ""
        state.config.lane = ""
        return state

    def test_an_unconfigured_display_is_covered_by_the_setup_screen(self, gui, root, v2_state):
        win = self.window(gui, self.unconfigured(v2_state))
        try:
            assert win._setup.winfo_manager() == "place"
            assert win._setup_url.cget("text") == self.URL
        finally:
            win.close()

    def test_a_configured_display_is_not(self, gui, root, v2_state):
        win = self.window(gui, v2_state)
        try:
            assert win._setup.winfo_manager() == ""
        finally:
            win.close()

    def test_the_local_name_is_offered_for_people(self, gui, root, v2_state):
        win = self.window(gui, self.unconfigured(v2_state))
        try:
            assert "fp-09.local" in win._setup_local.cget("text")
        finally:
            win.close()

    def test_the_code_is_the_address_drawn_module_for_module(self, gui, root, v2_state):
        from megalink_viewer import qr

        win = self.window(gui, self.unconfigured(v2_state))
        try:
            canvas = win._setup_code
            squares = [i for i in canvas.find_all() if canvas.type(i) == "rectangle"]
            assert len(squares) == sum(sum(row) for row in qr.modules(self.URL))
            # Every module the same whole number of pixels, or a scanner struggles.
            sizes = {
                (round(x1 - x0), round(y1 - y0))
                for x0, y0, x1, y1 in (canvas.coords(i) for i in squares)
            }
            assert len(sizes) == 1
            ((w, h),) = sizes
            assert w == h and w >= 2
        finally:
            win.close()

    def test_no_network_says_so_and_shows_no_code(self, gui, root, v2_state):
        win = self.window(gui, self.unconfigured(v2_state), reach=self.reach(url_address=None))
        try:
            assert "Waiting for a network" in win._setup_lead.cget("text")
            assert win._setup_code.winfo_manager() == ""
        finally:
            win.close()

    def test_no_network_counts_down_to_the_hotspot(self, gui, root, v2_state):
        # So a screen that has said "waiting" for a while is plainly alive.
        status = {"mode": "waiting", "hotspot_in": 20.0, "updated": time.time()}
        win = gui.LaneWindow(
            self.unconfigured(v2_state),
            "9",
            interval_ms=10_000,
            find_reach=lambda port: self.reach(url_address=None, port=port),
            read_network=lambda: status,
        )
        try:
            win.root.withdraw()
            win.refresh(poll=False)
            note = win._setup_note.cget("text")
            assert "in 20 seconds it will start its own Wi-Fi" in note
            assert "network cable" in note
        finally:
            win.close()

    def test_no_network_and_no_fallback_still_mentions_a_cable(self, gui, root, v2_state):
        win = self.window(gui, self.unconfigured(v2_state), reach=self.reach(url_address=None))
        try:
            note = win._setup_note.cget("text")
            assert "not on a network yet" in note and "network cable" in note
            assert "Check the network name" not in note
        finally:
            win.close()

    def test_a_display_with_its_page_turned_off_says_so(self, gui, root, v2_state):
        state = self.unconfigured(v2_state)
        state.config.web.enabled = False
        win = self.window(gui, state)
        try:
            assert "no" in win._setup_lead.cget(
                "text"
            ) and "configuration page" in win._setup_lead.cget("text")
        finally:
            win.close()

    def test_the_setup_screen_covers_the_idle_one(self, gui, root, v2_state):
        # Not both: a display with no club has no position to be idle on.
        win = self.window(gui, self.unconfigured(v2_state))
        try:
            assert win._setup.winfo_manager() == "place"
            assert win._idle.winfo_manager() == ""
        finally:
            win.close()

    def test_the_address_is_not_looked_up_every_frame(self, gui, root, v2_state):
        calls = []
        win = self.window(gui, self.unconfigured(v2_state), calls=calls)
        try:
            for _ in range(5):
                win.refresh(poll=False)
            assert len(calls) == 1
        finally:
            win.close()

    def test_the_code_is_not_redrawn_when_nothing_changed(self, gui, root, v2_state):
        win = self.window(gui, self.unconfigured(v2_state))
        try:
            before = win._setup_code.find_all()
            win.refresh(poll=False)
            assert win._setup_code.find_all() == before
        finally:
            win.close()

    def test_setting_the_display_up_takes_the_screen_away(self, gui, root, v2_state):
        win = self.window(gui, self.unconfigured(v2_state))
        try:
            v2_state.config.host = "stord-pk"
            v2_state.config.lane = "9"
            win.refresh(poll=False)
            assert win._setup.winfo_manager() == ""
        finally:
            win.close()

    def test_an_idle_position_shows_where_its_settings_are(self, gui, root, v2_state):
        # Lane 3 has nobody on it; somebody walking up may want to change it.
        win = gui.LaneWindow(
            v2_state, "3", interval_ms=10_000, find_reach=lambda port: self.reach(port=port)
        )
        win.root.withdraw()
        try:
            win.refresh(poll=False)
            assert win._idle.winfo_manager() == "place"
            assert win._idle_reach.cget("text") == f"Settings: {self.URL}"
        finally:
            win.close()


class TestHotspotScreen:
    """On its own Wi-Fi, a display gives the two steps to reach it."""

    SPOT: ClassVar[dict[str, Any]] = {
        "mode": "hotspot",
        "hotspot": {"ssid": "Megalink fp-09", "password": "7kqm-xw4p-9ht2", "address": "10.42.0.1"},
    }

    def window(self, gui, state, status):
        from megalink_viewer.address import Reach

        win = gui.LaneWindow(
            state,
            "9",
            interval_ms=10_000,
            find_reach=lambda port: Reach("fp-09", [("wlan0", "10.42.0.1")], port),
            read_network=lambda: status,
        )
        win.root.withdraw()
        win.root.geometry("1280x720")
        win.root.update_idletasks()
        win.refresh(poll=False)
        return win

    def codes(self, canvas):
        return len([i for i in canvas.find_all() if canvas.type(i) == "rectangle"])

    def test_a_configured_display_on_its_hotspot_still_shows_how_to_reach_it(
        self, gui, root, v2_state
    ):
        # It cannot show scores while it is off the network.
        win = self.window(gui, v2_state, self.SPOT)
        try:
            assert win._setup.winfo_manager() == "place"
            assert win._setup_title.cget("text") == "Connect to this display"
        finally:
            win.close()

    def test_step_one_is_joining_the_hotspot(self, gui, root, v2_state):
        from megalink_viewer import qr

        win = self.window(gui, v2_state, self.SPOT)
        try:
            assert win._setup_url.cget("text") == "Megalink fp-09"
            assert "7kqm-xw4p-9ht2" in win._setup_local.cget("text")
            # A code a phone's camera joins the network from.
            wifi = qr.wifi("Megalink fp-09", "7kqm-xw4p-9ht2")
            assert self.codes(win._setup_code) == sum(sum(row) for row in qr.modules(wifi))
        finally:
            win.close()

    def test_step_two_is_opening_the_settings_on_it(self, gui, root, v2_state):
        from megalink_viewer import qr

        win = self.window(gui, v2_state, self.SPOT)
        try:
            url = "http://10.42.0.1:8080/"
            assert url in win._setup_step2.cget("text")
            assert win._setup_code2.winfo_manager() == "grid"
            assert self.codes(win._setup_code2) == sum(sum(row) for row in qr.modules(url))
        finally:
            win.close()

    def test_off_the_hotspot_the_second_step_goes(self, gui, root, v2_state):
        v2_state.config.host = ""
        win = self.window(gui, v2_state, {"mode": "client", "hotspot": None})
        try:
            assert win._setup.winfo_manager() == "place"  # still unconfigured
            assert win._setup_code2.winfo_manager() == ""
            assert win._setup_step2.winfo_manager() == ""
        finally:
            win.close()

    def test_a_configured_display_on_a_network_is_left_alone(self, gui, root, v2_state):
        win = self.window(gui, v2_state, {"mode": "client", "hotspot": None})
        try:
            assert win._setup.winfo_manager() == ""
        finally:
            win.close()

    def test_no_hotspot_service_at_all_is_left_alone(self, gui, root, v2_state):
        # A machine without it -- a laptop, an older install.
        win = self.window(gui, v2_state, None)
        try:
            assert win._setup.winfo_manager() == ""
        finally:
            win.close()

    def test_an_open_hotspot_says_so(self, gui, root, v2_state):
        spot = {"mode": "hotspot", "hotspot": {"ssid": "Megalink fp-09", "password": ""}}
        win = self.window(gui, v2_state, spot)
        try:
            assert win._setup_local.cget("text") == "no password"
        finally:
            win.close()
