"""Protocol-neutral domain behaviour."""

from __future__ import annotations

import pytest

from megalink_viewer import v2
from megalink_viewer.models import (
    MATCH,
    SHOOTOFF,
    SIGHT,
    Clock,
    LaneResult,
    LaneView,
    RangeInfo,
    Series,
    Shooter,
    Shot,
    Stage,
)
from megalink_viewer.parse import Total, parse_shot_value, parse_total


def shot(value):
    return Shot(parse_shot_value(value))


def series(index=1, kind=MATCH, decimal=False, values=(), total=None):
    return Series(
        index=index,
        name=f"Series {index}",
        kind=kind,
        decimal=decimal,
        complete=False,
        shots=[shot(v) for v in values],
        total=total if total is not None else Total("", None),
    )


class TestSeries:
    def test_sight_flag_follows_the_kind(self):
        assert series(kind=SIGHT).sight
        assert not series(kind=MATCH).sight
        assert not series(kind=SHOOTOFF).sight

    def test_inner_count(self):
        assert series(values=["10.6x", "9.8", "*10.4"]).inner_count == 2

    def test_shot_sum_truncates_on_an_integer_series(self):
        assert series(decimal=False, values=["10.8", "9.9", "9.1"]).shot_sum == 28

    def test_shot_sum_keeps_tenths_on_a_decimal_series(self):
        got = series(decimal=True, values=["10.8", "9.9", "9.1"]).shot_sum
        assert got == pytest.approx(29.8)

    def test_shot_sum_ignores_non_scoring_markers(self):
        # A void shot contributes nothing.
        assert series(decimal=False, values=["10.4", ""]).shot_sum == 10

    def test_shot_sum_of_an_empty_series(self):
        assert series(values=[]).shot_sum is None

    def test_shot_display_preserves_precision(self):
        assert series(values=["10.0 ", "9.6"]).shot_display() == ["10.0", "9.6"]


class TestShooter:
    def test_occupied_needs_a_name_or_a_club(self):
        assert Shooter(name="A").occupied
        assert Shooter(club="B").occupied
        assert not Shooter(class_name="5").occupied
        assert not Shooter().occupied


class TestLaneResult:
    def test_counting_series_exclude_sighters(self):
        result = LaneResult(Total("", 0), [series(1, SIGHT), series(2, MATCH)])
        assert [s.index for s in result.counting_series] == [2]

    def test_active_series_is_looked_up_by_index(self):
        result = LaneResult(Total("", 0), [series(1), series(2)], active_series_index=1)
        assert result.active_series.index == 1

    def test_active_series_falls_back_to_the_last(self):
        result = LaneResult(Total("", 0), [series(1), series(2)], active_series_index=None)
        assert result.active_series.index == 2

    def test_active_series_index_that_does_not_exist(self):
        result = LaneResult(Total("", 0), [series(1)], active_series_index=99)
        assert result.active_series.index == 1

    def test_no_series_at_all(self):
        assert LaneResult(Total("", 0), []).active_series is None

    def test_shot_counts(self):
        result = LaneResult(
            Total("", 0),
            [series(1, SIGHT, values=["9.0"] * 3), series(2, MATCH, values=["9.0"] * 5)],
        )
        assert result.shot_count == 8
        assert result.counting_shot_count == 5


