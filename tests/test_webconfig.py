"""The configuration page a display serves for itself."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import ClassVar

import pytest

from megalink_viewer.config import Config, load, save
from megalink_viewer.controller import Controller
from megalink_viewer.webconfig import ConfigServer


@pytest.fixture
def display(config_path, fake_client):
    """A running display with its configuration page on a free port."""
    config = Config(host="stord-pk", range="1-10", lane="9")
    config.web.port = 0
    config.web.bind = "127.0.0.1"
    config.beacon.enabled = False
    save(config, config_path)
    controller = Controller(path=config_path, client=fake_client)
    controller.reload()
    controller.wait_for_data(timeout=5)
    server = ConfigServer(controller).start()
    yield controller, server, config_path
    server.stop()
    controller.stop()


def request(server, path, method="GET", payload=None, token=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["X-Megalink-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as response:
        body = response.read()
        return response.status, (json.loads(body) if body else None)


def expect_error(server, path, method="GET", payload=None, token=None):
    try:
        request(server, path, method, payload, token)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)
    raise AssertionError("expected an error response")


class TestTheSecondScreen:
    """A Pi 4 or 5 with two screens picks each one's firing point the same way."""

    def page(self, server):
        url = f"http://127.0.0.1:{server.port}/"
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.read().decode()

    def test_it_is_chosen_beside_the_first_from_the_same_list(self, display):
        _controller, server, _path = display
        body = self.page(server)
        showing = body.index("<h2>What to display</h2>")
        assert showing < body.index('<select id="lane2">') < body.index("<h2>This screen</h2>")
        # Not typed into a box at the bottom of the page any more.
        assert '<input id="lane2"' not in body
        assert "Same as screen 1" in body

    def test_the_page_is_told_which_screens_are_plugged_in(self, display):
        controller, server, _path = display
        controller.screens = ["HDMI-1", "HDMI-2"]
        _status, body = request(server, "/api/status")
        assert body["screens"] == ["HDMI-1", "HDMI-2"]

    def test_it_is_saved_with_the_first(self, display):
        controller, server, _path = display
        request(
            server,
            "/api/config",
            "PUT",
            {"host": "stord-pk", "range": "1-10", "lane": "9", "display": {"lane2": "10"}},
        )
        assert controller.config.lane == "9"
        assert controller.config.display.lane2 == "10"


class TestStatus:
    def test_healthz(self, display):
        _controller, server, _path = display
        status, body = request(server, "/healthz")
        assert status == 200 and body["ok"] is True

    def test_status_reports_what_is_showing(self, display):
        _controller, server, _path = display
        _status, body = request(server, "/api/status")
        assert body["shooter"] == "Etai Moredehi Bogen"
        assert body["lane"] == "9"
        assert body["connected"] is True

    def test_the_page_is_served(self, display):
        _controller, server, _path = display
        url = f"http://127.0.0.1:{server.port}/"
        with urllib.request.urlopen(url, timeout=10) as response:
            html = response.read().decode()
        assert response.headers["Content-Type"].startswith("text/html")
        assert 'id="lane"' in html


class TestReadingConfig:
    def test_the_config_is_served(self, display):
        _controller, server, _path = display
        _status, body = request(server, "/api/config")
        assert body["host"] == "stord-pk"

    def test_the_token_is_not_served(self, display):
        controller, server, _path = display
        controller.write(controller.config.merged({"web": {"token": "hunter2"}}))
        _status, body = request(server, "/api/config")
        assert body["web"]["token"] is True


