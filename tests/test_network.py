"""The Wi-Fi hotspot a display falls back to when it cannot get on a network.

The rules about *when* are tested against a pretend clock; what NetworkManager
is asked to do is tested against a pretend nmcli. That the connection files are
ones NetworkManager accepts was checked with ``nmcli --offline`` (1.52, Trixie)
when this was written; what is left for a real Pi is whether its Wi-Fi chip
brings the hotspot up.
"""

from __future__ import annotations

import json
import os
import stat
from typing import ClassVar

import pytest

from megalink_viewer import network
from megalink_viewer.network import Memory, Observation, decide

HOTSPOT = network.HOTSPOT_ID


def seen(connected=False, hotspot=False, clients=0, request=None, ssid=""):
    return Observation(connected, hotspot, clients, request, ssid)


class TestReadingNmcli:
    def test_fields_split_on_colons(self):
        assert network.split_terse("wlan0:wifi:connected:Range") == [
            "wlan0",
            "wifi",
            "connected",
            "Range",
        ]

    def test_escaped_colons_stay_in_their_field(self):
        # A network called "Range:1" arrives as "Range\:1".
        assert network.split_terse(r"Range\:1:80:WPA2") == ["Range:1", "80", "WPA2"]

    def test_escaped_backslashes_too(self):
        assert network.split_terse(r"back\\slash:1") == ["back\\slash", "1"]

    def test_empty_fields_are_kept(self):
        assert network.split_terse("wlan0:wifi:disconnected:") == [
            "wlan0",
            "wifi",
            "disconnected",
            "",
        ]

    def test_devices(self):
        devices = network.parse_devices(
            "wlan0:wifi:connected:Range\neth0:ethernet:unavailable:\nlo:loopback:connected (externally):lo\n"
        )
        assert [(d.name, d.connected) for d in devices] == [
            ("wlan0", True),
            ("eth0", False),
            ("lo", True),
        ]

    def test_scan_keeps_each_network_once_at_its_strongest(self):
        found = network.parse_scan("Range:40:WPA2\nRange:72:WPA2\nGuest:55:\n:90:WPA2\n")
        # Strongest first, the hidden network (no name) left out.
        assert found == [
            {"ssid": "Range", "signal": 72, "secure": True},
            {"ssid": "Guest", "signal": 55, "secure": False},
        ]

    def test_scan_marks_open_networks(self):
        assert network.parse_scan("Cafe:50:--\n")[0]["secure"] is False


class TestConnectionFiles:
    def test_a_network_to_join(self):
        text = network.keyfile("megalink Range", "Range", "secret-pass")
        assert "mode=infrastructure" in text
        assert "key-mgmt=wpa-psk" in text and "psk=secret-pass" in text
        assert "autoconnect=true" in text
        assert "method=auto" in text

    def test_an_open_network_has_no_security_section(self):
        assert "[wifi-security]" not in network.keyfile("megalink Guest", "Guest")

    def test_the_hotspot(self):
        text = network.keyfile(HOTSPOT, "Megalink fp-09", "7kqm-xw4p-9ht2", hotspot=True)
        assert "mode=ap" in text
        assert "method=shared" in text  # NetworkManager runs the DHCP
        assert "band=bg" in text  # 2.4 GHz: every phone, and all a Zero has

    def test_the_hotspot_never_starts_on_its_own(self):
        # This service decides when; left to itself NetworkManager would bring
        # it up at boot and never try the real network.
        assert "autoconnect=false" in network.keyfile(HOTSPOT, "x", "12345678", hotspot=True)

    def test_the_hotspot_turns_off_management_frame_protection(self):
        # The Pi's Wi-Fi firmware will not run an access point with it, and
        # NetworkManager's default of trying it is a known way to get no hotspot.
        assert "pmf=1" in network.keyfile(HOTSPOT, "x", "12345678", hotspot=True)

    def test_awkward_characters_are_escaped(self):
        # Checked against NetworkManager 1.52, which read these back exactly.
        text = network.keyfile("id", " lead\\back\nline", "p")
        assert "ssid=\\slead\\\\back\\nline" in text

    def test_a_network_is_filed_under_this_packages_name(self):
        connection_id, filename = network.profile_name("Range; with / odd\\chars")
        assert connection_id == "megalink Range; with / odd\\chars"
        assert filename.startswith("megalink-wifi-") and filename.endswith(".nmconnection")
        assert "/" not in filename and "\\" not in filename and " " not in filename

    def test_the_same_network_gets_the_same_file(self):
        # So entering it again replaces the first rather than piling up copies.
        assert network.profile_name("Range") == network.profile_name("Range")


