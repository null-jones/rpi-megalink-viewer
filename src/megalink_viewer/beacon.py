"""Letting a fleet dashboard find the displays on a range.

Each display broadcasts a small UDP packet every few seconds saying who it is,
what it is showing and which port its configuration page is on. A dashboard on
a laptop listens for those packets and builds a list.

UDP broadcast rather than mDNS on purpose: a range network is one flat subnet,
broadcast needs nothing installed on either end, and the alternative would be a
dependency (``zeroconf``) or a system service (``avahi``) on a machine chosen
for being small. The trade-off is that broadcast does not cross a router, so a
dashboard has to sit on the same network as the displays -- which it does.

A display's address comes from the packet's source, not its contents, so a
display that does not know its own IP is still reachable.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from typing import Any, Callable

#: Marker so a stray packet on this port is not mistaken for one of ours.
MAGIC = "megalink-display"
PROTOCOL = 1

#: Packets are kept small enough to never fragment.
MAX_PACKET = 1024

#: A display is treated as gone after this long without a packet.
DEFAULT_EXPIRY = 45.0


def encode(payload: dict[str, Any]) -> bytes:
    """Encode an announcement, trimming it if it would be oversized."""
    body = {"magic": MAGIC, "protocol": PROTOCOL, **payload}
    raw = json.dumps(body, default=str).encode("utf-8")
    if len(raw) <= MAX_PACKET:
        return raw
    # Drop the descriptive extras rather than the identity.
    essential = {
        key: body[key]
        for key in ("magic", "protocol", "name", "web", "host", "range", "lane")
        if key in body
    }
    return json.dumps(essential, default=str).encode("utf-8")


def decode(raw: bytes) -> dict[str, Any] | None:
    """Decode an announcement, or ``None`` if it is not one of ours."""
    if len(raw) > MAX_PACKET:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict) or body.get("magic") != MAGIC:
        return None
    return body


def primary_address() -> str | None:
    """This machine's address on the network it would use to reach the world.

    No packet is sent -- connecting a UDP socket only picks a route -- but it is
    the one dependency-free way to learn which interface matters on a host with
    several.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # reserved documentation address
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


def broadcast_targets(address: str = "") -> list[str]:
    """Where to send announcements.

    An explicit address wins. Otherwise: the limited broadcast address, which is
    what works on Linux, plus the local network's own broadcast address, because
    macOS and some Wi-Fi drivers refuse the limited one. Both are tried; one
    getting through is enough.
    """
    if address:
        return [address]
    targets = ["255.255.255.255"]
    local = primary_address()
    if local:
        octets = local.split(".")
        if len(octets) == 4:
            # Assuming a /24 is a heuristic, but range networks are flat and
            # this is the shape of essentially all of them.
            subnet = ".".join([*octets[:3], "255"])
            if subnet not in targets:
                targets.append(subnet)
    return targets


