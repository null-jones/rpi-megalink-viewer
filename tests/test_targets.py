"""Target geometry, and the check that pins it down.

The strongest evidence that the geometry table has been read correctly is that
the scores the ranges publish can be recovered from the positions they publish.
:meth:`TargetFace.score_at` exists for that, and the tests at the bottom run it
over every shot in the fixtures.
"""

from __future__ import annotations

import math

import pytest

from megalink_viewer import v1, v2
from megalink_viewer.targets import (
    LEGACY_IDS,
    SCORING_ALIASES,
    TargetFace,
    face,
    known_ids,
    scoring_face,
)


class TestTable:
    def test_table_loads(self):
        assert len(known_ids()) > 60

    def test_unknown_id(self):
        assert face("NOT_A_TARGET") is None
        assert face("") is None

    def test_legacy_ids_all_resolve(self):
        for legacy, modern in LEGACY_IDS.items():
            resolved = face(legacy)
            if resolved is None:
                # The hunting silhouettes are deliberately not carried.
                assert modern not in known_ids()
                continue
            assert resolved.id == modern

    def test_scoring_alias(self):
        # The 3D-Score 100m face is printed with its own rings but scored on
        # the standard ones.
        assert "DFS100_3D" in SCORING_ALIASES
        assert face("DFS100_3D").id == "DFS100_3D"
        assert scoring_face("DFS100_3D").id == "DFS100"

    def test_scoring_face_is_normally_the_drawn_face(self):
        assert scoring_face("ISSF10R").id == face("ISSF10R").id


class TestAirRifleFace:
    """The 10m air rifle target, whose real dimensions are well known."""

    @pytest.fixture
    def face_(self) -> TargetFace:
        return face("ISSF10R")

    def test_outer_ring(self, face_):
        # Ring 1 is 45.5mm across.
        assert face_.outer_radius == pytest.approx(22.75)

    def test_rings_are_two_and_a_half_millimetres_apart(self, face_):
        # 45.5mm down to 0.5mm over nine steps: 5mm of diameter a ring.
        assert face_.radius(1) == pytest.approx(22.75)
        assert face_.radius(10) == pytest.approx(0.25)
        assert face_.radius(4) == pytest.approx(15.25)

    def test_black_aiming_area_is_thirty_point_five_millimetres(self, face_):
        aim = face_.aiming_mark
        assert aim is not None
        radius, color = aim
        assert radius == pytest.approx(15.25)
        assert color == "black"

    def test_white_centre_dot(self, face_):
        centre = face_.centre
        assert centre is not None
        assert centre[0] == pytest.approx(0.25)
        assert centre[1] == "white"

    def test_pellet_diameter(self, face_):
        assert face_.gauge == pytest.approx(4.5)
        assert face_.gauge_radius == pytest.approx(2.25)

    def test_no_separate_inner_ring(self, face_):
        # The table gives a negative inner diameter: the ten is the inner ten.
        assert face_.inner_radius is None

    def test_ring_lines_switch_colour_over_the_black(self, face_):
        colors = {ring.number: ring.color for ring in face_.rings()}
        assert colors[1] == "black"
        assert colors[4] == "black"
        assert colors[5] == "white"
        assert colors[10] == "white"

    def test_numbers_on_both_axes(self, face_):
        assert face_.number_layout == "hv"
        assert len(face_.number_offsets()) == 4


class TestRapidFireFace:
    def test_numbers_only_on_the_vertical_axis(self):
        # This face prints its numbers above and below the rings only.
        rapid = face("ISSF25P_SMALL_RF")
        assert rapid.number_layout == "v"
        assert rapid.number_offsets() == [(0, -1), (0, 1)]

    def test_the_whole_face_is_black(self):
        rapid = face("ISSF25P_SMALL_RF")
        backing = rapid.backing
        assert backing is not None and backing[1] == "black"

    def test_a_shaped_aiming_mark_is_not_a_disc(self):
        # Its aiming mark is a silhouette, which this package does not draw.
        assert face("ISSF25P_SMALL_RF").aiming_mark is None

    def test_it_still_has_rings_to_draw(self):
        assert [r.number for r in face("ISSF25P_SMALL_RF").rings()]


class TestUnevenlySpacedFace:
    """The 3D-Score face lists each ring rather than interpolating."""

    def test_explicit_ring_diameters_are_used(self):
        face_ = face("DFS100_3D")
        assert face_.radius(1) == pytest.approx(125.0)
        assert face_.radius(2) == pytest.approx(120.0)
        assert face_.radius(10) == pytest.approx(15.0)
        # The spacing is deliberately not uniform.
        outer_step = face_.radius(1) - face_.radius(2)
        inner_step = face_.radius(9) - face_.radius(10)
        assert outer_step != pytest.approx(inner_step)

    def test_inner_ring_is_drawn(self):
        assert face("DFS100_3D").inner_radius == pytest.approx(7.5)


