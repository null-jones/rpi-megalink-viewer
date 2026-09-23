"""Falling back to a Wi-Fi hotspot when a display cannot get on a network.

A display that boots somewhere its Wi-Fi is not -- a new range, a changed
password, a card flashed without Wi-Fi at all -- is otherwise a dead screen that
only a keyboard and some knowledge can fix. Instead, if it cannot get on a
network within thirty seconds of booting, it starts a Wi-Fi network of its own,
puts the name and password on the screen as a code a phone can scan, and serves
its settings page there so the real network can be chosen.

Raspberry Pi OS has used NetworkManager since Bookworm, and NetworkManager does
the hotspot itself -- an access point with DHCP in "shared" mode -- so this only
has to decide *when*. The complication is the radio: a Pi has one, and it can be
a hotspot or join a network but not both. So while the hotspot is up, every few
minutes it is taken down for half a minute to see whether a known network has
come back -- but only when no phone is connected, so nobody setting the display
up is cut off halfway through.

This runs as root, in its own service, because changing the network needs it.
The display itself never does: its settings page leaves a request in
``/etc/megalink`` and this picks it up. What this is doing goes the other way,
in a status file under ``/run`` that the display reads to decide what to show.

Passwords are written into NetworkManager's own connection files rather than
passed to ``nmcli`` on its command line, where for as long as the command runs
any process on the machine can read them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import write_durably

#: The connection this manages. Its own name, so it never touches anything a
#: person or Raspberry Pi Imager set up.
HOTSPOT_ID = "megalink-hotspot"
#: Where NetworkManager's shared mode puts the machine on the hotspot.
HOTSPOT_ADDRESS = "10.42.0.1"

STATUS_PATH = Path("/run/megalink/network.json")
REQUEST_PATH = Path("/etc/megalink/wifi-request.json")
SECRET_PATH = Path("/etc/megalink/hotspot.json")
CONNECTIONS = Path("/etc/NetworkManager/system-connections")

#: How long after starting to wait for a network before offering a hotspot.
BOOT_GRACE = 30.0
#: How long a network that was working may be gone before the hotspot comes up.
#: Longer than at boot: a range's router restarting takes a minute or two, and a
#: display that jumped to a hotspot every time would be offline for longer.
LOST_GRACE = 120.0
#: How often, with nobody connected, the hotspot steps aside to look for a
#: network that has come back.
RETRY_INTERVAL = 300.0
#: How long it steps aside for.
RETRY_WINDOW = 30.0
#: How long a newly entered network is given to connect before giving up on it.
JOIN_WINDOW = 45.0
#: Seconds between looks.
TICK = 5.0

#: Letters that cannot be mistaken for one another when read off a screen.
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


# -- reading nmcli ----------------------------------------------------------


def split_terse(line: str) -> list[str]:
    """Split a line of ``nmcli --terse`` output into its fields.

    Fields are separated by colons, and a colon or backslash inside a field is
    escaped with a backslash -- so a network called ``Range:1`` comes through as
    ``Range\\:1`` and splitting on colons would break it in two.
    """
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


class Device:
    """One line of ``nmcli device status``."""

    __slots__ = ("connection", "name", "state", "type")

    def __init__(self, name: str, type: str, state: str, connection: str) -> None:
        self.name = name
        self.type = type
        self.state = state
        self.connection = connection

    @property
    def connected(self) -> bool:
        # "connected", and "connected (externally)" for one set up outside NM.
        return self.state.startswith("connected")

    def __repr__(self) -> str:
        return f"Device({self.name!r}, {self.type!r}, {self.state!r}, {self.connection!r})"


def parse_devices(text: str) -> list[Device]:
    """Decode ``nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status``."""
    found = []
    for line in (text or "").splitlines():
        fields = split_terse(line)
        if len(fields) >= 4 and fields[0]:
            found.append(Device(*fields[:4]))
    return found


def parse_scan(text: str) -> list[dict[str, Any]]:
    """Decode ``nmcli -t -f SSID,SIGNAL,SECURITY device wifi list``.

    One entry per network name, at its strongest, strongest first. Hidden
    networks have no name to offer and are left out; the settings page has a
    box for typing one in.
    """
    best: dict[str, dict[str, Any]] = {}
    for line in (text or "").splitlines():
        fields = split_terse(line)
        if len(fields) < 3 or not fields[0]:
            continue
        ssid, signal, security = fields[0], fields[1], fields[2]
        try:
            strength = int(signal)
        except ValueError:
            strength = 0
        entry = {"ssid": ssid, "signal": strength, "secure": bool(security.strip("- "))}
        if ssid not in best or strength > best[ssid]["signal"]:
            best[ssid] = entry
    return sorted(best.values(), key=lambda entry: (-entry["signal"], entry["ssid"]))


# -- NetworkManager connection files ----------------------------------------


def _keyfile_value(value: str) -> str:
    """Escape a value for a NetworkManager keyfile, which is GLib's format."""
    value = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    return "\\s" + value[1:] if value.startswith(" ") else value