class TestWritingConfig:
    def test_a_partial_update_is_applied_and_saved(self, display):
        controller, server, path = display
        status, body = request(server, "/api/config", "PUT", {"lane": "4"})
        assert status == 200
        assert body["config"]["lane"] == "4"
        assert body["status"]["lane"] == "4"
        assert controller.config.lane == "4"
        assert load(path).lane == "4"

    def test_other_settings_survive_a_partial_update(self, display):
        _controller, server, path = display
        request(server, "/api/config", "PUT", {"lane": "4"})
        assert load(path).host == "stord-pk"

    def test_post_works_as_well_as_put(self, display):
        _controller, server, _path = display
        status, _body = request(server, "/api/config", "POST", {"lane": "5"})
        assert status == 200

    def test_an_invalid_value_is_refused(self, display):
        controller, server, _path = display
        code, body = expect_error(server, "/api/config", "PUT", {"display": {"mode": "smoke"}})
        assert code == 400
        assert "display.mode" in body["error"]
        # And nothing changed.
        assert controller.config.display.mode == "gui"

    def test_malformed_json_is_refused(self, display):
        _controller, server, _path = display
        url = f"http://127.0.0.1:{server.port}/api/config"
        req = urllib.request.Request(
            url, data=b"{not json", headers={"Content-Type": "application/json"}, method="PUT"
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(req, timeout=10)
        assert excinfo.value.code == 400

    def test_an_oversized_body_is_refused(self, display):
        """And the client gets the reason, rather than a broken pipe."""
        _controller, server, _path = display
        code, body = expect_error(server, "/api/config", "PUT", {"host": "x" * 200_000})
        assert code == 413
        assert "too large" in body["error"]


class TestAuthorisation:
    def test_writes_are_open_when_no_token_is_set(self, display):
        _controller, server, _path = display
        status, _body = request(server, "/api/config", "PUT", {"lane": "2"})
        assert status == 200

    def test_a_token_is_required_once_set(self, display):
        controller, server, _path = display
        controller.write(controller.config.merged({"web": {"token": "hunter2"}}))
        code, body = expect_error(server, "/api/config", "PUT", {"lane": "3"})
        assert code == 403
        assert "token" in body["error"].lower()

    def test_the_right_token_is_accepted(self, display):
        controller, server, _path = display
        controller.write(controller.config.merged({"web": {"token": "hunter2"}}))
        status, _body = request(server, "/api/config", "PUT", {"lane": "3"}, token="hunter2")
        assert status == 200

    def test_a_wrong_token_is_refused(self, display):
        controller, server, _path = display
        controller.write(controller.config.merged({"web": {"token": "hunter2"}}))
        code, _body = expect_error(server, "/api/config", "PUT", {"lane": "3"}, token="nope")
        assert code == 403

    def test_reading_stays_open(self, display):
        controller, server, _path = display
        controller.write(controller.config.merged({"web": {"token": "hunter2"}}))
        status, _body = request(server, "/api/status")
        assert status == 200


class TestDiscoveryEndpoints:
    def test_hosts(self, display):
        _controller, server, _path = display
        _status, body = request(server, "/api/hosts")
        slugs = [h["host"] for h in body["hosts"]]
        assert "stord-pk" in slugs and "mulberry" in slugs

    def test_ranges(self, display):
        _controller, server, _path = display
        _status, body = request(server, "/api/ranges?host=stord-pk")
        assert [r["key"] for r in body["ranges"]] == ["1-10"]

    def test_ranges_defaults_to_the_configured_host(self, display):
        _controller, server, _path = display
        _status, body = request(server, "/api/ranges")
        assert body["host"] == "stord-pk"

    def test_lanes(self, display):
        _controller, server, _path = display
        _status, body = request(server, "/api/lanes?host=stord-pk&range=1-10")
        assert "9" in body["lanes"]

    def test_a_backend_failure_is_a_gateway_error(self, display, fake_client):
        _controller, server, _path = display
        fake_client.fail = "the database is unreachable"
        code, body = expect_error(server, "/api/ranges?host=nobody")
        assert code in (502, 400)
        assert "error" in body

    def test_answers_are_cached(self, display, fake_client):
        """A dropdown should not put a Pi Zero on the network per keystroke."""
        _controller, server, _path = display
        request(server, "/api/hosts")
        fake_client.fail = "network gone"
        status, body = request(server, "/api/hosts")
        assert status == 200 and body["hosts"]


class TestIdentify:
    def test_identify_marks_the_display(self, display):
        controller, server, _path = display
        assert not controller.identifying()
        status, body = request(server, "/api/identify", "POST", {})
        assert status == 200 and body["identifying"] is True
        assert controller.identifying()

    def test_identify_needs_the_token_when_set(self, display):
        controller, server, _path = display
        controller.write(controller.config.merged({"web": {"token": "hunter2"}}))
        code, _body = expect_error(server, "/api/identify", "POST", {})
        assert code == 403


class TestUnknownRoutes:
    def test_unknown_path(self, display):
        _controller, server, _path = display
        code, body = expect_error(server, "/api/nonsense")
        assert code == 404 and body["error"] == "not found"

    def test_a_method_the_route_does_not_take(self, display):
        _controller, server, _path = display
        status, body = request(server, "/api/config", "PUT", {})
        assert status == 200  # an empty patch is a no-op, not an error
        assert body["config"]["host"] == "stord-pk"


class TestLogo:
    """A club badge to show when nobody is on the firing point."""

    def _put(self, server, body):
        url = f"http://127.0.0.1:{server.port}/api/logo"
        request = urllib.request.Request(url, data=body, method="PUT")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def _get(self, server):
        url = f"http://127.0.0.1:{server.port}/api/logo"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.status, response.read(), response.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), None

    def test_none_to_begin_with(self, display):
        _controller, server, _path = display
        assert self._get(server)[0] == 404

    def test_a_png_round_trips_byte_for_byte(self, display):
        from conftest import make_png

        _controller, server, _path = display
        image = make_png()
        status, body = self._put(server, image)
        assert status == 200 and body["stored"] == "logo.png"
        status, fetched, content_type = self._get(server)
        assert status == 200
        assert fetched == image
        assert content_type == "image/png"

    def test_the_display_finds_it(self, display):
        from conftest import make_png

        controller, server, _path = display
        self._put(server, make_png())
        found = controller.logo_path()
        assert found is not None and found.name == "logo.png"

    def test_a_gif_is_accepted(self, display):
        _controller, server, _path = display
        # Tk reads PNG and GIF; those are the two.
        assert self._put(server, b"GIF89a" + b"\x00" * 32)[0] == 200

    def test_other_formats_are_refused(self, display):
        _controller, server, _path = display
        status, body = self._put(server, b"\xff\xd8\xff\xe0 a jpeg")
        assert status == 400
        assert "PNG and GIF" in body["error"]

    def test_the_format_is_read_from_the_bytes_not_the_request(self, display):
        """A content type is a claim; the file has to be one Tk can read."""
        _controller, server, _path = display
        url = f"http://127.0.0.1:{server.port}/api/logo"
        request = urllib.request.Request(
            url,
            data=b"not an image at all",
            method="PUT",
            headers={"Content-Type": "image/png"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)
        assert excinfo.value.code == 400

    def test_only_one_logo_is_kept(self, display):
        from conftest import make_png

        _controller, server, path = display
        self._put(server, make_png())
        self._put(server, b"GIF89a" + b"\x00" * 32)
        images = sorted(p.name for p in path.parent.iterdir() if p.name.startswith("logo"))
        assert images == ["logo.gif"]

    def test_removing_it(self, display):
        from conftest import make_png

        controller, server, _path = display
        self._put(server, make_png())
        url = f"http://127.0.0.1:{server.port}/api/logo"
        request = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(request, timeout=10) as response:
            assert json.load(response)["removed"] == ["logo.png"]
        assert self._get(server)[0] == 404
        assert controller.logo_path() is None

    def test_uploading_needs_the_token_when_set(self, display):
        from conftest import make_png

        controller, server, _path = display
        controller.write(controller.config.merged({"web": {"token": "hunter2"}}))
        assert self._put(server, make_png())[0] == 403

    def test_an_oversized_image_is_refused(self, display):
        _controller, server, _path = display
        from megalink_viewer.webconfig import MAX_LOGO_BYTES

        status, body = self._put(server, b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_LOGO_BYTES)
        assert status == 413
        assert "too large" in body["error"]


class TestFleetOnTheDisplay:
    """Every display can manage the range, so each serves the dashboard itself.

    A display already hears every other display's beacon; adding a listener is
    what turns that into a dashboard, and it means there is no separate service
    to run and no one machine whose loss takes the dashboard with it.
    """

    @pytest.fixture
    def meshed(self, config_path, fake_client):
        from megalink_viewer.beacon import Listener, encode

        config = Config(host="stord-pk", range="1-10", lane="9")
        config.web.port = 0
        config.web.bind = "127.0.0.1"
        config.beacon.enabled = False
        save(config, config_path)
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()

        listener = Listener(0)
        # Two neighbours, fed the announcements they would have broadcast. Not
        # started: binding a real broadcast socket is not what this is about.
        for address, name, lane in (("10.0.0.5", "fp-05", "5"), ("10.0.0.6", "fp-06", "6")):
            listener.accept(encode({"name": name, "lane": lane, "web": 8080}), address)
        server = ConfigServer(controller, listener=listener).start()
        yield controller, server
        server.stop()
        controller.stop()

    def test_the_dashboard_is_served_by_the_display(self, meshed):
        _controller, server = meshed
        url = f"http://127.0.0.1:{server.port}/fleet"
        with urllib.request.urlopen(url, timeout=10) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "text/html"
            assert b"Megalink displays" in response.read()

    def test_the_display_lists_its_neighbours(self, meshed):
        _controller, server = meshed
        _status, body = request(server, "/api/fleet/displays")
        assert {d["name"] for d in body["displays"]} == {"fp-05", "fp-06"}

    def test_the_configuration_page_says_the_dashboard_is_there(self, meshed):
        _controller, server = meshed
        _status, body = request(server, "/api/peers")
        assert body == {"fleet": True, "count": 2}

    def test_a_display_on_its_own_offers_no_dashboard(self, display):
        _controller, server, _path = display
        _status, body = request(server, "/api/peers")
        assert body["fleet"] is False
        # ...and the page is not served, rather than served empty.
        assert expect_error(server, "/fleet")[0] == 404

    def test_changing_a_neighbour_needs_the_same_token_as_changing_this_one(self, meshed):
        controller, server = meshed
        controller.write(controller.config.merged({"web": {"token": "shared"}}))
        payload = {"targets": ["10.0.0.5:8080"], "patch": {"lane": "3"}}
        assert expect_error(server, "/api/fleet/apply", "POST", payload)[0] == 403
        # A display is no less worth protecting because the request came by way
        # of its neighbour.
        assert expect_error(server, "/api/config", "PUT", {"lane": "3"})[0] == 403

    def test_the_list_of_neighbours_is_readable_without_the_token(self, meshed):
        controller, server = meshed
        controller.write(controller.config.merged({"web": {"token": "shared"}}))
        status, _body = request(server, "/api/fleet/displays")
        assert status == 200


class TestOneAnswerPerRequest:
    """A route that writes its own body must not be answered a second time.

    The logo sends an image and then returns ``None`` like any route that found
    nothing, so the dispatcher used to append its 404 to the same connection.
    These servers speak HTTP/1.0 and close after every reply, so the second
    response dies with the socket and nothing goes wrong today -- which is
    exactly why it is worth a test. Under HTTP/1.1 the client would read the
    first response by its Content-Length and take the second as the answer to
    its next request, and the failure would surface somewhere unrelated.

    So these force keep-alive on, which is the condition the invariant is for.
    """

    @pytest.fixture(autouse=True)
    def keep_alive(self, monkeypatch):
        from megalink_viewer.httpbase import JSONHandler

        monkeypatch.setattr(JSONHandler, "protocol_version", "HTTP/1.1")

    def exchange(self, server, requests):
        """Send several requests down one connection and read each reply."""
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            answers = []
            for path in requests:
                connection.request("GET", path)
                response = connection.getresponse()
                answers.append((response.status, response.headers.get_content_type()))
                response.read()
            return answers
        finally:
            connection.close()

    def test_an_image_does_not_leave_a_second_reply_behind(self, display, tmp_path):
        from conftest import make_png
        from megalink_viewer.config import logo_path

        _controller, server, path = display
        # Written where an upload puts it, beside the configuration.
        logo_path(path, ".png").write_bytes(make_png(8, 8))
        assert self.exchange(server, ["/api/logo", "/healthz", "/api/status"]) == [
            (200, "image/png"),
            (200, "application/json"),
            (200, "application/json"),
        ]

    def test_a_page_does_not_leave_a_second_reply_behind(self, meshed_display):
        server = meshed_display
        assert self.exchange(server, ["/fleet", "/healthz"]) == [
            (200, "text/html"),
            (200, "application/json"),
        ]


@pytest.fixture
def meshed_display(config_path, fake_client):
    from megalink_viewer.beacon import Listener, encode

    config = Config(host="stord-pk", range="1-10", lane="9")
    config.web.port = 0
    config.web.bind = "127.0.0.1"
    config.beacon.enabled = False
    save(config, config_path)
    controller = Controller(path=config_path, client=fake_client)
    controller.reload()
    listener = Listener(0)
    listener.accept(encode({"name": "fp-05", "lane": "5", "web": 8080}), "10.0.0.5")
    server = ConfigServer(controller, listener=listener).start()
    yield server
    server.stop()
    controller.stop()


class TestForgedRequests:
    """A web page must not be able to reconfigure a display.

    Browsers let any page send a "simple" request to any address without asking
    the server first -- a POST whose body is plain text, a form, or nothing --
    and the server acts on it even though the page never sees the answer. With
    no token set, which is the default, that let any page opened by anyone on
    the range network rewrite a display, and through the fleet endpoint, every
    display on the range.
    """

    def raw(self, server, path, body, content_type, method="POST"):
        headers = {"Content-Type": content_type} if content_type else {}
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}{path}",
            data=body.encode() if body is not None else b"",
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    @pytest.mark.parametrize(
        "content_type",
        [
            "text/plain;charset=UTF-8",  # <form enctype="text/plain">, fetch no-cors
            "application/x-www-form-urlencoded",  # an ordinary form
            "multipart/form-data; boundary=x",
            "",  # no body type at all
        ],
    )
    def test_a_forgeable_post_changes_nothing(self, display, content_type):
        controller, server, _path = display
        body = json.dumps({"lane": "1", "display": {"url": "https://attacker.example/"}})
        assert self.raw(server, "/api/config", body, content_type) == 415
        assert controller.config.lane == "9"
        assert controller.config.display.url == ""

    def test_the_identify_button_cannot_be_pressed_from_another_site(self, display):
        _controller, server, _path = display
        assert self.raw(server, "/api/identify", "", "") == 415

    def test_a_json_post_is_still_accepted(self, display):
        controller, server, _path = display
        status = self.raw(server, "/api/config", json.dumps({"lane": "4"}), "application/json")
        assert status == 200
        assert controller.config.lane == "4"

    def test_a_charset_on_the_json_type_is_fine(self, display):
        controller, server, _path = display
        status = self.raw(
            server, "/api/config", json.dumps({"lane": "5"}), "application/json; charset=utf-8"
        )
        assert status == 200
        assert controller.config.lane == "5"

    def test_reads_are_unaffected(self, display):
        _controller, server, _path = display
        assert request(server, "/api/status")[0] == 200

    def test_the_fleet_cannot_be_driven_from_another_site(self, meshed_display):
        # The one that mattered most: a single forged request used to reach
        # every display on the range.
        server = meshed_display
        body = json.dumps({"targets": ["10.0.0.5:8080"], "patch": {"lane": "1"}})
        assert self.raw(server, "/api/fleet/apply", body, "text/plain") == 415


