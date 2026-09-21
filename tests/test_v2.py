"""The current feed adapter, checked against snapshots of live ranges."""

from __future__ import annotations

import pytest

from megalink_viewer import v2
from megalink_viewer.models import MATCH, SIGHT


class TestRange:
    def test_range_metadata(self, v2_pistol):
        (range_,) = v2.ranges(v2_pistol, "1-10")
        assert range_.key == "1-10"
        assert range_.name == "1-10"
        assert range_.host_name == "Stord PK"
        assert range_.gun_type == "PISTOL"
        assert range_.protocol == 2
        assert range_.version

    def test_title_comes_from_the_range_program(self, v2_pistol):
        (range_,) = v2.ranges(v2_pistol, "1-10")
        assert range_.title == "NSF 25m NAIS"

    def test_relay_is_taken_from_the_firing_points(self, v2_pistol):
        (range_,) = v2.ranges(v2_pistol, "1-10")
        assert range_.relay > 0

    def test_empty_tree(self):
        assert v2.ranges({}) == []


class TestLanes:
    def test_lanes_follow_the_ranges_own_list(self, v2_pistol):
        lanes = v2.lanes(v2_pistol)
        assert lanes == [str(v) for v in v2_pistol["data"]["fpList"]]

    def test_lanes_of_an_empty_tree(self):
        assert v2.lanes({}) == []


class TestPistolRange:
    """Live 25m pistol with named shooters."""

    def test_named_shooter(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "9")
        assert view.shooter.name == "Etai Moredehi Bogen"
        assert view.shooter.occupied
        assert view.shooter_name == "Etai Moredehi Bogen"
        assert view.shooter.gun == "PISTOL"

    def test_card_total_is_the_ranges_own(self, v2_pistol):
        result = v2.lane_view(v2_pistol, "1-10", "9").result
        assert result.total.value == 84
        assert result.total.inner == 1

    def test_series_total_is_the_ranges_own(self, v2_pistol):
        result = v2.lane_view(v2_pistol, "1-10", "9").result
        sight = result.series[0]
        assert sight.kind == SIGHT
        assert sight.total.value == 46
        assert sight.total.inner == 1

    def test_published_series_total_agrees_with_the_shots(self, v2_pistol):
        """Cross-check: 9.4 + 10.7x + 8.9 + 9.1 + 10.0 truncates to 46."""
        result = v2.lane_view(v2_pistol, "1-10", "9").result
        sight = result.series[0]
        assert sight.shot_display() == ["9.4", "10.7x", "8.9", "9.1", "10.0"]
        assert sight.shot_sum == sight.total.value == 46
        assert sight.inner_count == 1

    def test_card_summary_is_exposed(self, v2_pistol):
        result = v2.lane_view(v2_pistol, "1-10", "9").result
        names = [name for name, _ in result.series_summary]
        assert "1. Serie 150S" in names
        totals = dict(result.series_summary)
        # The range's own summary adds up to the card total.
        assert sum(t.value for t in totals.values() if t.value) == result.total.value

    def test_counting_series_exclude_the_sighters(self, v2_pistol):
        result = v2.lane_view(v2_pistol, "1-10", "9").result
        assert all(s.kind == MATCH for s in result.counting_series)

    def test_target_is_named(self, v2_pistol):
        result = v2.lane_view(v2_pistol, "1-10", "9").result
        assert result.series[0].target_id == "ISSF25P_SMALL_RF"
        assert result.series[0].target_name == "25m Rapid .22"

    def test_shots_carry_their_elapsed_time(self, v2_pistol):
        result = v2.lane_view(v2_pistol, "1-10", "9").result
        timed = [s for series in result.series for s in series.shots if s.elapsed]
        assert timed, "this discipline reports a time per shot"

    def test_an_empty_lane_is_still_described(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "3")
        assert view.result is not None
        assert view.result.shot_count == 0
        assert not view.shooter.occupied
        assert view.shooter_name == "Lane 3"


class TestRifleRange:
    def test_sighting_series_in_progress(self, v2_rifle):
        result = v2.lane_view(v2_rifle, "mulberry", "1").result
        assert result.series[0].kind == SIGHT
        assert result.series[0].shots

    def test_millimetre_coordinates_are_passed_through(self, v2_rifle):
        result = v2.lane_view(v2_rifle, "mulberry", "1").result
        shot = result.series[0].shots[0]
        # v2 reports millimetres from centre, not a fraction of the target.
        assert shot.x is not None and shot.y is not None
        assert abs(shot.x) > 1

    def test_clock_start_is_an_epoch_millisecond_string(self, v2_rifle):
        result = v2.lane_view(v2_rifle, "mulberry", "1").result
        assert result.clock.start_ms is not None
        # Sanity: within a plausible range for a wall-clock timestamp in ms.
        assert result.clock.start_ms > 1_000_000_000_000

    def test_skew_is_a_plain_millisecond_offset(self, v2_rifle):
        result = v2.lane_view(v2_rifle, "mulberry", "1").result
        assert result.clock.skew == v2_rifle["serverClockSkew"]
        assert abs(result.clock.skew) < 60_000


class TestCrossCheckAgainstEveryLane:
    """Whatever the range publishes must decode without surprises."""

    @pytest.mark.parametrize("fixture", ["v2_pistol", "v2_rifle"])
    def test_every_lane_decodes(self, fixture, request):
        tree = request.getfixturevalue(fixture)
        lanes = v2.lanes(tree)
        assert lanes
        for lane in lanes:
            view = v2.lane_view(tree, "r", lane)
            assert view.lane == lane
            assert view.result is not None
            for series in view.result.series:
                assert series.index >= 1
                assert isinstance(series.shot_display(), list)


class TestMissingData:
    def test_unknown_lane(self, v2_pistol):
        view = v2.lane_view(v2_pistol, "1-10", "999")
        assert view.result is None

    def test_lane_not_active_flag(self, v2_pistol):
        assert v2.lane_not_active(v2_pistol, "9") is False