def keyfile(
    connection_id: str,
    ssid: str,
    password: str = "",
    *,
    hotspot: bool = False,
    interface: str = "wlan0",
    priority: int = 50,
    connection_uuid: str | None = None,
) -> str:
    """A NetworkManager connection file for a network to join, or the hotspot.

    The hotspot never connects on its own -- this service decides when -- and is
    kept to 2.4 GHz, which every phone can join and which is all a Pi Zero 2 W
    has. Protected management frames are switched off for it: the Pi's Wi-Fi
    firmware will not run an access point with them, and NetworkManager's
    default of trying them is a known reason a Pi's hotspot never appears.
    """
    lines = [
        "[connection]",
        f"id={_keyfile_value(connection_id)}",
        f"uuid={connection_uuid or uuid.uuid4()}",
        "type=wifi",
        f"interface-name={interface}",
    ]
    if hotspot:
        lines.append("autoconnect=false")
    else:
        lines += ["autoconnect=true", f"autoconnect-priority={priority}"]
    lines += [
        "",
        "[wifi]",
        f"mode={'ap' if hotspot else 'infrastructure'}",
        f"ssid={_keyfile_value(ssid)}",
    ]
    if hotspot:
        lines.append("band=bg")
    if password:
        lines += ["", "[wifi-security]", "key-mgmt=wpa-psk", f"psk={_keyfile_value(password)}"]
        if hotspot:
            lines += ["proto=rsn", "pairwise=ccmp", "group=ccmp", "pmf=1"]
    lines += ["", "[ipv4]", f"method={'shared' if hotspot else 'auto'}", ""]
    lines += ["[ipv6]", f"method={'disabled' if hotspot else 'auto'}", ""]
    return "\n".join(lines)


def profile_name(ssid: str) -> tuple[str, str]:
    """The connection id and file name for a network entered on the settings page.

    Named for this package, so a network added here can never overwrite one a
    person set up another way, and filed under a hash of the network name, since
    a network name can hold characters no file name should.
    """
    digest = hashlib.sha1(ssid.encode("utf-8")).hexdigest()[:10]
    return f"megalink {ssid}", f"megalink-wifi-{digest}.nmconnection"


def hotspot_ssid(name: str) -> str:
    """What the hotspot is called: recognisable, and within Wi-Fi's 32 bytes."""
    base = f"Megalink {name}".strip() if name else "Megalink display"
    encoded = base.encode("utf-8")[:32]
    return encoded.decode("utf-8", errors="ignore")


