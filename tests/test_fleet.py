"""The dashboard that lists and bulk-edits every display on a network."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from megalink_viewer.beacon import encode
from megalink_viewer.config import Config, load, save
from megalink_viewer.controller import Controller
from megalink_viewer.fleet import FleetServer, plan_lanes
from megalink_viewer.webconfig import ConfigServer


class TestPlanLanes:
    def test_a_numeric_start_counts_up_per_display(self):
        """A row of firing points is numbered consecutively."""
        assert plan_lanes(["a", "b", "c"], "5") == {"a": "5", "b": "6", "c": "7"}

    def test_a_step_skips(self):
        assert plan_lanes(["a", "b"], "1", 2) == {"a": "1", "b": "3"}

    def test_a_non_numeric_start_is_given_to_all(self):
        # Some ranges label firing points with letters.
        assert plan_lanes(["a", "b"], "C") == {"a": "C", "b": "C"}

    def test_no_displays(self):
        assert plan_lanes([], "1") == {}


@pytest.fixture
def two_displays(tmp_path, v2_pistol):
    """Two real displays, each with its own config file and web server."""
    from conftest import FakeClient

    made = []
    for lane in ("9", "7"):
        path = tmp_path / f"display-{lane}.json"
        config = Config(host="stord-pk", range="1-10", lane=lane)
        config.web.port = 0
        config.web.bind = "127.0.0.1"
        config.beacon.enabled = False
        config.beacon.name = f"firing-point-{lane}"
        save(config, path)
        client = FakeClient(
            trees={("stord-pk", "1-10"): v2_pistol},
            active={"stord-pk": {"1-10": {"hostName": "Stord PK", "rangeName": "1-10"}}},
        )
        controller = Controller(path=path, client=client)
        controller.reload()
        controller.wait_for_data(timeout=5)
        server = ConfigServer(controller).start()
        controller.config.web.port = server.port
        made.append((controller, server, path))
    yield made
    for controller, server, _path in made:
        server.stop()
        controller.stop()


@pytest.fixture
def fleet(two_displays):
    """A dashboard that has already heard from both displays."""
    from megalink_viewer.beacon import payload_for

    server = FleetServer(port=0, bind="127.0.0.1", beacon_port=0).start()
    # Feed the registry directly rather than relying on broadcast reaching us:
    # the transport has its own tests, and this keeps these deterministic.
    for controller, config_server, _path in two_displays:
        payload = dict(payload_for(controller))
        payload["web"] = config_server.port
        server.listener.accept(encode(payload), "127.0.0.1")
    yield server, two_displays
    server.stop()


def get(server, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=10) as r:
        return r.status, json.load(r)


def post(server, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


class TestListing:
    def test_healthz(self, fleet):
        server, _displays = fleet
        status, body = get(server, "/healthz")
        assert status == 200 and body["displays"] == 2

    def test_both_displays_are_listed(self, fleet):
        server, _displays = fleet
        _status, body = get(server, "/api/fleet/displays")
        names = sorted(d["name"] for d in body["displays"])
        assert names == ["firing-point-7", "firing-point-9"]

    def test_a_listing_carries_what_it_is_showing(self, fleet):
        server, _displays = fleet
        _status, body = get(server, "/api/fleet/displays")
        first = body["displays"][0]
        assert first["url"].startswith("http://127.0.0.1:")
        assert first["shooter"]
        assert first["connected"] is True

    def test_the_page_is_served(self, fleet):
        server, _displays = fleet
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/", timeout=10) as r:
            html = r.read().decode()
        assert 'id="rows"' in html


class TestBulkEditing:
    def test_the_same_change_goes_to_every_selected_display(self, fleet):
        server, displays = fleet
        _status, body = get(server, "/api/fleet/displays")
        ids = [d["id"] for d in body["displays"]]
        status, out = post(
            server, "/api/fleet/apply", {"targets": ids, "patch": {"range": "1-10", "lane": "2"}}
        )
        assert status == 200
        assert all(r["ok"] for r in out["results"])
        for _controller, _srv, path in displays:
            assert load(path).lane == "2"

    def test_consecutive_numbering_across_displays(self, fleet):
        """The point of the dashboard: set up a row of positions in one go."""
        server, displays = fleet
        _status, body = get(server, "/api/fleet/displays")
        ordered = sorted(body["displays"], key=lambda d: d["name"])
        ids = [d["id"] for d in ordered]
        status, out = post(
            server,
            "/api/fleet/apply",
            {"targets": ids, "patch": {"host": "stord-pk"}, "lanes": {"start": "3", "step": 1}},
        )
        assert status == 200
        assert [r["lane"] for r in out["results"]] == ["3", "4"]

        by_name = {}
        for controller, _srv, path in displays:
            by_name[controller.config.name] = load(path).lane
        assert by_name["firing-point-7"] == "3"
        assert by_name["firing-point-9"] == "4"

    def test_a_change_reaches_the_running_display_not_just_the_file(self, fleet):
        server, displays = fleet
        _status, body = get(server, "/api/fleet/displays")
        post(
            server,
            "/api/fleet/apply",
            {"targets": [body["displays"][0]["id"]], "patch": {"lane": "5"}},
        )
        changed = [c for c, _s, _p in displays if c.config.lane == "5"]
        assert changed, "the display itself should have adopted the change"

    def test_an_unknown_display_is_refused(self, fleet):
        server, _displays = fleet
        status, body = post(server, "/api/fleet/apply", {"targets": ["10.0.0.9:8080"], "patch": {}})
        assert status == 400
        assert "not a display" in body["error"]

    def test_no_selection_is_refused(self, fleet):
        server, _displays = fleet
        status, body = post(server, "/api/fleet/apply", {"targets": [], "patch": {"lane": "1"}})
        assert status == 400 and "no displays selected" in body["error"]

    def test_a_bad_patch_is_refused(self, fleet):
        server, _displays = fleet
        _status, body = get(server, "/api/fleet/displays")
        status, out = post(
            server, "/api/fleet/apply", {"targets": [body["displays"][0]["id"]], "patch": "lane=1"}
        )
        assert status == 400 and "patch must be" in out["error"]

    def test_an_unreachable_display_is_reported_per_display(self, fleet):
        """One display off does not stop the others being set up."""
        server, displays = fleet
        _status, body = get(server, "/api/fleet/displays")
        ids = [d["id"] for d in body["displays"]]
        # Take one of them offline.
        displays[0][1].stop()
        status, out = post(server, "/api/fleet/apply", {"targets": ids, "patch": {"lane": "8"}})
        assert status == 207
        assert sorted(r["ok"] for r in out["results"]) == [False, True]
        assert any("error" in r for r in out["results"] if not r["ok"])

    def test_an_invalid_value_is_reported_from_the_display(self, fleet):
        server, _displays = fleet
        _status, body = get(server, "/api/fleet/displays")
        status, out = post(
            server,
            "/api/fleet/apply",
            {"targets": [body["displays"][0]["id"]], "patch": {"display": {"mode": "smoke"}}},
        )
        assert status == 207
        assert "display.mode" in out["results"][0]["error"]


class TestIdentify:
    def test_identify_reaches_the_displays(self, fleet):
        server, displays = fleet
        _status, body = get(server, "/api/fleet/displays")
        ids = [d["id"] for d in body["displays"]]
        status, out = post(server, "/api/fleet/identify", {"targets": ids})
        assert status == 200 and all(r["ok"] for r in out["results"])
        assert all(controller.identifying() for controller, _s, _p in displays)

    def test_identify_needs_a_selection(self, fleet):
        server, _displays = fleet
        status, _body = post(server, "/api/fleet/identify", {"targets": []})
        assert status == 400


class TestTokenPassThrough:
    def test_the_dashboard_presents_its_token(self, tmp_path, v2_pistol):
        """Displays behind a shared secret still take bulk changes."""
        from conftest import FakeClient
        from megalink_viewer.beacon import payload_for

        path = tmp_path / "secure.json"
        config = Config(host="stord-pk", range="1-10", lane="9")
        config.web.port = 0
        config.web.bind = "127.0.0.1"
        config.web.token = "hunter2"
        config.beacon.enabled = False
        save(config, path)
        client = FakeClient(
            trees={("stord-pk", "1-10"): v2_pistol},
            active={"stord-pk": {"1-10": {"hostName": "Stord PK", "rangeName": "1-10"}}},
        )
        controller = Controller(path=path, client=client)
        controller.reload()
        controller.wait_for_data(timeout=5)
        display_server = ConfigServer(controller).start()
        controller.config.web.port = display_server.port

        payload = dict(payload_for(controller))
        payload["web"] = display_server.port

        blind = FleetServer(port=0, bind="127.0.0.1", beacon_port=0).start()
        armed = FleetServer(port=0, bind="127.0.0.1", beacon_port=0, token="hunter2").start()
        try:
            for server in (blind, armed):
                server.listener.accept(encode(payload), "127.0.0.1")
            identifier = f"127.0.0.1:{display_server.port}"

            status, out = post(
                blind, "/api/fleet/apply", {"targets": [identifier], "patch": {"lane": "2"}}
            )
            assert status == 207, "without the token the display must refuse"
            assert "403" in out["results"][0]["error"]

            status, out = post(
                armed, "/api/fleet/apply", {"targets": [identifier], "patch": {"lane": "2"}}
            )
            assert status == 200 and out["results"][0]["ok"]
            assert load(path).lane == "2"
        finally:
            blind.stop()
            armed.stop()
            display_server.stop()
            controller.stop()


class TestEachDisplayItsOwn:
    """The page works out each display's firing points and sends them as one."""

    def test_each_display_gets_its_own_change(self, fleet):
        server, displays = fleet
        _status, body = get(server, "/api/fleet/displays")
        ids = {d["name"]: d["id"] for d in body["displays"]}
        status, _out = post(
            server,
            "/api/fleet/apply",
            {
                "targets": list(ids.values()),
                "patch": {"host": "stord-pk"},
                "each": {
                    ids["firing-point-7"]: {"lane": "12"},
                    ids["firing-point-9"]: {"lane": "13", "display": {"lane2": "14"}},
                },
            },
        )
        assert status == 200
        lanes = {c.config.name: (c.config.lane, c.config.display.lane2) for c, _s, _p in displays}
        assert lanes == {"firing-point-7": ("12", ""), "firing-point-9": ("13", "14")}

    def test_a_display_s_own_settings_are_merged_not_replaced(self, fleet):
        # Its second screen, set as part of the display section, must not
        # take the rest of that section with it.
        server, displays = fleet
        controller = displays[0][0]
        controller.config.display.idle_text = "CLOSED"
        _status, body = get(server, "/api/fleet/displays")
        target = next(d["id"] for d in body["displays"] if d["name"] == controller.config.name)
        post(
            server,
            "/api/fleet/apply",
            {"targets": [target], "each": {target: {"display": {"lane2": "3"}}}},
        )
        assert controller.config.display.lane2 == "3"
        assert controller.config.display.idle_text == "CLOSED"

    def test_a_bad_each_is_refused(self, fleet):
        server, _displays = fleet
        _status, body = get(server, "/api/fleet/displays")
        target = body["displays"][0]["id"]
        status, out = post(
            server, "/api/fleet/apply", {"targets": [target], "each": {target: "lane=1"}}
        )
        assert status == 400 and "each must" in out["error"]


