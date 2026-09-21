"""State tracking against a stream."""

from __future__ import annotations

from megalink_viewer.client import ARENA_DB, LIVE_DB, Event, Source
from megalink_viewer.watch import RangeState


def v2_source():
    return Source(2, ARENA_DB, "data/stord-pk/1-10", "stord-pk", "1-10")


class TestRangeState:
    def test_starts_disconnected(self):
        state = RangeState(v2_source())
        assert not state.connected
        assert state.age() is None
        assert state.lanes() == []

    def test_initial_snapshot_populates_everything(self, v2_pistol):
        state = RangeState(v2_source())
        state.apply(Event("put", "/", v2_pistol))
        assert state.connected
        assert state.lanes()
        assert state.range_info().host_name == "Stord PK"
        assert state.lane_view("9").shooter.name == "Etai Moredehi Bogen"

    def test_a_patch_updates_the_view(self, v2_pistol):
        state = RangeState(v2_source())
        state.apply(Event("put", "/", v2_pistol))
        before = state.lane_view("9").result.total.value
        state.apply(Event("patch", "/data/fp/9/cardTotals", {"total": "95-2x"}))
        after = state.lane_view("9").result
        assert after.total.value == 95
        assert after.total.inner == 2
        assert after.total.value != before

    def test_a_new_shot_shows_up(self, v2_pistol):
        state = RangeState(v2_source())
        state.apply(Event("put", "/", v2_pistol))
        series = state.lane_view("9").result.series[0]
        index = len(series.shots) + 1
        state.apply(
            Event(
                "put",
                f"/data/fp/9/series/1/shots/{index}",
                {"v": "10.9x", "x": 0.5, "y": -0.5, "t": 1.0},
            )
        )
        updated = state.lane_view("9").result.series[0]
        assert len(updated.shots) == len(series.shots) + 1
        assert updated.shots[-1].value.display() == "10.9x"
        assert updated.shots[-1].value.inner

    def test_a_streamed_shot_survives_the_array_to_map_conversion(self, v2_pistol):
        """The fixture's `series` and `shots` nodes are JSON arrays."""
        state = RangeState(v2_source())
        state.apply(Event("put", "/", v2_pistol))
        before = state.lane_view("9").result
        state.apply(Event("put", "/data/fp/9/series/1/shots/9", {"v": "10.1 "}))
        after = state.lane_view("9").result
        # Every other series and its shots must still be there.
        assert len(after.series) == len(before.series)
        assert after.series[0].name == before.series[0].name
        assert after.total.value == before.total.value

    def test_replaying_the_events_the_feed_actually_sends(self, v2_pistol):
        """The two event shapes observed on a live range.

        A range in progress sends a `patch` of the firing point's `cardTotals`
        and a `put` of the whole series node, in that order, for every group of
        shots -- and the series node it addresses by index is a JSON array in
        the snapshot, so this exercises the array-to-map conversion on the exact
        path the feed uses.
        """
        state = RangeState(v2_source())
        state.apply(Event("put", "/", v2_pistol))
        series_before = len(state.lane_view("9").result.series)

        state.apply(
            Event(
                "patch",
                "/data/fp/9",
                {
                    "cardTotals": {
                        "seriesName": ["1. Serie 150S", "2. Serie 150S", " 20 Skud"],
                        "seriesTotals": ["41", "43", "94"],
                        "shotNr": ["1-5", "6-10", "1-20"],
                        "splitList": ["94", "0"],
                        "total": "178-4x",
                    }
                },
            )
        )
        state.apply(
            Event(
                "put",
                "/data/fp/9/series/9",
                {
                    "aimRingR": 0,
                    "complete": False,
                    "decimal": False,
                    "gaugeOnly": False,
                    "gaugeR": 2.8,
                    "name": " 20 Skud",
                    "seriesSize": 20,
                    "seriesTotals": {"splitTotals": ["94", "0"], "total": "94-3x"},
                    "shotList": 10,
                    "shotNr": "1-20",
                    "shots": [
                        None,
                        {"t": 257.25, "v": "9.9 ", "x": 8.324, "y": 10.801},
                        {"t": 274.91, "v": "9.5 ", "x": 18.329, "y": -7.342},
                        {"t": 296.75, "v": "10.5x", "x": -1.641, "y": -6.086},
                    ],
                    "targetId": "ISSF25P_SMALL_PREC",
                    "targetName": "25m Precision",
                    "targetNr": 21,
                    "targetVar": 255,
                    "type": "MATCH",
                    "valueColor": False,
                },
            )
        )

        result = state.lane_view("9").result
        # The new series is there, and nothing that was already there was lost.
        assert len(result.series) == series_before + 1
        assert result.series[0].name == "Prøve 150S"
        added = next(s for s in result.series if s.index == 9)
        assert added.name == "20 Skud"
        assert added.total.value == 94
        assert added.total.inner == 3
        assert added.shot_display() == ["9.9", "9.5", "10.5x"]
        assert added.inner_count == 1
        # The patched card total replaced only the card totals.
        assert result.total.value == 178
        assert result.total.inner == 4
        assert dict(result.series_summary)[" 20 Skud".strip()].value == 94

    def test_a_reconnect_replaces_the_tree(self, v2_pistol, v2_rifle):
        state = RangeState(v2_source())
        state.apply(Event("put", "/", v2_pistol))
        # Firebase replays the full state as a root `put` on reconnect.
        state.apply(Event("put", "/", v2_rifle))
        assert state.range_info().host_name == "Mulberry"

    def test_age_tracks_the_last_event(self, v2_pistol):
        state = RangeState(v2_source())
        state.apply(Event("put", "/", v2_pistol))
        assert state.age(now=state.updated_at + 7) == 7

    def test_works_for_the_legacy_protocol_too(self, v1_dfs):
        state = RangeState(Source(1, LIVE_DB, "data/nidaros-skl", "nidaros-skl", "15m"))
        state.apply(Event("put", "/", v1_dfs))
        assert state.lanes() == ["2", "3", "4"]
        assert state.lane_view("2").result.total.value == 238