class TestClock:
    """The stage walk, reimplemented from the web client.

    ``start_ms`` is when the range clock started, on the range's own clock;
    ``START`` stands in for it so elapsed time stays explicit.
    """

    START = 1_000_000

    def clock(self, *stages, running=True, start_ms=START, skew=0):
        return Clock(running=running, start_ms=start_ms, stages=list(stages), skew=skew)

    @staticmethod
    def stage(kind, counting_up, start_count, offset=0):
        return Stage(type=kind, counting_up=counting_up, offset=offset, start_count=start_count)

    def at(self, elapsed_ms):
        """The ``now`` that puts the clock *elapsed_ms* into its run."""
        return self.START + elapsed_ms

    def test_not_running(self):
        assert self.clock(running=False).remaining_ms(self.at(5_000)) is None

    def test_never_started(self):
        # Ranges sit idle with a clock switched on but no start time.
        assert self.clock(self.stage("SHOOT", True, 0), start_ms=None).remaining_ms(0) is None

    def test_counting_down_stage_reports_time_left(self):
        remaining = self.clock(self.stage("SHOOT", False, 60_000)).remaining_ms(self.at(10_000))
        assert remaining is not None
        assert 50_000 <= remaining <= 51_000

    def test_counting_up_stage_reports_elapsed(self):
        assert self.clock(self.stage("SHOOT", True, 0)).remaining_ms(self.at(7_000)) == 7_000

    def test_open_ended_command_stage_is_skipped(self):
        clock = self.clock(self.stage("COMMAND", True, 0), self.stage("SHOOT", False, 60_000))
        remaining = clock.remaining_ms(self.at(10_000))
        assert remaining is not None and remaining > 49_000

    def test_past_the_last_stage_reads_zero(self):
        assert self.clock(self.stage("SHOOT", False, 1_000)).remaining_ms(self.at(60_000)) == 0

    def test_preparation_then_shooting(self):
        clock = self.clock(
            self.stage("COMMAND", False, 15_000),
            self.stage("SHOOT", False, 60_000, offset=15_000),
        )
        during_command = clock.remaining_ms(self.at(5_000))
        assert during_command is not None and 10_000 <= during_command <= 11_000
        during_shoot = clock.remaining_ms(self.at(30_000))
        assert during_shoot is not None and 45_000 <= during_shoot <= 46_000

    def test_skew_shifts_the_range_clock_onto_server_time(self):
        stage = self.stage("SHOOT", True, 0)
        assert self.clock(stage, skew=0).remaining_ms(self.at(10_000)) == 10_000
        assert self.clock(stage, skew=-2_000).remaining_ms(self.at(10_000)) == 12_000

    def test_a_clock_with_no_stages(self):
        assert self.clock().remaining_ms(self.at(10_000)) == 0


class TestLaneView:
    def test_shooter_name_prefers_the_name(self):
        view = LaneView("3", shooter=Shooter(name="A Shooter", club="A Club"))
        assert view.shooter_name == "A Shooter"

    def test_shooter_name_falls_back_to_the_club(self):
        assert LaneView("3", shooter=Shooter(club="A Club")).shooter_name == "A Club"

    def test_shooter_name_falls_back_to_the_lane(self):
        assert LaneView("3", shooter=Shooter()).shooter_name == "Lane 3"
        assert LaneView("3").shooter_name == "Lane 3"

    def test_present_requires_a_result_or_a_shooter(self):
        assert not LaneView("1").present
        assert not LaneView("1", shooter=Shooter()).present
        assert LaneView("1", shooter=Shooter(name="X")).present
        assert LaneView("1", result=LaneResult(Total("", 0), [])).present


class TestRangeInfo:
    def test_repr_names_the_protocol(self):
        assert "v2" in repr(RangeInfo("k", "N", protocol=2))