class TestHotspotIdentity:
    def test_the_name_says_what_it_is(self):
        assert network.hotspot_ssid("fp-09") == "Megalink fp-09"

    def test_the_name_fits_in_thirty_two_bytes(self):
        name = network.hotspot_ssid("å" * 40)
        assert len(name.encode("utf-8")) <= 32
        name.encode("utf-8").decode("utf-8")  # not cut through a character

    def test_the_password_is_long_enough_for_wpa(self):
        assert len(network.new_password()) >= 8

    def test_the_password_has_no_look_alikes(self):
        # It is read off a screen, and sometimes typed.
        for _ in range(200):
            password = network.new_password()
            assert not set(password) & set("0O1lI"), password

    def test_the_secret_is_made_once_and_kept(self, tmp_path):
        path = tmp_path / "hotspot.json"
        first = network.load_secret(path, "fp-09")
        assert network.load_secret(path, "fp-09") == first
        assert first["ssid"] == "Megalink fp-09"

    def test_the_secret_is_readable_only_by_root(self, tmp_path):
        path = tmp_path / "hotspot.json"
        network.load_secret(path, "fp-09")
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


class TestBooting:
    def test_a_network_found_in_time_means_no_hotspot(self):
        memory = Memory(since=0)
        assert decide(10, seen(connected=True), memory) == []
        assert memory.mode == "client"

    def test_it_waits_thirty_seconds_before_giving_up(self):
        memory = Memory(since=0)
        assert decide(29, seen(), memory) == []
        assert memory.mode == "waiting"

    def test_after_thirty_seconds_the_hotspot_comes_up(self):
        memory = Memory(since=0)
        assert decide(30, seen(), memory) == ["start_hotspot"]
        assert memory.mode == "hotspot"

    def test_a_hotspot_left_up_by_the_last_run_is_adopted(self):
        # The service restarted; the hotspot is still there. Do not restart it.
        memory = Memory(since=0)
        assert decide(1, seen(hotspot=True), memory) == []
        assert memory.mode == "hotspot"


class TestLosingTheNetwork:
    def client(self):
        memory = Memory(since=0)
        decide(1, seen(connected=True), memory)
        return memory

    def test_a_short_drop_is_ridden_out(self):
        # A range's router restarting takes a minute or two.
        memory = self.client()
        decide(100, seen(), memory)
        assert decide(100 + network.LOST_GRACE - 1, seen(), memory) == []
        assert memory.mode == "client"

    def test_a_long_one_brings_the_hotspot_up(self):
        memory = self.client()
        decide(100, seen(), memory)
        assert decide(100 + network.LOST_GRACE, seen(), memory) == ["start_hotspot"]

    def test_coming_back_resets_the_count(self):
        memory = self.client()
        decide(100, seen(), memory)
        decide(150, seen(connected=True), memory)
        decide(200, seen(), memory)
        # 120s after the *second* drop, not the first.
        assert decide(200 + network.LOST_GRACE - 1, seen(), memory) == []


