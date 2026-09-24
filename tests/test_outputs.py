"""Finding the screens attached to a Pi with two HDMI sockets."""

from __future__ import annotations

from megalink_viewer.outputs import Screen, describe, parse_monitors, screens

# What a Pi 4 Model B reports with both micro-HDMI sockets in use.
PI4 = """Monitors: 2
 0: +*HDMI-1 1920/530x1080/300+0+0  HDMI-1
 1: +HDMI-2 1920/530x1080/300+1920+0  HDMI-2
"""

# A Pi 5 with a 4K panel on the left and the primary on the right, listed by
# xrandr in the other order.
PI5_MIXED = """Monitors: 2
 0: +*HDMI-A-1 1920/600x1080/340+3840+0  HDMI-A-1
 1: +HDMI-A-2 3840/700x2160/390+0+0  HDMI-A-2
"""

ONE = """Monitors: 1
 0: +*HDMI-1 1280/340x720/190+0+0  HDMI-1
"""


class TestParsing:
    def test_both_outputs_are_found(self):
        found = parse_monitors(PI4)
        assert [s.name for s in found] == ["HDMI-1", "HDMI-2"]

    def test_each_output_knows_its_size_and_place(self):
        left, right = parse_monitors(PI4)
        assert (left.width, left.height, left.x, left.y) == (1920, 1080, 0, 0)
        assert (right.width, right.height, right.x, right.y) == (1920, 1080, 1920, 0)

    def test_the_millimetre_sizes_are_not_mistaken_for_pixels(self):
        # "1920/530x1080/300" is 1920x1080 pixels on a 530x300mm panel.
        left = parse_monitors(PI4)[0]
        assert (left.width, left.height) == (1920, 1080)

    def test_an_output_with_no_millimetres_still_parses(self):
        found = parse_monitors("Monitors: 1\n 0: +*XWAYLAND0 1920x1080+0+0  XWAYLAND0\n")
        assert found == [Screen("XWAYLAND0", 1920, 1080, 0, 0)]

    def test_screens_are_ordered_left_to_right(self):
        # Not in xrandr's order: "the first screen" should mean the one on the
        # left of the bench, and keep meaning that when a cable is moved.
        found = parse_monitors(PI5_MIXED)
        assert [s.name for s in found] == ["HDMI-A-2", "HDMI-A-1"]
        assert [s.x for s in found] == [0, 3840]

    def test_the_primary_is_noted(self):
        assert [s.primary for s in parse_monitors(PI4)] == [True, False]

    def test_a_single_screen_is_a_list_of_one(self):
        assert len(parse_monitors(ONE)) == 1

    def test_nothing_sensible_gives_nothing(self):
        assert parse_monitors("") == []
        assert parse_monitors("bash: xrandr: command not found") == []
        assert parse_monitors("Monitors: 0\n") == []

    def test_geometry_is_what_tk_wants(self):
        assert parse_monitors(PI4)[1].geometry == "1920x1080+1920+0"


class TestAsking:
    """Not being able to ask is an answer, not a crash."""

    def test_a_missing_xrandr_is_not_an_error(self):
        def explode():
            raise FileNotFoundError("xrandr")

        assert screens(run=explode) == []

    def test_an_empty_answer_is_not_an_error(self):
        assert screens(run=lambda: "") == []

    def test_a_real_answer_comes_through(self):
        assert len(screens(run=lambda: PI4)) == 2

    def test_it_says_what_it_found(self):
        assert describe(parse_monitors(PI4)) == (
            "screens: HDMI-1 1920x1080+0+0, HDMI-2 1920x1080+1920+0"
        )

    def test_it_says_when_it_found_nothing(self):
        assert describe([]) == "no screens reported by xrandr"