class TestCoordinateScales:
    def test_v2_reports_millimetres(self):
        assert face("ISSF10R").scale_for(2) == 1.0

    def test_v1_reports_a_fraction_of_the_outer_radius(self):
        face_ = face("ISSF10R")
        assert face_.scale_for(1) == pytest.approx(face_.outer_radius)

    def test_a_shot_at_the_edge_of_a_v1_target(self):
        face_ = face("DFS15")
        # x = 1.0 in v1 units is the outer ring.
        assert 1.0 * face_.scale_for(1) == pytest.approx(face_.outer_radius)


class TestScoreAt:
    def test_dead_centre_is_the_best_ring_or_better(self):
        face_ = face("ISSF10R")
        assert face_.score_at(0.0) >= 10.0

    def test_the_gauge_counts_in_the_shooters_favour(self):
        """A shot scores the best ring its edge touches, not its centre."""
        face_ = face("ISSF10R")
        # Centre exactly on the ring-9 line, so the pellet edge is well inside.
        on_the_line = face_.radius(9)
        assert face_.score_at(on_the_line) > 9.0

    def test_score_falls_off_with_distance(self):
        face_ = face("DFS200")
        scores = [face_.score_at(r) for r in (0, 100, 200, 300, 400)]
        assert scores == sorted(scores, reverse=True)

    def test_a_face_with_too_little_geometry(self):
        # Constructed rather than loaded: nothing to interpolate.
        assert TargetFace({"id": "X", "value": {}, "draw": {}}).score_at(1.0) is None


def _series_with_shots(view):
    return [s for s in (view.result.series if view.result else []) if s.shots]


class TestScoresReproduceFromPositions:
    """The check that validates the whole geometry pipeline.

    For every shot in the fixtures, recompute the score from the published
    position and compare with the published value. Agreement to a small
    fraction of a ring means the ring diameters, the coordinate scale and the
    gauge rule are all right.
    """

    TOLERANCE = 0.35

    def _check(self, series_list, protocol):
        checked = 0
        for series in series_list:
            face_ = series.face
            if face_ is None:
                continue
            scorer = scoring_face(series.target_id) or face_
            for shot, x_mm, y_mm in series.positions_mm():
                published = shot.value.score
                if published is None:
                    continue
                # Above the innermost ring the reading is an extrapolation, so
                # only the in-rings region is a fair comparison.
                if published >= face_.highest_ring:
                    continue
                got = scorer.score_at(math.hypot(x_mm, y_mm))
                assert got is not None
                assert abs(got - published) <= self.TOLERANCE, (
                    f"{series.target_id} shot {shot.value.display()}: "
                    f"published {published}, computed {got:.2f}"
                )
                checked += 1
        return checked

    def test_v2_pistol(self, v2_pistol):
        total = 0
        for lane in v2.lanes(v2_pistol):
            total += self._check(_series_with_shots(v2.lane_view(v2_pistol, "1-10", lane)), 2)
        assert total > 20

    def test_v2_rifle(self, v2_rifle):
        total = 0
        for lane in v2.lanes(v2_rifle):
            total += self._check(_series_with_shots(v2.lane_view(v2_rifle, "mulberry", lane)), 2)
        assert total > 20

    def test_v1_dfs(self, v1_dfs):
        total = 0
        for lane in v1.lanes(v1_dfs, "15m"):
            total += self._check(_series_with_shots(v1.lane_view(v1_dfs, "15m", lane)), 1)
        assert total > 20

    def test_v1_issf(self, v1_issf):
        total = 0
        for lane in v1.lanes(v1_issf, "lower-range-comp-1"):
            total += self._check(
                _series_with_shots(v1.lane_view(v1_issf, "lower-range-comp-1", lane)), 1
            )
        assert total > 20


class TestSeriesGeometryHelpers:
    def test_v2_positions_are_already_millimetres(self, v2_pistol):
        series = _series_with_shots(v2.lane_view(v2_pistol, "1-10", "9"))[0]
        assert series.mm_per_unit == 1.0
        shot, x, y = series.positions_mm()[0]
        assert (x, y) == (shot.x, shot.y)

    def test_v1_positions_are_scaled_up(self, v1_dfs):
        series = _series_with_shots(v1.lane_view(v1_dfs, "15m", "2"))[0]
        assert series.mm_per_unit == pytest.approx(41.5)
        shot, x, _y = series.positions_mm()[0]
        assert x == pytest.approx(shot.x * 41.5)

    def test_hole_radius_comes_from_the_range_on_v2(self, v2_pistol):
        series = _series_with_shots(v2.lane_view(v2_pistol, "1-10", "9"))[0]
        assert series.gauge_radius == pytest.approx(2.8)
        assert series.hole_radius == pytest.approx(2.8)

    def test_hole_radius_falls_back_to_the_face(self, v1_dfs):
        series = _series_with_shots(v1.lane_view(v1_dfs, "15m", "2"))[0]
        assert series.hole_radius == pytest.approx(2.8, abs=0.05)

    def test_unplottable_shots_are_left_out(self, v2_rifle):
        for lane in v2.lanes(v2_rifle):
            for series in _series_with_shots(v2.lane_view(v2_rifle, "mulberry", lane)):
                assert len(series.positions_mm()) <= len(series.shots)
