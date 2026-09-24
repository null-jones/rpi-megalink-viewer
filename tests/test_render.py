"""The per-position display."""

from __future__ import annotations

from typing import ClassVar

from megalink_viewer import v1, v2
from megalink_viewer.models import LaneView
from megalink_viewer.render import (
    GLYPH_HEIGHT,
    big_text,
    colorize,
    format_clock,
    render_lane,
    render_lane_screen,
    visible_len,
)


class TestBigText:
    def test_height_is_fixed(self):
        assert len(big_text("618.4")) == GLYPH_HEIGHT

    def test_rows_are_equal_width(self):
        rows = big_text("10.6x")
        assert len({len(r) for r in rows}) == 1

    def test_unknown_characters_become_blanks(self):
        rows = big_text("?")
        assert set("".join(rows)) <= {" "}

    def test_empty_string(self):
        assert big_text("") == [""] * GLYPH_HEIGHT


class TestFormatClock:
    def test_no_reading(self):
        assert format_clock(None) == "--:--"

    def test_minutes_and_seconds(self):
        assert format_clock(83_000) == "01:23"

    def test_rounds_down_to_whole_seconds(self):
        assert format_clock(59_999) == "00:59"

    def test_hours(self):
        assert format_clock(3_725_000) == "1:02:05"

    def test_negative_is_clamped(self):
        assert format_clock(-5_000) == "00:00"


class TestVisibleLen:
    def test_ignores_ansi(self):
        assert visible_len(colorize("abc", "red")) == 3

    def test_plain_text(self):
        assert visible_len("abc") == 3


class TestRenderLane:
    def test_lines_fit_the_requested_width(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        width = 72
        for line in render_lane(view, width=width):
            assert visible_len(line) <= width, repr(line)

    def test_shows_the_shooter_and_the_total(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        text = "\n".join(render_lane(view, width=90))
        assert "Etai Moredehi Bogen" in text
        assert "LANE 9" in text
        assert "Stord PK" in text

    def test_total_is_rendered_in_block_digits(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        text = "\n".join(render_lane(view, width=90))
        # The card total is 84; block digits use the full block character.
        assert "█" in text
        assert "TOTAL" in text

    def test_series_lines_carry_shots_and_totals(self, v1_dfs):
        view = v1.lane_view(v1_dfs, "15m", "2")
        text = "\n".join(render_lane(view, width=100))
        assert "1. Serie Liggende" in text
        assert "10.8x" in text

    def test_active_series_is_marked(self, v1_dfs):
        view = v1.lane_view(v1_dfs, "15m", "2")
        lines = render_lane(view, width=100)
        assert any(line.startswith("▸") for line in lines)

    def test_shots_are_dropped_whole_when_they_do_not_fit(self, v1_dfs):
        view = v1.lane_view(v1_dfs, "15m", "2")
        # Narrow enough that the ten-shot series must be trimmed.
        text = "\n".join(render_lane(view, width=52))
        assert "…" in text
        # A trimmed line must not leave a half-printed value like "….2".
        assert "….2" not in text

    def test_empty_view_still_renders(self):
        lines = render_lane(LaneView("7"), width=60)
        text = "\n".join(lines)
        assert "LANE 7" in text
        assert "waiting for range" in text

    def test_no_data_is_reported_in_the_footer(self):
        text = "\n".join(render_lane(LaneView("1"), width=60, age=None))
        assert "no data" in text

    def test_stale_data_is_reported_in_the_footer(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        assert "42s ago" in "\n".join(render_lane(view, width=80, age=42.0))

    def test_fresh_data_reads_live(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        assert "live" in "\n".join(render_lane(view, width=80, age=0.5))

    def test_colour_is_opt_in(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        assert "\x1b[" not in "\n".join(render_lane(view, width=80, color=False))
        assert "\x1b[" in "\n".join(render_lane(view, width=80, color=True))

    def test_colour_does_not_break_alignment(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        width = 80
        for line in render_lane(view, width=width, color=True):
            assert visible_len(line) <= width, repr(line)

    def test_very_narrow_width_is_survivable(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        lines = render_lane(view, width=10)
        assert lines


class TestRenderScreen:
    def test_pads_to_the_requested_height(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        screen = render_lane_screen(view, width=80, height=30)
        assert screen.count("\n") == 29

    def test_clips_to_the_requested_height(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        screen = render_lane_screen(view, width=80, height=8)
        assert screen.count("\n") == 7


class TestFrame:
    """A console display must redraw over itself, not scroll away."""

    def test_a_terminal_frame_redraws_in_place(self, v2_pistol):
        from megalink_viewer.render import CLEAR_BELOW, CLEAR_TO_EOL, HOME, frame

        view = v2.lane_view(v2_pistol, "1-10", "9")
        out = frame(view, width=80, height=24, interactive=True)
        assert out.startswith(HOME)
        assert CLEAR_TO_EOL in out
        assert out.endswith(CLEAR_BELOW)

    def test_a_terminal_frame_fills_the_height_exactly(self, v2_pistol):
        from megalink_viewer.render import frame

        view = v2.lane_view(v2_pistol, "1-10", "9")
        out = frame(view, width=80, height=24, interactive=True)
        assert out.count("\n") == 23

    def test_a_piped_frame_has_no_escape_sequences(self, v2_pistol):
        from megalink_viewer.render import frame

        view = v2.lane_view(v2_pistol, "1-10", "9")
        out = frame(view, width=80, height=24, interactive=False)
        assert "\x1b" not in out
        assert out.endswith("\n\n")

    def test_an_error_is_appended(self, v2_pistol):
        from megalink_viewer.render import frame

        view = v2.lane_view(v2_pistol, "1-10", "9")
        out = frame(view, width=80, height=24, extra="the feed is unreachable")
        assert "the feed is unreachable" in out


class TestHotspotOnTheConsole:
    SPOT: ClassVar[dict[str, str]] = {
        "ssid": "Megalink fp-09",
        "password": "7kqm-xw4p-9ht2",
        "address": "10.42.0.1",
    }

    def reach(self):
        from megalink_viewer.address import Reach

        return Reach("fp-09", [("wlan0", "10.42.0.1")], 8080)

    def test_it_gives_both_steps(self):
        from megalink_viewer.render import render_setup

        text = "\n".join(render_setup(self.reach(), 120, 60, hotspot=self.SPOT))
        assert "Megalink fp-09" in text and "7kqm-xw4p-9ht2" in text
        assert "http://10.42.0.1:8080/" in text

    def test_with_no_network_it_counts_down_to_the_hotspot(self):
        import time

        from megalink_viewer.address import Reach
        from megalink_viewer.render import render_setup

        status = {"mode": "waiting", "hotspot_in": 20.0, "updated": time.time()}
        text = " ".join(
            line.strip()
            for line in render_setup(Reach("fp-09", [], 8080), 60, 40, network_status=status)
        )
        assert "Waiting for a network" in text
        assert "in 20 seconds it will start its own Wi-Fi" in text
        assert "network cable" in text

    def test_the_code_is_for_joining_the_network(self):
        from megalink_viewer import qr
        from megalink_viewer.render import render_setup

        lines = render_setup(self.reach(), 120, 60, interactive=True, hotspot=self.SPOT)
        code = qr.terminal(qr.wifi("Megalink fp-09", "7kqm-xw4p-9ht2"))
        assert code[0] in "".join(lines)

    def test_no_code_in_a_log(self):
        from megalink_viewer.render import render_setup

        lines = render_setup(self.reach(), 120, 60, interactive=False, hotspot=self.SPOT)
        assert not any("\x1b[" in line for line in lines)