class TestOnTheHotspot:
    def hotspot(self, at=0):
        memory = Memory(since=at)
        decide(at + network.BOOT_GRACE, seen(), memory)
        return memory, at + network.BOOT_GRACE

    def test_it_steps_aside_now_and_then_to_look_for_a_network(self):
        memory, start = self.hotspot()
        assert decide(start + network.RETRY_INTERVAL, seen(hotspot=True), memory) == [
            "stop_hotspot"
        ]
        assert memory.mode == "retrying"

    def test_but_never_while_a_phone_is_connected(self):
        # Somebody is setting the display up; dropping the hotspot would cut
        # them off halfway through.
        memory, start = self.hotspot()
        assert (
            decide(start + network.RETRY_INTERVAL * 3, seen(hotspot=True, clients=1), memory) == []
        )
        assert memory.mode == "hotspot"

    def test_a_network_that_came_back_is_kept(self):
        memory, start = self.hotspot()
        decide(start + network.RETRY_INTERVAL, seen(hotspot=True), memory)
        assert decide(start + network.RETRY_INTERVAL + 10, seen(connected=True), memory) == []
        assert memory.mode == "client"

    def test_one_that_did_not_brings_the_hotspot_back(self):
        memory, start = self.hotspot()
        decide(start + network.RETRY_INTERVAL, seen(hotspot=True), memory)
        back = start + network.RETRY_INTERVAL + network.RETRY_WINDOW
        assert decide(back, seen(), memory) == ["start_hotspot"]
        assert memory.mode == "hotspot"

    def test_a_cable_plugged_in_ends_the_hotspot(self):
        memory, start = self.hotspot()
        assert decide(start + 10, seen(connected=True, hotspot=True), memory) == ["stop_hotspot"]
        assert memory.mode == "client"

    def test_a_hotspot_that_vanished_is_brought_back(self):
        memory, start = self.hotspot()
        assert decide(start + network.TICK * 2, seen(hotspot=False), memory) == ["start_hotspot"]


class TestJoiningANetwork:
    REQUEST: ClassVar[dict[str, str]] = {"ssid": "Range", "password": "secret-pass"}

    def hotspot(self):
        memory = Memory(since=0)
        decide(network.BOOT_GRACE, seen(), memory)
        return memory

    def test_a_request_takes_the_hotspot_down_and_joins(self):
        memory = self.hotspot()
        assert decide(40, seen(hotspot=True, request=self.REQUEST), memory) == [
            "stop_hotspot",
            "join",
        ]
        assert memory.mode == "joining" and memory.joining == "Range"

    def test_joining_the_right_network_succeeds(self):
        memory = self.hotspot()
        decide(40, seen(hotspot=True, request=self.REQUEST), memory)
        decide(50, seen(connected=True, ssid="Range"), memory)
        assert memory.mode == "client"
        assert memory.error == ""

    def test_a_wrong_password_brings_the_hotspot_back_with_a_reason(self):
        memory = self.hotspot()
        decide(40, seen(hotspot=True, request=self.REQUEST), memory)
        assert decide(40 + network.JOIN_WINDOW, seen(), memory) == ["start_hotspot"]
        assert memory.mode == "hotspot"
        assert "Range" in memory.error

    def test_changing_network_from_a_working_one(self):
        # On the range network, asked to move to another.
        memory = Memory(since=0)
        decide(1, seen(connected=True, ssid="Old"), memory)
        assert decide(5, seen(connected=True, ssid="Old", request=self.REQUEST), memory) == ["join"]

    def test_still_on_the_old_network_is_not_mistaken_for_success(self):
        memory = Memory(since=0)
        decide(1, seen(connected=True, ssid="Old"), memory)
        decide(5, seen(connected=True, ssid="Old", request=self.REQUEST), memory)
        decide(10, seen(connected=True, ssid="Old"), memory)
        assert memory.mode == "joining"

    def test_failing_to_move_leaves_it_where_it_was(self):
        memory = Memory(since=0)
        decide(1, seen(connected=True, ssid="Old"), memory)
        decide(5, seen(connected=True, ssid="Old", request=self.REQUEST), memory)
        # Nothing to start: it is still on a network, just not the new one.
        assert decide(5 + network.JOIN_WINDOW, seen(connected=True, ssid="Old"), memory) == []
        assert memory.mode == "client"
        assert "Range" in memory.error