class TestTheAnnouncement:
    def test_it_carries_the_second_screen_and_the_names(self, two_displays):
        from megalink_viewer.beacon import payload_for

        controller = two_displays[0][0]
        controller.config.display.lane2 = "10"
        controller.screens = ["HDMI-1", "HDMI-2"]
        payload = payload_for(controller)
        assert payload["lane2"] == "10" and payload["screens"] == 2
        assert payload["mode"] == "gui"
        assert payload["host_name"] == "Stord PK"


class TestLookupsOnALaptop:
    """The standalone dashboard offers the clubs to choose from as well."""

    def test_the_clubs_and_ranges_are_there(self, v2_pistol):
        from conftest import FakeClient

        client = FakeClient(
            trees={("stord-pk", "1-10"): v2_pistol},
            active={"stord-pk": {"1-10": {"hostName": "Stord PK", "rangeName": "1-10"}}},
        )
        server = FleetServer(port=0, bind="127.0.0.1", beacon_port=0, client=client).start()
        try:
            _status, hosts = get(server, "/api/hosts")
            assert [h["name"] for h in hosts["hosts"]] == ["Stord PK"]
            _status, ranges = get(server, "/api/ranges?host=stord-pk")
            assert [r["key"] for r in ranges["ranges"]] == ["1-10"]
        finally:
            server.stop()

    def test_without_a_client_there_are_none(self):
        server = FleetServer(port=0, bind="127.0.0.1", beacon_port=0).start()
        try:
            with pytest.raises(urllib.error.HTTPError):
                get(server, "/api/hosts")
        finally:
            server.stop()
