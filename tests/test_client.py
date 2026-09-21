"""Stream handling, discovery and range resolution."""

from __future__ import annotations

import pytest

from megalink_viewer.client import (
    ARENA_DB,
    LIVE_DB,
    Event,
    MegalinkClient,
    MegalinkError,
    Source,
    apply_event,
    match_range,
    parse_active_hosts,
)


def put(path, data):
    return Event("put", path, data)


def patch(path, data):
    return Event("patch", path, data)


class TestApplyEvent:
    """Firebase's stream semantics: `put` replaces, `patch` merges."""

    def test_root_put_replaces_the_whole_tree(self):
        assert apply_event({"old": 1}, put("/", {"new": 2})) == {"new": 2}

    def test_root_patch_merges_top_level_keys(self):
        assert apply_event({"a": 1, "b": 2}, patch("/", {"b": 3, "c": 4})) == {
            "a": 1,
            "b": 3,
            "c": 4,
        }

    def test_nested_put_replaces_only_that_node(self):
        tree = {"results": {"r_1": {"total": "1"}, "r_2": {"total": "2"}}}
        updated = apply_event(tree, put("/results/r_1", {"total": "9"}))
        assert updated["results"]["r_1"] == {"total": "9"}
        assert updated["results"]["r_2"] == {"total": "2"}

    def test_nested_patch_keeps_untouched_siblings(self):
        tree = {"fp": {"1": {"relay": 3, "activeSeries": 1}}}
        updated = apply_event(tree, patch("/fp/1", {"activeSeries": 2}))
        assert updated["fp"]["1"] == {"relay": 3, "activeSeries": 2}

    def test_put_creates_missing_intermediate_nodes(self):
        updated = apply_event({}, put("/data/fp/7/relay", 12))
        assert updated == {"data": {"fp": {"7": {"relay": 12}}}}

    def test_put_of_null_deletes(self):
        tree = {"cards": {"a": 1, "b": 2}}
        assert apply_event(tree, put("/cards/a", None)) == {"cards": {"b": 2}}

    def test_patch_null_value_deletes_just_that_key(self):
        tree = {"fp": {"1": {"a": 1, "b": 2}}}
        updated = apply_event(tree, patch("/fp/1", {"a": None}))
        assert updated["fp"]["1"] == {"b": 2}

    def test_original_tree_is_not_mutated(self):
        tree = {"fp": {"1": {"relay": 3}}}
        apply_event(tree, put("/fp/1/relay", 9))
        assert tree == {"fp": {"1": {"relay": 3}}}, "events must not mutate in place"

    def test_a_shot_arriving_mid_series(self):
        """The shape a live shot takes: one leaf appearing under a series."""
        tree = {"data": {"fp": {"9": {"series": {"1": {"shots": {"1": {"v": "9.4 "}}}}}}}}
        updated = apply_event(tree, put("/data/fp/9/series/1/shots/2", {"v": "10.7x"}))
        shots = updated["data"]["fp"]["9"]["series"]["1"]["shots"]
        assert shots == {"1": {"v": "9.4 "}, "2": {"v": "10.7x"}}

    def test_put_over_a_non_dict_tree(self):
        assert apply_event("nonsense", put("/a", 1)) == {"a": 1}


class TestApplyEventOverArrays:
    """Firebase serialises integer-keyed maps as arrays, then addresses them by
    index. Descending into one must preserve what is already there."""

    def test_descending_into_an_array_keeps_its_entries(self):
        tree = {"series": [None, {"sum": "47"}, {"sum": "46"}]}
        updated = apply_event(tree, put("/series/3", {"sum": "49"}))
        assert updated["series"] == {"1": {"sum": "47"}, "2": {"sum": "46"}, "3": {"sum": "49"}}

    def test_a_shot_appended_to_an_array_of_shots(self):
        tree = {"series": [None, {"shots": [None, {"v": "9.4 "}, {"v": "10.7x"}]}]}
        updated = apply_event(tree, put("/series/1/shots/3", {"v": "8.9 "}))
        assert updated["series"]["1"]["shots"] == {
            "1": {"v": "9.4 "},
            "2": {"v": "10.7x"},
            "3": {"v": "8.9 "},
        }

    def test_patching_a_node_held_in_an_array(self):
        tree = {"series": [None, {"sum": "47", "complete": False}]}
        updated = apply_event(tree, patch("/series/1", {"complete": True}))
        assert updated["series"]["1"] == {"sum": "47", "complete": True}

    def test_root_patch_over_an_array_tree(self):
        assert apply_event([None, {"a": 1}], patch("/", {"2": {"b": 2}})) == {
            "1": {"a": 1},
            "2": {"b": 2},
        }