class TestRequests:
    def test_a_request_is_taken_once(self, tmp_path):
        path = tmp_path / "wifi-request.json"
        network.write_request("Range", "secret-pass", path)
        assert network.read_request(path) == {"ssid": "Range", "password": "secret-pass"}
        assert network.read_request(path) is None
        assert not path.exists()

    def test_a_request_holds_a_password_so_only_its_owner_reads_it(self, tmp_path):
        path = tmp_path / "wifi-request.json"
        network.write_request("Range", "secret-pass", path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_rubbish_is_ignored_and_removed(self, tmp_path):
        path = tmp_path / "wifi-request.json"
        path.write_text("not json")
        assert network.read_request(path) is None
        assert not path.exists()

    def test_a_request_with_no_network_name_is_ignored(self, tmp_path):
        path = tmp_path / "wifi-request.json"
        path.write_text(json.dumps({"ssid": "  ", "password": "x"}))
        assert network.read_request(path) is None


class TestStatus:
    SECRET: ClassVar[dict[str, str]] = {"ssid": "Megalink fp-09", "password": "7kqm-xw4p-9ht2"}

    def test_on_the_hotspot_it_gives_the_way_in(self):
        memory = Memory(mode="hotspot")
        out = network.status(memory, self.SECRET, [])
        assert out["hotspot"] == {
            "ssid": "Megalink fp-09",
            "password": "7kqm-xw4p-9ht2",
            "address": network.HOTSPOT_ADDRESS,
        }

    def test_off_it_there_is_nothing_to_join(self):
        assert network.status(Memory(mode="client"), self.SECRET, [])["hotspot"] is None

    def test_a_network_being_joined_never_has_its_password_written(self):
        memory = Memory(mode="joining")
        memory.joining = "Range"
        text = json.dumps(network.status(memory, self.SECRET, []))
        assert "Range" in text and "secret-pass" not in text

    def test_reading_a_missing_status_is_none(self, tmp_path):
        assert network.read_status(tmp_path / "missing.json") is None


class FakeNmcli:
    """Answers nmcli and iw as a Pi would, and records what it was asked."""

    def __init__(self, devices="wlan0:wifi:disconnected:\n", running=True):
        self.devices = devices
        self.running = running
        self.calls = []

    def __call__(self, args, timeout=30.0):
        self.calls.append(args)
        if args[:4] == ["nmcli", "-t", "-f", "RUNNING"]:
            return 0, "running\n" if self.running else "stopped\n"
        if "device" in args and "status" in args:
            return 0, self.devices
        if args[:2] == ["iw", "dev"]:
            return 0, ""
        if "wifi" in args and "list" in args:
            return 0, "Range:70:WPA2\n"
        if args[-2:] and "up" in args:
            if network.HOTSPOT_ID in args:
                self.devices = f"wlan0:wifi:connected:{network.HOTSPOT_ID}\n"
            return 0, ""
        return 0, ""


class TestNetworkManager:
    def test_a_client_connection_counts_as_connected(self, tmp_path):
        nm = network.NetworkManager(FakeNmcli("wlan0:wifi:connected:Range\n"), tmp_path)
        assert network.observe(nm, None).connected

    def test_the_hotspot_does_not_count_as_being_on_a_network(self, tmp_path):
        nm = network.NetworkManager(FakeNmcli(f"wlan0:wifi:connected:{HOTSPOT}\n"), tmp_path)
        observed = network.observe(nm, None)
        assert observed.hotspot_active and not observed.connected

    def test_a_cable_counts(self, tmp_path):
        nm = network.NetworkManager(
            FakeNmcli("eth0:ethernet:connected:Wired\nwlan0:wifi:disconnected:\n"), tmp_path
        )
        assert network.observe(nm, None).connected

    def test_connection_files_are_private_and_reloaded(self, tmp_path):
        fake = FakeNmcli()
        nm = network.NetworkManager(fake, tmp_path)
        nm.save("x.nmconnection", "[connection]\n")
        # NetworkManager ignores a connection file anyone else can read.
        assert stat.S_IMODE(os.stat(tmp_path / "x.nmconnection").st_mode) == 0o600
        assert ["nmcli", "connection", "reload"] in fake.calls

    def test_no_password_is_ever_put_on_a_command_line(self, tmp_path):
        # Where, for as long as the command runs, any process could read it.
        fake = FakeNmcli()
        status, request = tmp_path / "status.json", tmp_path / "request.json"
        network.write_request("Range", "the-real-secret", request)
        ticks = iter(range(0, 10_000, 5))
        stops = iter([False, False, True])
        network.run(
            network.NetworkManager(fake, tmp_path / "nm"),
            stop=lambda: next(stops),
            report=lambda _m: None,
            clock=lambda: next(ticks),
            sleep=lambda _s: None,
            status_path=status,
            request_path=request,
            secret_path=tmp_path / "secret.json",
        )
        secret = json.loads((tmp_path / "secret.json").read_text())["password"]
        for call in fake.calls:
            joined = " ".join(call)
            assert "the-real-secret" not in joined
            assert secret not in joined


class TestTheService:
    def run(self, tmp_path, fake, ticks, request=None):
        status, request_path = tmp_path / "status.json", tmp_path / "request.json"
        if request:
            network.write_request(*request, request_path)
        clock = iter(range(0, 100_000, 5))
        remaining = iter([False] * ticks + [True])
        said = []
        network.run(
            network.NetworkManager(fake, tmp_path / "nm"),
            stop=lambda: next(remaining),
            name="fp-09",
            report=said.append,
            clock=lambda: next(clock),
            sleep=lambda _s: None,
            status_path=status,
            request_path=request_path,
            secret_path=tmp_path / "secret.json",
        )
        return json.loads(status.read_text()), said

    def test_with_no_network_the_hotspot_comes_up_and_says_how_to_join(self, tmp_path):
        out, _said = self.run(tmp_path, FakeNmcli(), ticks=10)
        assert out["mode"] == "hotspot"
        assert out["hotspot"]["ssid"] == "Megalink fp-09"
        assert out["hotspot"]["address"] == "10.42.0.1"

    def test_the_hotspot_file_is_written_before_it_is_started(self, tmp_path):
        self.run(tmp_path, FakeNmcli(), ticks=10)
        text = (tmp_path / "nm" / f"{HOTSPOT}.nmconnection").read_text()
        assert "mode=ap" in text

    def test_it_looks_round_before_the_radio_becomes_a_hotspot(self, tmp_path):
        # Once it is a hotspot it cannot scan, so the list is taken first.
        out, _said = self.run(tmp_path, FakeNmcli(), ticks=10)
        assert out["networks"] == [{"ssid": "Range", "signal": 70, "secure": True}]

    def test_on_a_network_it_stays_put(self, tmp_path):
        out, _said = self.run(tmp_path, FakeNmcli("wlan0:wifi:connected:Range\n"), ticks=10)
        assert out["mode"] == "client"
        assert out["hotspot"] is None

    def test_the_status_file_is_readable_by_the_display(self, tmp_path):
        self.run(tmp_path, FakeNmcli(), ticks=2)
        assert stat.S_IMODE(os.stat(tmp_path / "status.json").st_mode) == 0o644

    def test_a_request_is_written_as_a_connection_and_joined(self, tmp_path):
        fake = FakeNmcli()
        self.run(tmp_path, fake, ticks=2, request=("Range", "secret-pass"))
        _id, filename = network.profile_name("Range")
        assert "psk=secret-pass" in (tmp_path / "nm" / filename).read_text()
        assert any("up" in call and "megalink Range" in call for call in fake.calls)

    def test_without_networkmanager_it_waits_rather_than_failing(self, tmp_path):
        said = []
        stops = iter([False, True])
        network.run(
            network.NetworkManager(FakeNmcli(running=False), tmp_path),
            stop=lambda: next(stops, True),
            report=said.append,
            sleep=lambda _s: None,
            status_path=tmp_path / "status.json",
            request_path=tmp_path / "request.json",
            secret_path=tmp_path / "secret.json",
        )
        assert any("NetworkManager is not running" in line for line in said)
        assert not (tmp_path / "status.json").exists()


def test_the_hotspot_address_is_networkmanagers():
    # NetworkManager's shared mode always takes this address on the hotspot.
    assert network.HOTSPOT_ADDRESS == "10.42.0.1"


@pytest.mark.parametrize("value", ["plain", "with space", "semi;colon", 'quote"d'])
def test_ordinary_values_are_left_alone(value):
    assert network._keyfile_value(value) == value
