"""The legacy feed adapter, checked against snapshots of real clubs."""

from __future__ import annotations

import pytest

from megalink_viewer import v1
from megalink_viewer.models import MATCH, SIGHT


class TestRanges:
    def test_range_metadata(self, v1_dfs):
        (range_,) = v1.ranges(v1_dfs)
        assert range_.key == "15m"
        assert range_.host_name == "Nidaros SKL"
        assert range_.gun_type == "RIFLE"
        assert range_.relay == 18
        assert range_.protocol == 1
        assert range_.practice

    def test_ranges_arriving_as_an_array_still_parse(self, v1_list_shaped):
        # Firebase turns an integer-keyed map into a JSON array with a hole at 0.
        found = v1.ranges(v1_list_shaped)
        assert found
        assert found[0].key.isdigit()

    def test_missing_ranges_node(self):
        assert v1.ranges({}) == []


class TestLanes:
    def test_lanes_are_numeric_order_not_lexical(self, v1_issf):
        lanes = v1.lanes(v1_issf, "lower-range-comp-1")
        assert lanes == sorted(lanes, key=int)

    def test_lanes_of_an_unknown_range(self, v1_dfs):
        assert v1.lanes(v1_dfs, "nope") == []


class TestDfsScoring:
    """Integer-scored DFS: the ring counts, so shot values truncate."""

    def test_series_are_decoded_in_order(self, v1_dfs):
        view = v1.lane_view(v1_dfs, "15m", "2")
        names = [s.name for s in view.result.series]
        assert names[0] == "Prøve"
        assert names[1] == "1. Serie Liggende"

    def test_sighting_series_is_marked_and_excluded(self, v1_dfs):
        result = v1.lane_view(v1_dfs, "15m", "2").result
        assert result.series[0].kind == SIGHT
        assert result.series[1].kind == MATCH
        assert result.series[0] not in result.counting_series

    def test_shots_truncate_to_the_ring(self, v1_dfs):
        result = v1.lane_view(v1_dfs, "15m", "2").result
        first = next(s for s in result.series if s.name == "1. Serie Liggende")
        assert not first.decimal
        assert first.shot_display() == ["10.8x", "9.6", "9.6", "9.9", "10.1"]
        # 10 + 9 + 9 + 9 + 10 -- not 11 + 10 + 10 + 10 + 10.
        assert first.shot_sum == 47

    def test_derived_sum_agrees_with_the_range(self, v1_dfs):
        """On this range every series' shots add up to the published total."""
        result = v1.lane_view(v1_dfs, "15m", "2").result
        for series in result.series:
            assert series.shot_sum == series.total.value, series.name

    def test_counting_series_add_up_to_the_card_total(self, v1_dfs):
        result = v1.lane_view(v1_dfs, "15m", "2").result
        assert sum(s.total.value for s in result.counting_series) == result.total.value == 238

    def test_inner_count_matches_the_card_total(self, v1_dfs):
        result = v1.lane_view(v1_dfs, "15m", "2").result
        assert sum(s.inner_count for s in result.counting_series) == result.total.inner == 5

    def test_active_series(self, v1_dfs):
        result = v1.lane_view(v1_dfs, "15m", "2").result
        assert result.active_series_index == 5
        assert result.active_series.name == "4. Serie Liggende"

    def test_shot_count(self, v1_dfs):
        assert v1.lane_view(v1_dfs, "15m", "2").result.shot_count == 30

    def test_a_lane_that_has_not_shot_yet(self, v1_dfs):
        result = v1.lane_view(v1_dfs, "15m", "4").result
        assert result.shot_count == 0
        assert result.total.value == 0

    def test_club_without_names_falls_back_to_the_lane(self, v1_dfs):
        view = v1.lane_view(v1_dfs, "15m", "2")
        assert view.shooter_name == "Lane 2"
        assert not view.shooter.occupied


class TestIssfSplitCard:
    """Decimal ISSF, where one long series is cut into groups of ten."""

    def test_decimal_series_keeps_tenths(self, v1_issf):
        result = v1.lane_view(v1_issf, "lower-range-comp-1", "50").result
        match = next(s for s in result.series if not s.sight)
        assert match.decimal
        assert "." in match.shot_display()[0]

    def test_published_series_total_is_the_current_group_of_ten(self, v1_issf):
        """The whole point of keeping ``shot_sum`` separate from ``total``."""
        result = v1.lane_view(v1_issf, "lower-range-comp-1", "50").result
        match = next(s for s in result.series if not s.sight and len(s.shots) > 10)
        assert match.total.value != match.shot_sum
        # The shots add up to the card total; `sum` only covers the last group.
        assert match.shot_sum == pytest.approx(result.total.value)

    def test_splits_are_passed_through_untouched(self, v1_issf):
        result = v1.lane_view(v1_issf, "lower-range-comp-1", "50").result
        match = next(s for s in result.series if not s.sight and len(s.shots) > 10)
        # Card-scoped, so they must not be summed into a series total.
        assert len(match.splits) > 1


class TestClockWiring:
    def test_idle_range_clock_reads_nothing(self, v1_dfs):
        result = v1.lane_view(v1_dfs, "15m", "2").result
        # The range reports `running: true` with `timestamp: 0`.
        assert result.clock.remaining_ms(1_000_000_000) is None

    def test_skew_is_taken_from_the_range(self, v1_dfs):
        result = v1.lane_view(v1_dfs, "15m", "2").result
        assert result.clock.skew == v1_dfs["clockSkew"]["15m"]


class TestMissingData:
    def test_unknown_lane(self, v1_dfs):
        view = v1.lane_view(v1_dfs, "15m", "99")
        assert view.result is None
        assert view.shooter is None
        assert view.shooter_name == "Lane 99"
        assert not view.present

    def test_empty_tree(self):
        view = v1.lane_view({}, "x", "1")
        assert view.range is None
        assert view.result is None