# ``xrandr --query`` on a Pi 4 just after X has started with no configuration:
# both outputs at the origin, showing the same thing.
QUERY_MIRRORED = """\
Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 7680 x 7680
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+  50.00    59.94
   1280x720      60.00    50.00    59.94
HDMI-2 connected 1280x720+0+0 (normal left inverted right x axis y axis) 344mm x 194mm
   1280x720      60.00*+
   1024x768      60.00
"""

QUERY_APART = """\
Screen 0: minimum 320 x 200, current 3200 x 1080, maximum 7680 x 7680
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+
HDMI-2 connected 1280x720+1920+0 (normal left inverted right x axis y axis) 344mm x 194mm
   1280x720      60.00*+
"""

QUERY_ONE = """\
Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 7680 x 7680
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+
HDMI-2 disconnected (normal left inverted right x axis y axis)
"""


class TestLayingOut:
    """Two screens have to be put side by side before two windows can be."""

    def test_only_outputs_with_a_screen_count(self):
        from megalink_viewer.outputs import parse_query

        assert [s.name for s in parse_query(QUERY_ONE)] == ["HDMI-1"]

    def test_the_outputs_are_read_with_where_they_are(self):
        from megalink_viewer.outputs import parse_query

        found = parse_query(QUERY_MIRRORED)
        assert [(s.name, s.geometry) for s in found] == [
            ("HDMI-1", "1920x1080+0+0"),
            ("HDMI-2", "1280x720+0+0"),
        ]

    def test_in_socket_order_whatever_order_xrandr_says(self):
        from megalink_viewer.outputs import parse_query

        text = "HDMI-2 connected 1280x720+0+0 (x)\nHDMI-1 connected 1920x1080+0+0 (x)\n"
        assert [s.name for s in parse_query(text)] == ["HDMI-1", "HDMI-2"]

    def test_an_output_with_nothing_showing_yet_is_still_plugged_in(self):
        from megalink_viewer.outputs import parse_query

        (screen,) = parse_query("HDMI-2 connected (normal left inverted right)\n")
        assert (screen.name, screen.width) == ("HDMI-2", 0)

    def test_mirrored_screens_are_put_side_by_side(self):
        # What X does with no configuration, and why both screens showed the
        # same firing point on the first image.
        from megalink_viewer.outputs import parse_query, side_by_side

        assert side_by_side(parse_query(QUERY_MIRRORED)) == [
            "xrandr",
            "--output",
            "HDMI-1",
            "--auto",
            "--pos",
            "0x0",
            "--output",
            "HDMI-2",
            "--auto",
            "--right-of",
            "HDMI-1",
        ]

    def test_screens_already_apart_are_left_alone(self):
        from megalink_viewer.outputs import parse_query, side_by_side

        assert side_by_side(parse_query(QUERY_APART)) is None

    def test_one_screen_needs_nothing(self):
        from megalink_viewer.outputs import parse_query, side_by_side

        assert side_by_side(parse_query(QUERY_ONE)) is None

    def test_arranging_runs_the_command_and_says_so(self):
        from megalink_viewer.outputs import arrange

        ran = []
        said = arrange(
            run_query=lambda: QUERY_MIRRORED, run=lambda command: ran.append(command) or True
        )
        assert ran and ran[0][0] == "xrandr"
        assert said == "HDMI-1 on the left, HDMI-2 on the right"

    def test_a_failure_is_said_too(self):
        from megalink_viewer.outputs import arrange

        said = arrange(run_query=lambda: QUERY_MIRRORED, run=lambda command: False)
        assert said.startswith("could not arrange the screens")

    def test_nothing_to_do_runs_nothing(self):
        from megalink_viewer.outputs import arrange

        ran = []
        assert "already side by side" in arrange(run_query=lambda: QUERY_APART, run=ran.append)
        assert "1 screen(s)" in arrange(run_query=lambda: QUERY_ONE, run=ran.append)
        assert ran == []

    def test_no_xrandr_is_no_screens(self):
        from megalink_viewer.outputs import connected

        def explode():
            raise FileNotFoundError("xrandr")

        assert connected(explode) == []
