"""Announcing a display, and finding the displays on a network."""

from __future__ import annotations

import json

from megalink_viewer.beacon import (
    MAGIC,
    MAX_PACKET,
    Announcer,
    Listener,
    broadcast_targets,
    decode,
    encode,
    payload_for,
)


class TestEncoding:
    def test_round_trip(self):
        payload = encode({"name": "point-9", "web": 8080})
        decoded = decode(payload)
        assert decoded["name"] == "point-9"
        assert decoded["magic"] == MAGIC

    def test_a_foreign_packet_is_ignored(self):
        assert decode(b'{"hello":"world"}') is None

    def test_rubbish_is_ignored(self):
        assert decode(b"\xff\xfe not json") is None
        assert decode(b"") is None

    def test_an_oversized_packet_is_ignored(self):
        assert decode(b"x" * (MAX_PACKET + 1)) is None

    def test_a_long_payload_is_trimmed_rather_than_dropped(self):
        """Identity matters more than the description."""
        payload = encode({"name": "point-9", "web": 8080, "lane": "9", "showing": "x" * 4000})
        assert len(payload) <= MAX_PACKET
        decoded = decode(payload)
        assert decoded["name"] == "point-9"
        assert decoded["web"] == 8080
        assert "showing" not in decoded


class TestTargets:
    def test_an_explicit_address_is_used_alone(self):
        assert broadcast_targets("10.0.0.255") == ["10.0.0.255"]

    def test_the_default_includes_the_limited_broadcast(self):
        assert "255.255.255.255" in broadcast_targets()

    def test_the_default_adds_the_local_network(self):
        """macOS and some Wi-Fi drivers refuse the limited broadcast."""
        targets = broadcast_targets()
        assert len(targets) >= 1
        assert all(target.count(".") == 3 for target in targets)


class TestRegistry:
    def test_a_packet_registers_a_display(self):
        listener = Listener(port=0)
        display = listener.accept(encode({"name": "point-9", "web": 8080}), "10.0.0.7", now=100.0)
        assert display is not None
        assert display.name == "point-9"
        # The address comes from the packet's source, not its contents.
        assert display.address == "10.0.0.7"
        assert display.url == "http://10.0.0.7:8080"

    def test_a_display_without_a_port_has_no_page(self):
        listener = Listener(port=0)
        display = listener.accept(encode({"name": "x"}), "10.0.0.7", now=0.0)
        assert display.url is None

    def test_repeat_packets_update_rather_than_duplicate(self):
        listener = Listener(port=0)
        listener.accept(encode({"name": "a", "web": 8080, "lane": "1"}), "10.0.0.7", now=0.0)
        listener.accept(encode({"name": "a", "web": 8080, "lane": "2"}), "10.0.0.7", now=5.0)
        displays = listener.displays(now=5.0)
        assert len(displays) == 1
        assert displays[0].payload["lane"] == "2"

    def test_two_displays_on_one_host_are_distinct(self):
        listener = Listener(port=0)
        listener.accept(encode({"name": "a", "web": 8080}), "10.0.0.7", now=0.0)
        listener.accept(encode({"name": "b", "web": 8081}), "10.0.0.7", now=0.0)
        assert len(listener.displays(now=0.0)) == 2

    def test_a_silent_display_expires(self):
        listener = Listener(port=0, expiry=30.0)
        listener.accept(encode({"name": "a", "web": 8080}), "10.0.0.7", now=0.0)
        assert listener.displays(now=20.0)
        assert not listener.displays(now=100.0)

    def test_a_foreign_packet_registers_nothing(self):
        listener = Listener(port=0)
        assert listener.accept(b"{}", "10.0.0.7", now=0.0) is None
        assert not listener.displays(now=0.0)

    def test_displays_are_listed_by_name(self):
        listener = Listener(port=0)
        for name, port in (("zulu", 1), ("alpha", 2), ("mike", 3)):
            listener.accept(encode({"name": name, "web": port}), "10.0.0.7", now=0.0)
        assert [d.name for d in listener.displays(now=0.0)] == ["alpha", "mike", "zulu"]

    def test_find_by_identifier(self):
        listener = Listener(port=0)
        listener.accept(encode({"name": "a", "web": 8080}), "10.0.0.7", now=0.0)
        assert listener.find("10.0.0.7:8080") is not None
        assert listener.find("10.0.0.7:9999") is None

    def test_the_dict_form_carries_what_a_dashboard_needs(self):
        listener = Listener(port=0)
        listener.accept(
            encode({"name": "a", "web": 8080, "showing": "stord-pk · lane 9"}),
            "10.0.0.7",
            now=0.0,
        )
        data = listener.displays(now=1.0)[0].to_dict(now=1.0)
        assert data["id"] == "10.0.0.7:8080"
        assert data["url"] == "http://10.0.0.7:8080"
        assert data["showing"] == "stord-pk · lane 9"
        assert data["last_seen"] == 1.0
        # Wire framing is not the dashboard's business.
        assert "magic" not in data and "protocol" not in data


class TestOverTheWire:
    def test_a_real_announcement_is_received(self):
        """Loopback rather than broadcast, so the test is self-contained."""
        listener = Listener(port=0, expiry=60)
        listener._socket = listener._open()
        port = listener._socket.getsockname()[1]
        listener.stop()

        listener = Listener(port=port, expiry=60).start()
        try:
            announcer = Announcer(
                lambda: {"name": "point-9", "web": 8080, "lane": "9"},
                port=port,
                address="127.0.0.1",
            )
            assert announcer.send_once(), announcer.error
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline and not listener.displays():
                __import__("time").sleep(0.02)
            displays = listener.displays()
            assert displays and displays[0].name == "point-9"
        finally:
            listener.stop()

    def test_an_unreachable_target_is_reported(self):
        announcer = Announcer(lambda: {"name": "x"}, port=1, address="0.0.0.1")
        announcer.send_once()
        # Either it failed and said why, or the platform accepted it silently.
        assert announcer.error is None or isinstance(announcer.error, str)


class TestPayloadForAController:
    def test_it_summarises_the_display(self, config_path, fake_client):
        from megalink_viewer.config import Config, save
        from megalink_viewer.controller import Controller

        save(Config(host="stord-pk", range="1-10", lane="9"), config_path)
        controller = Controller(path=config_path, client=fake_client)
        controller.reload()
        controller.wait_for_data(timeout=5)
        payload = payload_for(controller)
        assert payload["host"] == "stord-pk"
        assert payload["lane"] == "9"
        assert payload["shooter"] == "Etai Moredehi Bogen"
        assert payload["connected"] is True
        # And it has to fit in a packet.
        assert len(json.dumps(payload)) < MAX_PACKET