class Announcer:
    """Broadcasts what a display is showing, on a timer."""

    def __init__(
        self,
        payload: Callable[[], dict[str, Any]],
        port: int,
        interval: float = 10.0,
        address: str = "",
    ) -> None:
        self._payload = payload
        self.port = int(port)
        self.interval = max(1.0, float(interval))
        #: Empty means work it out; see :func:`broadcast_targets`.
        self.address = address
        self.targets = broadcast_targets(address)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Last failure, if any -- surfaced rather than swallowed, because a
        #: display nobody can find looks identical to one that is switched off.
        self.error: str | None = None

    def send_once(self) -> bool:
        """Send one announcement. Returns whether any target accepted it."""
        packet = encode(self._payload())
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        failures = []
        try:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for target in self.targets:
                try:
                    sock.sendto(packet, (target, self.port))
                except OSError as exc:
                    failures.append(f"{target}: {exc}")
                else:
                    self.error = None
                    return True
        finally:
            sock.close()
        self.error = "; ".join(failures) or "no broadcast target"
        return False

    def start(self) -> Announcer:
        def run() -> None:
            while True:
                self.send_once()
                if self._stop.wait(self.interval):
                    return

        self._thread = threading.Thread(target=run, name="megalink-beacon", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def __enter__(self) -> Announcer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


class Display:
    """A display a dashboard has heard from."""

    __slots__ = ("address", "first_seen", "last_seen", "payload")

    def __init__(self, address: str, payload: dict[str, Any], now: float) -> None:
        self.address = address
        self.payload = payload
        self.first_seen = now
        self.last_seen = now

    @property
    def name(self) -> str:
        return str(self.payload.get("name") or self.address)

    @property
    def web_port(self) -> int:
        try:
            return int(self.payload.get("web") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def url(self) -> str | None:
        """Where this display's configuration page lives."""
        port = self.web_port
        return f"http://{self.address}:{port}" if port else None

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.monotonic()) - self.last_seen

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        data = dict(self.payload)
        data.pop("magic", None)
        data.pop("protocol", None)
        data.update(
            {
                "address": self.address,
                "url": self.url,
                "last_seen": round(self.age(now), 1),
                "id": f"{self.address}:{self.web_port}",
            }
        )
        return data

    def __repr__(self) -> str:
        return f"Display({self.name!r}, {self.address!r})"


class Listener:
    """Collects announcements into a registry of live displays."""

    def __init__(self, port: int, expiry: float = DEFAULT_EXPIRY) -> None:
        self.port = int(port)
        self.expiry = float(expiry)
        self._displays: dict[str, Display] = {}
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _open(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # So a dashboard and a display can share a machine during setup. Note
        # that this shares the *broadcast* traffic they are both there for;
        # two listeners on one host split unicast datagrams between them
        # instead of each getting a copy, which matters only when testing a
        # fleet on a single machine. A range runs one listener per Pi.
        reuse_port = getattr(socket, "SO_REUSEPORT", None)
        if reuse_port is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, reuse_port, 1)
        sock.bind(("", self.port))
        sock.settimeout(0.5)
        return sock

    def accept(self, raw: bytes, address: str, now: float | None = None) -> Display | None:
        """Record one packet. Exposed so the registry can be tested directly."""
        payload = decode(raw)
        if payload is None:
            return None
        moment = now if now is not None else time.monotonic()
        try:
            port = int(payload.get("web") or 0)
        except (TypeError, ValueError):
            port = 0
        key = f"{address}:{port}"
        with self._lock:
            found = self._displays.get(key)
            if found is None:
                found = Display(address, payload, moment)
                self._displays[key] = found
            else:
                found.payload = payload
                found.last_seen = moment
            return found

    def displays(self, now: float | None = None) -> list[Display]:
        """Everything heard from recently, most recently seen first."""
        moment = now if now is not None else time.monotonic()
        with self._lock:
            stale = [key for key, d in self._displays.items() if moment - d.last_seen > self.expiry]
            for key in stale:
                del self._displays[key]
            found = list(self._displays.values())
        found.sort(key=lambda d: (d.name.lower(), d.address))
        return found

    def find(self, identifier: str) -> Display | None:
        with self._lock:
            return self._displays.get(identifier)

    def start(self) -> Listener:
        self._socket = self._open()

        def run() -> None:
            sock = self._socket
            assert sock is not None
            while not self._stop.is_set():
                try:
                    raw, sender = sock.recvfrom(MAX_PACKET + 1)
                except socket.timeout:
                    continue
                except OSError:
                    return
                self.accept(raw, sender[0])

        self._thread = threading.Thread(target=run, name="megalink-listen", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def __enter__(self) -> Listener:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def payload_for(controller: Any) -> dict[str, Any]:
    """The announcement for a running display."""
    status = controller.status()
    config = controller.config
    return {
        "name": config.name,
        "web": config.web.port,
        "host": status.get("host"),
        "range": status.get("range"),
        "lane": status.get("lane"),
        "showing": status.get("showing"),
        "shooter": status.get("shooter"),
        "total": status.get("total"),
        "shots": status.get("shots"),
        "connected": status.get("connected"),
        "age": status.get("age"),
        "error": status.get("error"),
    }
