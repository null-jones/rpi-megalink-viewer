"""Working out the address to put on the screen."""

from __future__ import annotations

import json

from megalink_viewer import address


def ip_json(*interfaces):
    """What ``ip -json -4 address show`` prints, for the given interfaces."""
    out = [
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "scope": "host"}]}
    ]
    for name, local in interfaces:
        out.append(
            {"ifname": name, "addr_info": [{"family": "inet", "local": local, "scope": "global"}]}
        )
    return json.dumps(out)


class TestParsing:
    def test_loopback_is_never_offered(self):
        assert address.parse_ip_json(ip_json()) == []

    def test_wired_and_wireless_are_found(self):
        found = address.parse_ip_json(ip_json(("wlan0", "192.168.1.23"), ("eth0", "10.0.0.5")))
        assert found == [("eth0", "10.0.0.5"), ("wlan0", "192.168.1.23")]

    def test_containers_and_vpns_are_not_the_way_in(self):
        found = address.parse_ip_json(
            ip_json(
                ("docker0", "172.17.0.1"), ("tailscale0", "100.96.0.2"), ("wlan0", "192.168.1.9")
            )
        )
        assert found == [("wlan0", "192.168.1.9")]

    def test_rubbish_is_nothing(self):
        assert address.parse_ip_json("") == []
        assert address.parse_ip_json("not json") == []
        assert address.parse_ip_json("{}") == []


class TestFinding:
    def find(self, listed="", outbound=None, port=8080):
        return address.find(port, run=lambda: listed, hostname="fp-09", outbound=lambda: outbound)

    def test_the_url_is_by_address(self):
        # An address, not the .local name: plenty of phones cannot resolve those.
        reach = self.find(ip_json(("wlan0", "192.168.1.23")), outbound="192.168.1.23")
        assert reach.url() == "http://192.168.1.23:8080/"

    def test_the_local_name_is_offered_too(self):
        assert (
            self.find(ip_json(("wlan0", "192.168.1.23"))).local_url() == "http://fp-09.local:8080/"
        )

    def test_the_way_out_is_preferred(self):
        reach = self.find(
            ip_json(("eth0", "10.0.0.5"), ("wlan0", "192.168.1.23")), outbound="192.168.1.23"
        )
        assert reach.address == "192.168.1.23"

    def test_a_hotspot_with_no_way_out_still_has_an_address(self):
        # A Pi running its own Wi-Fi has no route anywhere, so the outbound probe
        # has nothing to say -- but the phone that has joined it can still reach it.
        reach = self.find(ip_json(("wlan0", "10.42.0.1")), outbound=None)
        assert reach.url() == "http://10.42.0.1:8080/"

    def test_a_machine_without_ip_still_finds_its_way_out(self):
        # macOS, and anything else without iproute2.
        assert self.find("", outbound="192.168.1.50").url() == "http://192.168.1.50:8080/"

    def test_no_network_at_all_is_no_url(self):
        assert self.find("", outbound=None).url() is None

    def test_port_80_is_left_out(self):
        assert self.find(ip_json(("eth0", "10.0.0.5")), port=80).url() == "http://10.0.0.5/"

    def test_a_path_can_be_added(self):
        assert (
            self.find(ip_json(("eth0", "10.0.0.5"))).url("/fleet") == "http://10.0.0.5:8080/fleet"
        )

    def test_the_probe_sends_nothing_and_answers_something_here(self):
        # Not asserting what: only that it runs, and never answers loopback.
        found = address.outbound_address()
        assert found is None or not found.startswith("127.")