class TestTokenComparison:
    def test_a_wrong_token_is_refused(self, display):
        controller, server, _path = display
        controller.write(controller.config.merged({"web": {"token": "right"}}))
        assert expect_error(server, "/api/config", "PUT", {"lane": "2"}, token="wrong")[0] == 403

    def test_a_token_that_is_only_a_prefix_is_refused(self, display):
        controller, server, _path = display
        controller.write(controller.config.merged({"web": {"token": "right-and-long"}}))
        assert expect_error(server, "/api/config", "PUT", {"lane": "2"}, token="right")[0] == 403

    def test_the_right_token_is_accepted(self, display):
        controller, server, _path = display
        controller.write(controller.config.merged({"web": {"token": "right"}}))
        assert request(server, "/api/config", "PUT", {"lane": "2"}, token="right")[0] == 200


class TestSetupPage:
    """What a browser-mode display shows while it has nothing else to."""

    @pytest.fixture
    def unconfigured(self, config_path, fake_client):
        from megalink_viewer.address import Reach

        config = Config()
        config.web.port = 0
        config.web.bind = "127.0.0.1"
        config.beacon.enabled = False
        save(config, config_path)
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        reach = {"value": Reach("fp-09", [("wlan0", "192.168.1.23")], 8080, "192.168.1.23")}
        server = ConfigServer(controller, find_reach=lambda port: reach["value"]).start()
        yield server, reach
        server.stop()
        controller.stop()

    def page(self, server):
        url = f"http://127.0.0.1:{server.port}/setup"
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.headers.get_content_type(), response.read().decode()

    def test_it_is_a_page(self, unconfigured):
        server, _reach = unconfigured
        status, kind, _body = self.page(server)
        assert (status, kind) == (200, "text/html")

    def test_it_gives_the_address(self, unconfigured):
        server, _reach = unconfigured
        assert "http://192.168.1.23:8080/" in self.page(server)[2]

    def test_it_carries_the_code_inline(self, unconfigured):
        # Inline, so a browser with no route anywhere can still draw it.
        body = self.page(server := unconfigured[0])[2]
        assert "<svg" in body and "crispEdges" in body
        assert server

    def test_it_reloads_itself(self, unconfigured):
        # So it follows the address, and gives way once the display is set up.
        assert 'http-equiv="refresh"' in self.page(unconfigured[0])[2]

    def test_with_no_network_it_says_so(self, unconfigured):
        from megalink_viewer.address import Reach

        server, reach = unconfigured
        reach["value"] = Reach("fp-09", [], 8080)
        body = self.page(server)[2]
        assert "Waiting for a network" in body
        assert "<svg" not in body

    def test_with_no_network_it_counts_down_to_the_hotspot(self, config_path, fake_client):
        import time

        from megalink_viewer.address import Reach

        config = Config()
        config.web.port = 0
        config.web.bind = "127.0.0.1"
        config.beacon.enabled = False
        save(config, config_path)
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        status = {"mode": "waiting", "hotspot_in": 20.0, "updated": time.time()}
        server = ConfigServer(
            controller,
            find_reach=lambda port: Reach("fp-09", [], port),
            read_network=lambda: status,
        ).start()
        try:
            body = self.page(server)[2]
        finally:
            server.stop()
            controller.stop()
        # The number is marked for the page's script to count down.
        assert 'data-countdown="20.0">20 seconds</span> it will start its own Wi-Fi' in body
        assert "network cable" in body
        assert "location.reload()" in body

    def test_with_nothing_counting_down_there_is_no_countdown(self, unconfigured):
        from megalink_viewer.address import Reach

        server, reach = unconfigured
        reach["value"] = Reach("fp-09", [], 8080)
        body = self.page(server)[2]
        assert "data-countdown=" not in body
        assert "network cable" in body

    def test_names_are_escaped(self, unconfigured, config_path):
        # The display name is set by whoever configures it, and goes into HTML.
        server, _reach = unconfigured
        controller_config = load(config_path)
        controller_config.beacon.name = "<script>alert(1)</script>"
        save(controller_config, config_path)
        body = self.page(server)[2]
        assert "<script>alert" not in body