class TestSeriesGroups:
    """A card published as one long series is shown in the groups it is read in.

    US Naval Academy publishes an ISSF 60-shot card as a single series of sixty
    with ``maxSeriesSize: 10``. Its own display shows six tens; a display that
    piled all sixty onto one target would be unreadable long before the card was
    finished.
    """

    def card(self, v2_split_card, lane="28"):
        return v2.lane_view(v2_split_card, "airgun", lane).result

    def match(self, v2_split_card, lane="28"):
        return next(s for s in self.card(v2_split_card, lane).series if not s.sight)

    def test_the_range_states_the_group_size(self, v2_split_card):
        series = self.match(v2_split_card)
        assert series.size == 60
        assert series.split_size == 10

    def test_a_long_series_is_divided_into_groups(self, v2_split_card):
        assert len(self.match(v2_split_card).groups()) == 6

    def test_each_group_takes_its_own_totals(self, v2_split_card):
        totals = [g.total.display() for g in self.match(v2_split_card).groups()]
        # The range's own splitTotals: two full tens, then one shot of the third.
        assert totals == ["95", "95", "7", "0", "0", "0"]

    def test_each_group_takes_its_own_shots(self, v2_split_card):
        groups = self.match(v2_split_card).groups()
        assert [len(g.shots) for g in groups] == [10, 10, 1, 0, 0, 0]

    def test_the_groups_do_not_overlap_or_lose_a_shot(self, v2_split_card):
        series = self.match(v2_split_card)
        rejoined = [shot for group in series.groups() for shot in group.shots]
        assert rejoined == series.shots

    def test_a_group_knows_where_it_starts_in_the_card(self, v2_split_card):
        assert [g.offset for g in self.match(v2_split_card).groups()] == [0, 10, 20, 30, 40, 50]

    def test_a_group_keeps_the_name_of_what_is_being_shot(self, v2_split_card):
        series = self.match(v2_split_card)
        assert {g.name for g in series.groups()} == {series.name}

    def test_a_group_points_back_at_its_series(self, v2_split_card):
        series = self.match(v2_split_card)
        assert all(g.parent is series for g in series.groups())
        assert series.parent is None

    def test_a_series_already_at_group_size_is_not_divided(self, v2_pistol):
        result = v2.lane_view(v2_pistol, "1-10", "9").result
        series = next(s for s in result.series if s.size and s.size <= s.split_size)
        assert series.groups() == [series]

    def test_group_totals_are_ignored_when_they_do_not_add_up(self, v2_pistol):
        # A sighting series is often declared as a round 100 shots whatever the
        # group size is, so its ten split totals describe nothing. Dividing on
        # them anyway would invent groups the range never showed.
        series = Series(
            index=1,
            name="Sight",
            kind=SIGHT,
            decimal=True,
            complete=False,
            shots=[Shot(value=10.0) for _ in range(7)],
            total=parse_total("70"),
            splits=[parse_total("0") for _ in range(10)],
            size=100,
            split_size=5,
        )
        groups = series.groups()
        assert len(groups) == 2  # from the shots, not the ten bogus totals
        assert [len(g.shots) for g in groups] == [5, 2]

    def test_an_undivided_series_needs_no_group_size(self):
        series = Series(
            index=1,
            name="x",
            kind=MATCH,
            decimal=True,
            complete=False,
            shots=[Shot(value=10.0)],
            total=parse_total("10"),
        )
        assert series.groups() == [series]


class TestActiveGroup:
    def card(self, v2_split_card, lane="28"):
        return v2.lane_view(v2_split_card, "airgun", lane).result

    def test_the_card_is_shown_as_its_groups(self, v2_split_card):
        groups = self.card(v2_split_card).display_series
        # Two groups of sighters shot, then the three tens of the card so far.
        assert [(g.name, len(g.shots)) for g in groups] == [
            ("Sight", 10),
            ("Sight", 8),
            ("60 Shots", 10),
            ("60 Shots", 10),
            ("60 Shots", 1),
        ]

    def test_groups_past_the_last_shot_are_not_shown(self, v2_split_card):
        result = self.card(v2_split_card)
        # The card is six groups and the strip says so, but there is nothing to
        # look at on the three that have not been shot yet.
        assert len(self.match_series(result).groups()) == 6
        assert not any(g.shots == [] for g in result.display_series)

    def match_series(self, result):
        return next(s for s in result.series if not s.sight)

    def test_the_group_being_shot_is_the_last_with_a_shot_in_it(self, v2_split_card):
        active = self.card(v2_split_card).active_display_group
        # Twenty-one shots fired, so the third ten -- not the sixth, which is
        # empty, and not the second, which is finished.
        assert active.offset == 20
        assert len(active.shots) == 1

    def test_an_untouched_card_starts_at_its_first_group(self, v2_split_card):
        active = self.card(v2_split_card, "25").active_display_group
        assert active is None or active.offset == 0

    def test_a_card_with_no_series_has_no_group(self):
        assert LaneResult(parse_total("0"), []).active_display_group is None
        assert LaneResult(parse_total("0"), []).display_series == []
