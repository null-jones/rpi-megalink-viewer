"""Decoding of the wire format's display strings."""

from __future__ import annotations

import pytest

from megalink_viewer.parse import (
    entries,
    loose_key,
    parse_shot_value,
    parse_total,
    slug,
    strip_style,
)


class TestStripStyle:
    def test_removes_trailing_style_blob(self):
        assert strip_style('0.00Style:{"font":"italic"}') == "0.00"

    def test_leaves_plain_text_alone(self):
        assert strip_style("618.4") == "618.4"

    def test_none_becomes_empty(self):
        assert strip_style(None) == ""


class TestParseShotValue:
    @pytest.mark.parametrize(
        ("raw", "score", "inner"),
        [
            ("10.6x", 10.6, True),  # ISSF inner ten, x suffix
            ("*10.5", 10.5, True),  # DFS inner ten, asterisk prefix
            ("10.0 ", 10.0, False),  # padded to keep columns aligned
            ("9.7", 9.7, False),
            (" 8.6 ", 8.6, False),
        ],
    )
    def test_numeric_forms(self, raw, score, inner):
        value = parse_shot_value(raw)
        assert value.score == pytest.approx(score)
        assert value.inner is inner
        assert value.scored

    def test_display_keeps_the_precision_megalink_sent(self):
        # "10.0" must not collapse to "10" on a decimal target.
        assert parse_shot_value("10.0 ").display() == "10.0"
        assert parse_shot_value("10.6x").display() == "10.6x"

    def test_void_marker_does_not_score(self):
        value = parse_shot_value("")
        assert value.symbol is not None
        assert value.symbol.name == "VOID"
        assert value.score is None
        assert not value.scored
        assert value.display() == "-"

    def test_frame_marker(self):
        value = parse_shot_value(" ")
        assert value.symbol.name == "FRAME"
        assert value.score is None

    def test_issf_ten_marker_counts_as_inner(self):
        value = parse_shot_value("")
        assert value.symbol.name == "ISSF_TEN"
        assert value.inner

    def test_style_blob_is_stripped_before_decoding(self):
        assert parse_shot_value('10.4xStyle:{"font":"italic"}').score == pytest.approx(10.4)

    def test_empty_value(self):
        assert parse_shot_value("").score is None


class TestRing:
    """A decimal series scores the measurement; an integer series scores the ring."""

    def test_decimal_series_keeps_tenths(self):
        assert parse_shot_value("10.8").ring(decimal=True) == pytest.approx(10.8)

    def test_integer_series_truncates_rather_than_rounds(self):
        # 10.8 is a ten, not an eleven -- this is what makes DFS sums add up.
        assert parse_shot_value("10.8").ring(decimal=False) == 10
        assert parse_shot_value("9.9").ring(decimal=False) == 9

    def test_non_scoring_marker_has_no_ring(self):
        assert parse_shot_value("").ring(decimal=False) is None


class TestParseTotal:
    def test_dfs_sum_with_inner_count(self):
        total = parse_total("238 (*5)")
        assert total.value == 238
        assert total.inner == 5
        assert total.display() == "238 (5x)"

    def test_issf_sum_with_x_count(self):
        total = parse_total("552-8x")
        assert total.value == 552
        assert total.inner == 8

    def test_decimal_sum(self):
        assert parse_total("618.4").value == pytest.approx(618.4)

    def test_zero_forms(self):
        assert parse_total("0 (*0)").value == 0
        assert parse_total("0-0x").value == 0
        assert parse_total("0").value == 0

    def test_elapsed_time_sum(self):
        total = parse_total("10.42s")
        assert total.seconds == pytest.approx(10.42)
        assert total.value is None
        assert total.display() == "10.42s"

    def test_styled_sum(self):
        assert parse_total('0.00Style:{"font":"italic"}').value == 0

    def test_empty_sum(self):
        assert parse_total("").value is None
        assert parse_total("").display() == "-"


class TestEntries:
    def test_object_form(self):
        assert entries({"a": 1, "b": 2}) == [("a", 1), ("b", 2)]

    def test_array_form_is_keyed_by_index(self):
        # Firebase turns integer-keyed maps into arrays; series start at 1, so
        # index 0 arrives as a hole and must be dropped.
        assert entries([None, {"x": 1}]) == [("1", {"x": 1})]

    def test_nulls_are_dropped_from_objects(self):
        assert entries({"a": None, "b": 1}) == [("b", 1)]

    def test_other_types_yield_nothing(self):
        assert entries(None) == []
        assert entries("text") == []


class TestSlugs:
    def test_database_key_slug(self):
        assert slug("50 M") == "50-m"
        assert slug("Upper Range Comp 1") == "upper-range-comp-1"

    def test_loose_key_bridges_url_and_database_spellings(self):
        # live.megalink.no/#!/hanebjerg-skyttecenter/50m addresses key "50-m".
        assert loose_key("50m") == loose_key("50-m") == loose_key("50 M")
