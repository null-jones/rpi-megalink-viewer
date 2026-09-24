"""How to reach this display from a phone.

What goes on the screen of a display that has not been set up yet: the address
of its configuration page. That is less simple than it sounds, because a Pi can
be on a range network, on its own Wi-Fi hotspot with no route anywhere, or not
on any network yet -- and the address has to be right in all three.

An IP address goes in the QR code, not the ``.local`` name. Plenty of phones
still cannot resolve ``.local`` names, while an address works on any phone on
the same network. The name is shown as well, for people.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from collections.abc import Callable
from typing import Any

#: Interfaces that are never the way in from a phone: containers and bridges on
#: a machine that also happens to run them.
_IGNORED = ("docker", "veth", "br-", "virbr", "lxc", "tailscale", "wg", "tun", "zt")


class Reach:
    """Where this display's configuration page can be found."""

    __slots__ = ("addresses", "hostname", "port", "primary_address")

    def __init__(
        self,
        hostname: str,
        addresses: list[tuple[str, str]],
        port: int,
        primary_address: str | None = None,
    ) -> None:
        self.hostname = hostname
        #: ``(interface, address)`` for every address a phone might use.
        self.addresses = addresses
        self.port = int(port)
        self.primary_address = primary_address

    @property
    def address(self) -> str | None:
        """The one address to show first: the way out, if there is one."""
        known = [address for _name, address in self.addresses]
        if self.primary_address and (self.primary_address in known or not known):
            return self.primary_address
        return known[0] if known else None

    def url(self, path: str = "/") -> str | None:
        """The configuration page by address, or ``None`` with no network."""
        address = self.address
        if address is None or not self.port:
            return None
        return f"http://{address}{self._port_suffix()}{path}"

    def local_url(self, path: str = "/") -> str | None:
        """The same page by ``.local`` name, for people rather than cameras."""
        if not self.hostname or not self.port:
            return None
        return f"http://{self.hostname}.local{self._port_suffix()}{path}"

    def _port_suffix(self) -> str:
        return "" if self.port == 80 else f":{self.port}"

    def __repr__(self) -> str:
        return f"Reach({self.hostname!r}, {self.address!r}, port={self.port})"


def parse_ip_json(text: str) -> list[tuple[str, str]]:
    """Decode ``ip -json -4 address show``: every address worth offering.

    Loopback is dropped by scope rather than by name, and interfaces that only
    lead somewhere else -- containers, VPNs -- by name. Wired and wireless come
    first, which is where a phone on the same network will be.
    """
    try:
        interfaces = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    found = []
    for interface in interfaces if isinstance(interfaces, list) else []:
        name = str(interface.get("ifname") or "")
        if not name or name.startswith(_IGNORED):
            continue
        for info in interface.get("addr_info") or []:
            if info.get("family") != "inet" or info.get("scope") != "global":
                continue
            local = info.get("local")
            if local:
                found.append((name, str(local)))

    def preference(item: tuple[str, str]) -> int:
        name = item[0]
        if name.startswith(("eth", "en")):
            return 0
        if name.startswith(("wlan", "wl")):
            return 1
        return 2

    return sorted(found, key=preference)


def outbound_address() -> str | None:
    """The address the machine would send from to reach the rest of the world.

    A UDP "connection" sends nothing; it only makes the kernel pick a route and
    a source address. On a network with no route out -- a Pi running its own
    hotspot -- there is no answer, which is correct: there is no way out.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # A documentation address: routable in principle, never in use.
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith(("0.", "127.")) else address


def find(
    port: int,
    run: Callable[[], str] | None = None,
    hostname: str | None = None,
    outbound: Callable[[], str | None] | None = None,
) -> Reach:
    """How to reach this display right now.

    Everything that asks the operating system is injectable, so this can be
    tested on any machine -- including the ones without ``ip``, which is most
    machines that are not Linux.
    """
    listed = parse_ip_json((run or _run_ip)())
    primary = (outbound or outbound_address)()
    if not listed and primary:
        listed = [("", primary)]
    return Reach(
        hostname=hostname if hostname is not None else socket.gethostname().split(".")[0],
        addresses=listed,
        port=port,
        primary_address=primary,
    )


def _run_ip() -> str:
    try:
        result = subprocess.run(
            ["ip", "-json", "-4", "address", "show"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def summary(reach: Any, wifi: str = "") -> str:
    """One line on how this display is on the network, for a corner of its screen.

    "megalink-a199 · 192.168.1.23 on Wi-Fi RangeNet · settings http://…" --
    everything someone setting up a range wants to read off a screen without
    walking round to find a laptop.
    """
    url = reach.url() if reach is not None else None
    if not url:
        return ""
    name = next((n for n, a in reach.addresses if a == reach.address), "")
    if name.startswith(("eth", "en")):
        how = " on a network cable"
    elif name.startswith(("wlan", "wl")):
        how = f" on Wi-Fi {wifi}" if wifi else " on Wi-Fi"
    else:
        how = ""
    parts = [reach.hostname, f"{reach.address}{how}", f"settings {url}"]
    return " · ".join(part for part in parts if part)


class FirstAddress:
    """When to show the network details: for a few seconds once there is an address.

    From when the display first has one, rather than from when it started: a
    Pi that takes twenty seconds to join its Wi-Fi would otherwise spend its
    ten seconds saying it had no network, and then say nothing. Given up if no
    address comes at all -- the display then has other things to say about
    that -- and never shown again once shown.
    """

    def __init__(
        self,
        seconds: float = 10.0,
        within: float = 180.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.seconds = seconds
        self.within = within
        self._clock = clock
        self._started = clock()
        self._until: float | None = None
        self.done = False

    def text(self, reach: Any, wifi: str = "") -> str:
        """The line to show now, or empty."""
        if self.done:
            return ""
        now = self._clock()
        line = summary(reach, wifi)
        if self._until is None:
            if not line:
                if now - self._started > self.within:
                    self.done = True
                return ""
            self._until = now + self.seconds
        if now >= self._until:
            self.done = True
            return ""
        return line


def describe(reach: Any) -> str:
    """A line for the journal."""
    url = reach.url()
    return f"configuration page at {url}" if url else "no network address yet"