class TestWifi:
    """Choosing the display's network from its settings page."""

    STATUS: ClassVar[dict] = {
        "mode": "hotspot",
        "hotspot": {"ssid": "Megalink fp-09", "password": "7kqm-xw4p-9ht2", "address": "10.42.0.1"},
        "networks": [{"ssid": "Range", "signal": 70, "secure": True}],
        "joining": "",
        "error": "",
    }

    @pytest.fixture
    def served(self, config_path, fake_client):
        config = Config(host="stord-pk", range="1-10", lane="9")
        config.web.port = 0
        config.web.bind = "127.0.0.1"
        config.beacon.enabled = False
        save(config, config_path)
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        status = {"value": dict(self.STATUS)}
        server = ConfigServer(controller, read_network=lambda: status["value"]).start()
        yield controller, server, status, config_path
        server.stop()
        controller.stop()

    def test_the_networks_it_can_see_are_listed(self, served):
        _c, server, _s, _p = served
        _code, body = request(server, "/api/wifi")
        assert body["available"] and body["mode"] == "hotspot"
        assert body["networks"][0]["ssid"] == "Range"

    def test_no_password_is_handed_out(self, served):
        # Not the hotspot's, and never one being joined.
        _c, server, _s, _p = served
        _code, body = request(server, "/api/wifi")
        assert "7kqm-xw4p-9ht2" not in json.dumps(body)

    def test_a_display_without_the_service_says_so(self, served):
        _c, server, status, _p = served
        status["value"] = None
        assert request(server, "/api/wifi")[1] == {"available": False}

    def test_joining_leaves_a_request_beside_the_configuration(self, served):
        _c, server, _s, path = served
        code, body = request(
            server, "/api/wifi", "POST", {"ssid": "Range", "password": "secret-pass"}
        )
        assert code == 200 and body["queued"]
        left = path.parent / "wifi-request.json"
        assert json.loads(left.read_text()) == {
            "ssid": "Range",
            "password": "secret-pass",
            "country": "",
        }

    def test_the_request_is_private(self, served):
        import os
        import stat

        _c, server, _s, path = served
        request(server, "/api/wifi", "POST", {"ssid": "Range", "password": "secret-pass"})
        mode = stat.S_IMODE(os.stat(path.parent / "wifi-request.json").st_mode)
        assert mode == 0o600

    def test_an_open_network_needs_no_password(self, served):
        _c, server, _s, _p = served
        assert request(server, "/api/wifi", "POST", {"ssid": "CRPC Guest"})[0] == 200

    @pytest.mark.parametrize(
        "payload",
        [
            {"ssid": "", "password": "secret-pass"},
            {"ssid": "x" * 33, "password": "secret-pass"},
            {"ssid": "Range", "password": "short"},
            {"ssid": "Range", "password": "x" * 64},  # 64, but not hex
        ],
    )
    def test_what_wpa_would_refuse_is_refused_here(self, served, payload):
        # Told now, rather than half a minute after being disconnected.
        _c, server, _s, path = served
        assert expect_error(server, "/api/wifi", "POST", payload)[0] == 400
        assert not (path.parent / "wifi-request.json").exists()

    def test_a_raw_sixty_four_character_key_is_allowed(self, served):
        _c, server, _s, _p = served
        assert (
            request(server, "/api/wifi", "POST", {"ssid": "Range", "password": "a" * 64})[0] == 200
        )

    def test_joining_needs_the_token(self, served):
        controller, server, _s, _p = served
        controller.write(controller.config.merged({"web": {"token": "shared"}}))
        payload = {"ssid": "Range", "password": "secret-pass"}
        assert expect_error(server, "/api/wifi", "POST", payload)[0] == 403
        assert request(server, "/api/wifi", "POST", payload, token="shared")[0] == 200

    def test_joining_cannot_be_forged_from_another_site(self, served):
        # Of everything on the page this is the one that most wants protecting:
        # it moves the display to a network of the sender's choosing.
        _c, server, _s, path = served
        forged = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/wifi",
            data=json.dumps({"ssid": "Evil", "password": "12345678"}).encode(),
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(forged, timeout=10)
        assert caught.value.code == 415
        assert not (path.parent / "wifi-request.json").exists()

    def test_the_setup_page_gives_both_steps_on_the_hotspot(self, served):
        _c, server, _s, _p = served
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/setup", timeout=10) as r:
            body = r.read().decode()
        assert "1. Join its Wi-Fi" in body and "Megalink fp-09" in body
        assert f"http://10.42.0.1:{server.port}/" in body
        assert body.count("<svg") == 2


class TestWifiCountry:
    @pytest.fixture
    def served(self, config_path, fake_client):
        config = Config(host="stord-pk", range="1-10", lane="9")
        config.web.port = 0
        config.web.bind = "127.0.0.1"
        config.beacon.enabled = False
        save(config, config_path)
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        server = ConfigServer(
            controller, read_network=lambda: {"mode": "hotspot", "country": "CA"}
        ).start()
        yield server, config_path
        server.stop()
        controller.stop()

    def test_the_country_in_force_is_offered(self, served):
        server, _p = served
        assert request(server, "/api/wifi")[1]["country"] == "CA"

    def test_a_chosen_country_goes_with_the_network(self, served):
        server, path = served
        request(
            server,
            "/api/wifi",
            "POST",
            {"ssid": "Range", "password": "secret-pass", "country": "no"},
        )
        assert json.loads((path.parent / "wifi-request.json").read_text())["country"] == "NO"

    def test_a_country_must_be_two_letters(self, served):
        server, _p = served
        payload = {"ssid": "Range", "password": "secret-pass", "country": "Norway"}
        assert expect_error(server, "/api/wifi", "POST", payload)[0] == 400