def new_password(choose: Callable[[str], str] = secrets.choice) -> str:
    """A hotspot password someone can read off a screen and type if they must.

    Twelve characters from an alphabet without look-alikes, in groups of four.
    """
    groups = ["".join(choose(_PASSWORD_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "-".join(groups)


def load_secret(path: Path = SECRET_PATH, name: str = "") -> dict[str, str]:
    """The hotspot's name and password, made once and then kept.

    Kept rather than made fresh each time, so the password on a sticker on the
    back of a display stays true.
    """
    try:
        data = json.loads(path.read_text("utf-8"))
        if isinstance(data, dict) and data.get("ssid") and data.get("password"):
            return {"ssid": str(data["ssid"]), "password": str(data["password"])}
    except (OSError, ValueError):
        pass
    secret = {"ssid": hotspot_ssid(name), "password": new_password()}
    write_durably(path, json.dumps(secret, indent=2).encode("utf-8"), prefix=".hotspot-")
    os.chmod(path, 0o600)
    return secret


# -- asking NetworkManager ----------------------------------------------------


def _run(args: list[str], timeout: float = 30.0) -> tuple[int, str]:  # pragma: no cover
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return result.returncode, result.stdout


class NetworkManager:
    """The few things this needs from NetworkManager, through ``nmcli``."""

    def __init__(
        self,
        run: Callable[..., tuple[int, str]] | None = None,
        connections: Path = CONNECTIONS,
        interface: str = "wlan0",
    ) -> None:
        self._run = run or _run
        self.connections = Path(connections)
        self.interface = interface

    def available(self) -> bool:
        code, out = self._run(["nmcli", "-t", "-f", "RUNNING", "general"])
        return code == 0 and out.strip() == "running"

    def devices(self) -> list[Device]:
        code, out = self._run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"]
        )
        return parse_devices(out) if code == 0 else []

    def wifi_ssid(self) -> str:
        """The network the Wi-Fi is joined to, or empty."""
        code, out = self._run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi", "list", "--rescan", "no"]
        )
        if code != 0:
            return ""
        for line in out.splitlines():
            fields = split_terse(line)
            if len(fields) >= 2 and fields[0] == "yes":
                return fields[1]
        return ""

    def hotspot_clients(self) -> int:
        """Phones joined to the hotspot now; 0 when it cannot tell.

        Counted as none when unsure, rather than as someone. The cost of being
        wrong that way is a phone dropped for half a minute; the cost of being
        wrong the other way is a display stuck on its hotspot for good.
        """
        code, out = self._run(["iw", "dev", self.interface, "station", "dump"])
        if code != 0:
            return 0
        return sum(1 for line in out.splitlines() if line.startswith("Station "))

    def scan(self) -> list[dict[str, Any]]:
        code, out = self._run(
            [
                "nmcli",
                "-t",
                "-f",
                "SSID,SIGNAL,SECURITY",
                "device",
                "wifi",
                "list",
                "--rescan",
                "yes",
            ],
            timeout=45.0,
        )
        return parse_scan(out) if code == 0 else []

    def save(self, filename: str, text: str) -> None:
        """Write a connection file where NetworkManager will read it."""
        target = self.connections / filename
        write_durably(target, text.encode("utf-8"), prefix=".megalink-")
        # NetworkManager refuses a connection file anyone else can read, and
        # says so only in its own log.
        os.chmod(target, 0o600)
        self._run(["nmcli", "connection", "reload"])

    def up(self, connection_id: str, wait: float = 40.0) -> bool:
        code, _out = self._run(
            ["nmcli", "--wait", str(int(wait)), "connection", "up", "id", connection_id],
            timeout=wait + 15,
        )
        return code == 0

    def down(self, connection_id: str) -> None:
        self._run(["nmcli", "connection", "down", "id", connection_id])


# -- deciding -----------------------------------------------------------------


class Observation:
    """What was true when the service last looked."""

    __slots__ = ("clients", "connected", "hotspot_active", "request", "wifi_ssid")

    def __init__(
        self,
        connected: bool,
        hotspot_active: bool,
        clients: int = 0,
        request: dict[str, str] | None = None,
        wifi_ssid: str = "",
    ) -> None:
        #: On a network by Wi-Fi or by cable -- the hotspot does not count.
        self.connected = connected
        self.hotspot_active = hotspot_active
        self.clients = clients
        #: A network entered on the settings page, not yet tried.
        self.request = request
        self.wifi_ssid = wifi_ssid


class Memory:
    """What the service remembers between looks."""

    __slots__ = ("error", "joining", "lost_since", "mode", "since")

    def __init__(self, mode: str = "waiting", since: float = 0.0) -> None:
        #: waiting, client, hotspot, retrying or joining.
        self.mode = mode
        self.since = since
        self.lost_since: float | None = None
        #: The network being tried, while joining.
        self.joining = ""
        #: Why the last attempt to join a network failed, for the settings page.
        self.error = ""


def observe(nm: NetworkManager, request: dict[str, str] | None) -> Observation:
    devices = nm.devices()
    hotspot = any(d.connection == HOTSPOT_ID and d.connected for d in devices)
    connected = any(
        d.connected and d.type in ("wifi", "ethernet") and d.connection != HOTSPOT_ID
        for d in devices
    )
    return Observation(
        connected=connected,
        hotspot_active=hotspot,
        clients=nm.hotspot_clients() if hotspot else 0,
        request=request,
        wifi_ssid=nm.wifi_ssid() if connected else "",
    )


def decide(now: float, seen: Observation, memory: Memory) -> list[str]:
    """What to do next. Changes ``memory`` in place and returns the actions.

    Kept free of anything that touches the network, so that every rule about
    when a display gives up on its Wi-Fi can be tested with a pretend clock.
    Actions are ``start_hotspot``, ``stop_hotspot`` and ``join``.
    """
    mode = memory.mode

    def become(new_mode: str) -> None:
        memory.mode = new_mode
        memory.since = now

    # A network entered on the settings page is tried first, whatever else is
    # going on -- that is the person in front of the display asking for it.
    if seen.request and mode != "joining":
        memory.joining = seen.request.get("ssid", "")
        memory.error = ""
        actions = ["stop_hotspot"] if seen.hotspot_active else []
        become("joining")
        return [*actions, "join"]

    if mode == "waiting":
        if seen.connected:
            become("client")
            return []
        if seen.hotspot_active:  # left up by a previous run of this service
            become("hotspot")
            return []
        if now - memory.since >= BOOT_GRACE:
            become("hotspot")
            return ["start_hotspot"]
        return []

    if mode == "client":
        if seen.connected:
            memory.lost_since = None
            return []
        if memory.lost_since is None:
            memory.lost_since = now
            return []
        if now - memory.lost_since >= LOST_GRACE:
            memory.lost_since = None
            become("hotspot")
            return ["start_hotspot"]
        return []

    if mode == "hotspot":
        if seen.connected:  # a cable plugged in, say
            become("client")
            return ["stop_hotspot"]
        if seen.clients == 0 and now - memory.since >= RETRY_INTERVAL:
            become("retrying")
            return ["stop_hotspot"]
        if not seen.hotspot_active and now - memory.since >= TICK * 2:
            # Taken down by something else, or it never came up; try again.
            memory.since = now
            return ["start_hotspot"]
        return []

    if mode == "retrying":
        if seen.connected:
            become("client")
            return []
        if now - memory.since >= RETRY_WINDOW:
            become("hotspot")
            return ["start_hotspot"]
        return []

    if mode == "joining":
        if seen.connected and (not memory.joining or seen.wifi_ssid == memory.joining):
            memory.joining = ""
            become("client")
            return []
        if now - memory.since >= JOIN_WINDOW:
            memory.error = f"Could not join {memory.joining}. Check the name and password."
            memory.joining = ""
            if seen.connected:  # still on the network it was on before
                become("client")
                return []
            become("hotspot")
            return ["start_hotspot"]
        return []

    become("waiting")  # pragma: no cover - an unknown mode
    return []


# -- the service ---------------------------------------------------------------


def read_request(path: Path = REQUEST_PATH) -> dict[str, str] | None:
    """Take a network entered on the settings page, once."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        # Taken once: a request tried twice would drop the network it had
        # just joined to try it again.
        with contextlib.suppress(OSError):
            path.unlink()
    if not isinstance(data, dict) or not str(data.get("ssid") or "").strip():
        return None
    return {"ssid": str(data["ssid"]), "password": str(data.get("password") or "")}


def write_request(ssid: str, password: str, path: Path = REQUEST_PATH) -> None:
    """Leave a network for the service to join. What the settings page calls."""
    body = json.dumps({"ssid": ssid, "password": password}).encode("utf-8")
    write_durably(path, body, prefix=".wifi-request-")
    os.chmod(path, 0o600)


def read_status(path: Path = STATUS_PATH) -> dict[str, Any] | None:
    """What the service says it is doing, or ``None`` if it is not running."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def status(
    memory: Memory, secret: dict[str, str], networks: list[dict[str, Any]]
) -> dict[str, Any]:
    """The status file's contents. The hotspot password is on the screen anyway;
    the password of a network being joined never goes in here."""
    on_hotspot = memory.mode == "hotspot"
    return {
        "mode": memory.mode,
        "hotspot": (
            {"ssid": secret["ssid"], "password": secret["password"], "address": HOTSPOT_ADDRESS}
            if on_hotspot
            else None
        ),
        "joining": memory.joining,
        "error": memory.error,
        "networks": networks,
        "updated": time.time(),
    }