class TestParseActiveHosts:
    def test_hosts_map_to_their_live_ranges(self, v2_hosts_active):
        hosts = parse_active_hosts(v2_hosts_active)
        assert hosts
        for host, ranges in hosts.items():
            assert isinstance(host, str)
            for entry in ranges:
                assert entry.host == host
                assert entry.key
                assert entry.name

    def test_a_club_running_several_ranges(self):
        data = {
            "ockero-skytteforening": {
                "10-m": {"hostName": "Öckerö", "rangeName": "10 m", "eventName": ""},
                "50-m": {"hostName": "Öckerö", "rangeName": "50 m", "eventName": "Cup"},
            }
        }
        ((host, ranges),) = parse_active_hosts(data).items()
        assert host == "ockero-skytteforening"
        assert [r.key for r in ranges] == ["10-m", "50-m"]
        assert ranges[1].event == "Cup"
        assert ranges[0].host_name == "Öckerö"

    def test_empty_node(self):
        assert parse_active_hosts(None) == {}


class Named:
    def __init__(self, key, name):
        self.key = key
        self.name = name


class TestMatchRange:
    def test_exact_key(self):
        found = match_range([Named("10-m", "10 m"), Named("50-m", "50 m")], "50-m")
        assert found.key == "50-m"

    def test_url_slug_matches_the_database_key(self):
        # live.megalink.no/#!/hanebjerg-skyttecenter/50m addresses key "50-m".
        found = match_range([Named("50-m", "50 M")], "50m")
        assert found is not None

    def test_display_name_matches(self):
        found = match_range([Named("1-10", "1-10"), Named("x", "Upper Range")], "upper range")
        assert found.key == "x"

    def test_substring_is_the_last_resort(self):
        found = match_range([Named("bunker-inverness-scotland", "Bunker")], "inverness")
        assert found is not None

    def test_no_query_takes_the_only_range(self):
        found = match_range([Named("a", "A"), Named("b", "B")], "")
        assert found.key == "a"

    def test_no_candidates(self):
        assert match_range([], "anything") is None

    def test_no_match(self):
        assert match_range([Named("a", "A")], "zzz") is None


class TestSource:
    def test_v2_source_decodes_its_tree(self, v2_pistol):
        source = Source(2, ARENA_DB, "data/stord-pk/1-10", "stord-pk", "1-10")
        assert source.lanes(v2_pistol)
        info = source.range_info(v2_pistol)
        assert info is not None and info.protocol == 2
        view = source.lane_view(v2_pistol, "9")
        assert view.shooter.name

    def test_v1_source_selects_one_range_from_the_club_tree(self, v1_dfs):
        source = Source(1, LIVE_DB, "data/nidaros-skl", "nidaros-skl", "15m")
        assert source.lanes(v1_dfs) == ["2", "3", "4"]
        info = source.range_info(v1_dfs)
        assert info is not None and info.key == "15m"

    def test_repr_names_the_protocol(self):
        source = Source(2, ARENA_DB, "p", "h", "r")
        assert "v2" in repr(source)


class FakeClient(MegalinkClient):
    """A client with the network replaced by canned nodes."""

    def __init__(self, active=None, v1_tree=None):
        super().__init__()
        self._active = active or {}
        self._v1 = v1_tree or {}

    def active(self):
        return parse_active_hosts(self._active)

    def snapshot_v1(self, host):
        return self._v1


class TestResolve:
    def test_prefers_the_current_protocol(self):
        client = FakeClient(
            active={"mulberry": {"mulberry": {"rangeName": "Mulberry", "hostName": "Mulberry"}}}
        )
        source = client.resolve("mulberry", "mulberry")
        assert source.protocol == 2
        assert source.path == "data/mulberry/mulberry"
        assert source.base == ARENA_DB

    def test_url_slug_resolves(self):
        client = FakeClient(
            active={"hanebjerg-skyttecenter": {"50m": {"rangeName": "50M", "hostName": "H"}}}
        )
        assert client.resolve("hanebjerg-skyttecenter", "50m").range_key == "50m"

    def test_falls_back_to_the_legacy_tree(self, v1_dfs):
        client = FakeClient(active={}, v1_tree=v1_dfs)
        source = client.resolve("nidaros-skl", "15m")
        assert source.protocol == 1
        assert source.base == LIVE_DB
        assert source.path == "data/nidaros-skl"
        assert source.range_key == "15m"

    def test_no_range_argument_takes_the_only_one(self):
        client = FakeClient(active={"mulberry": {"mulberry": {"rangeName": "Mulberry"}}})
        assert client.resolve("mulberry").range_key == "mulberry"

    def test_unknown_range_lists_what_is_available(self):
        client = FakeClient(active={"mulberry": {"mulberry": {"rangeName": "Mulberry"}}})
        with pytest.raises(MegalinkError) as excinfo:
            client.resolve("mulberry", "300m")
        assert "mulberry" in str(excinfo.value)

    def test_host_with_nothing_live(self):
        client = FakeClient(active={})
        with pytest.raises(MegalinkError, match="no live ranges"):
            client.resolve("quiet-club")