def run(
    nm: NetworkManager,
    stop: Callable[[], bool],
    name: str = "",
    report: Callable[[str], None] = print,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    status_path: Path = STATUS_PATH,
    request_path: Path = REQUEST_PATH,
    secret_path: Path = SECRET_PATH,
) -> None:
    """Watch the network until told to stop."""
    while not nm.available():
        report("NetworkManager is not running; the hotspot fallback is off until it is")
        for _ in range(12):
            if stop():
                return
            sleep(TICK)

    secret = load_secret(secret_path, name)
    memory = Memory(since=clock())
    networks: list[dict[str, Any]] = []
    pending: dict[str, str] | None = None
    report(f"watching the network; hotspot {secret['ssid']!r} if it is needed")

    while not stop():
        request = read_request(request_path)
        if request:
            pending = request
        seen = observe(nm, request)
        previous = memory.mode
        for action in decide(clock(), seen, memory):
            if action == "start_hotspot":
                # Look round first: while the radio is a hotspot it cannot.
                networks = nm.scan() or networks
                nm.save(
                    f"{HOTSPOT_ID}.nmconnection",
                    keyfile(
                        HOTSPOT_ID,
                        secret["ssid"],
                        secret["password"],
                        hotspot=True,
                        interface=nm.interface,
                    ),
                )
                if not nm.up(HOTSPOT_ID, wait=20):
                    report("the hotspot would not start")
            elif action == "stop_hotspot":
                nm.down(HOTSPOT_ID)
            elif action == "join" and pending:
                connection_id, filename = profile_name(pending["ssid"])
                nm.save(
                    filename,
                    keyfile(
                        connection_id, pending["ssid"], pending["password"], interface=nm.interface
                    ),
                )
                report(f"joining {pending['ssid']!r}")
                nm.up(connection_id, wait=JOIN_WINDOW - 5)
                pending = None
        if memory.mode != previous:
            report(f"network: {previous} -> {memory.mode}")
        write_durably(
            status_path,
            json.dumps(status(memory, secret, networks)).encode("utf-8"),
            prefix=".network-",
        )
        os.chmod(status_path, 0o644)
        sleep(TICK)
