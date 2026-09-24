"""The configuration page a display serves for itself.

Point a phone at ``http://<display>:8080/`` and you can change which club, range
and firing point it is showing, without a keyboard and without an SSH session.
Changes go through the same file the display watches, so the screen follows
within a couple of seconds.

The API is small and deliberately dull:

===============================  ======================================
``GET  /api/status``             what this display is showing right now
``GET  /api/config``             the stored configuration
``PUT  /api/config``             merge a partial update and adopt it
``GET  /api/hosts``              clubs currently streaming
``GET  /api/ranges?host=``       that club's live ranges
``GET  /api/lanes?host=&range=`` the firing points on a range
``POST /api/identify``           make this screen announce itself
``GET  /healthz``                liveness, for a watchdog
===============================  ======================================

Writes require the ``X-Megalink-Token`` header when a token is configured. With
no token set the API is open, which suits a closed range network -- and is why
there are no permissive CORS headers here: a fleet dashboard talks to this from
its own server rather than from a browser page.
"""

from __future__ import annotations

import hmac
import html
import threading
import time
from typing import Any

from . import address, network, qr
from .client import MegalinkError
from .config import LOGO_SUFFIXES, ConfigError, find_logo, logo_path, write_durably
from .controller import Controller
from .fleet import PAGE as FLEET_PAGE
from .fleet import Routes as FleetRoutes
from .httpbase import DRAIN_LIMIT, BodyTooLarge, JSONHandler, Server

#: Discovery answers are cached for this long. The live host list changes on the
#: order of minutes, and a dropdown should not put a Pi Zero on the network for
#: every keystroke.
CACHE_SECONDS = 20.0

#: A club badge is small. This is generous and still bounds what a display will
#: read into memory.
MAX_LOGO_BYTES = 4 * 1024 * 1024


def _image_kind(raw: bytes) -> str | None:
    """The suffix for an image Tk can display, from its leading bytes."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


class _Cache:
    """A tiny time-based cache, so browsing the dropdowns stays cheap."""

    def __init__(self, seconds: float = CACHE_SECONDS) -> None:
        self._seconds = seconds
        self._entries: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, produce: Any) -> Any:
        now = time.monotonic()
        with self._lock:
            found = self._entries.get(key)
            if found is not None and now - found[0] < self._seconds:
                return found[1]
        value = produce()
        with self._lock:
            self._entries[key] = (now, value)
        return value


def _peer_count(fleet: Any) -> int:
    """How many displays this one can currently see, itself included."""
    return len(fleet.listener.displays()) if fleet is not None else 0


def make_handler(
    controller: Controller,
    cache: _Cache | None = None,
    listener: Any = None,
    find_reach: Any = None,
    read_network: Any = None,
) -> type[JSONHandler]:
    """Build a request handler bound to one display.

    Given a beacon ``listener``, the display also serves the fleet dashboard at
    ``/fleet``: every display on the range hears every other one already, so
    each is capable of managing the lot and there is no separate service to run.
    """
    shared = cache if cache is not None else _Cache()
    fleet = FleetRoutes(listener, controller.config.web.token) if listener is not None else None

    class Handler(JSONHandler):
        page = PAGE

        # -- authorisation -------------------------------------------------

        def _authorise(self) -> None:
            token = controller.config.web.token
            if not token:
                return
            supplied = self.headers.get("X-Megalink-Token") or self.query.get("token") or ""
            if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
                raise PermissionError("a valid X-Megalink-Token is required")

        # -- discovery -----------------------------------------------------

        def _hosts(self) -> Any:
            def produce() -> Any:
                active = controller.client.active()
                return [
                    {
                        "host": host,
                        "name": entries[0].host_name if entries else host,
                        "ranges": [
                            {"key": r.key, "name": r.name, "event": r.event} for r in entries
                        ],
                    }
                    for host, entries in sorted(active.items())
                ]

            return shared.get("hosts", produce)

        def _ranges(self, host: str) -> Any:
            def produce() -> Any:
                return [
                    {"key": r.key, "name": r.name, "protocol": r.protocol, "event": r.event}
                    for r in controller.client.ranges(host)
                ]

            return shared.get(f"ranges:{host}", produce)

        def _lanes(self, host: str, range_name: str) -> Any:
            def produce() -> Any:
                source = controller.client.resolve(host, range_name)
                return controller.client.source_lanes(source)

            return shared.get(f"lanes:{host}:{range_name}", produce)

        # -- the logo ------------------------------------------------------

        def _send_file(self, path: Any) -> None:
            body = path.read_bytes()
            self._send(
                200, body, LOGO_SUFFIXES.get(path.suffix.lower(), "application/octet-stream")
            )

        def _wifi_status(self) -> dict[str, Any]:
            """What the hotspot fallback is doing, for the settings page.

            No password in it -- not the hotspot's, which whoever is reading
            this has already used, and never one being joined.
            """
            status = (read_network or network.read_status)()
            if not isinstance(status, dict):
                return {"available": False}
            return {
                "available": True,
                "mode": status.get("mode", ""),
                "joining": status.get("joining", ""),
                "error": status.get("error", ""),
                "networks": status.get("networks") or [],
                "hotspot": (status.get("hotspot") or {}).get("ssid", ""),
                "country": status.get("country", ""),
            }

        def _join_wifi(self, payload: Any) -> dict[str, Any]:
            """Leave a network for the hotspot service to join.

            Checked here, where the person who typed it can be told, rather than
            left to fail half a minute later on a machine they have just been
            disconnected from.
            """
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            ssid = str(payload.get("ssid") or "")
            password = str(payload.get("password") or "")
            if not ssid.strip() or len(ssid.encode("utf-8")) > 32:
                raise ValueError("a Wi-Fi network name is 1 to 32 characters")
            is_raw_key = len(password) == 64 and all(
                c in "0123456789abcdefABCDEF" for c in password
            )
            if password and not is_raw_key and not 8 <= len(password) <= 63:
                raise ValueError(
                    "a Wi-Fi password is 8 to 63 characters, or none for an open network"
                )
            country = str(payload.get("country") or "").strip().upper()
            if country and not (len(country) == 2 and country.isalpha()):
                raise ValueError("a Wi-Fi country is two letters, such as GB, US, NO or CA")
            network.write_request(
                ssid, password, controller.path.parent / "wifi-request.json", country=country
            )
            return {"queued": True, "ssid": ssid, "country": country}

        def _store_logo(self) -> Any:
            """Save an uploaded picture beside the configuration.

            PNG and GIF only, because those are what Tk can display without a
            third-party imaging library, and this package has no dependencies.
            The format is taken from the bytes rather than from the request:
            a content type is a claim, and the file has to be one Tk can read.
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise ValueError("bad Content-Length") from exc
            if length <= 0:
                raise ValueError("no image was sent")
            if length > MAX_LOGO_BYTES:
                self._drain(min(length, DRAIN_LIMIT))
                raise BodyTooLarge(
                    f"the image is too large ({length} bytes; the limit is {MAX_LOGO_BYTES})"
                )
            raw = self.rfile.read(length)
            suffix = _image_kind(raw)
            if suffix is None:
                raise ValueError("only PNG and GIF images can be shown")

            target = logo_path(controller.path, suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            # The new one goes down first and the other format is removed only
            # once it is safely on disk. The other way round, a power cut in
            # between would leave no badge at all.
            write_durably(target, raw, prefix=".logo-")
            for other in LOGO_SUFFIXES:
                if other != suffix:
                    stale = logo_path(controller.path, other)
                    if stale.is_file():
                        stale.unlink()
            return {"stored": target.name, "bytes": len(raw)}

        # -- routing -------------------------------------------------------

        def _fleet(self, routes: FleetRoutes, method: str, path: str) -> tuple[int, Any] | None:
            """The dashboard, mounted under ``/fleet``.

            Changing other displays needs the same token as changing this one:
            a display is no less worth protecting because the request arrived
            by way of its neighbour.
            """
            if path == "/fleet":
                if method not in ("GET", "HEAD"):
                    return 405, {"error": "method not allowed"}
                self.send_html(200, FLEET_PAGE)
                return None
            if path == "/api/fleet/displays":
                return routes.displays()
            if path == "/api/fleet/apply" and method == "POST":
                self._authorise()
                return routes.apply(self.read_json())
            if path == "/api/fleet/identify" and method == "POST":
                self._authorise()
                return routes.identify(self.read_json())
            return None

        def route(self, method: str) -> tuple[int, Any] | None:
            path = self.route_path

            if fleet is not None:
                answer = self._fleet(fleet, method, path)
                if answer is not None:
                    return answer

            if path == "/setup" and method in ("GET", "HEAD"):
                # What a browser-mode display shows while it has nothing else to:
                # the same instructions as the window's set-up screen. The
                # settings form itself would be no use on a screen with no
                # keyboard in front of it.
                bound = int(self.server.server_address[1])
                self.send_html(
                    200, setup_page(controller, find_reach, port=bound, read_network=read_network)
                )
                return None

            if path == "/healthz":
                return 200, {"ok": True, "name": controller.config.name}

            if path == "/api/status":
                return 200, controller.status()

            if path == "/api/peers":
                # Whether this display can see the others, whether or not the
                # dashboard is being served. Useful on its own for diagnosis.
                return 200, {"fleet": fleet is not None, "count": _peer_count(fleet)}

            if path == "/api/config":
                if method in ("GET", "HEAD"):
                    return 200, controller.config.public_dict()
                if method in ("PUT", "POST"):
                    self._authorise()
                    patch = self.read_json()
                    try:
                        updated = controller.config.merged(patch)
                    except ConfigError as exc:
                        raise ValueError(str(exc)) from exc
                    controller.write(updated)
                    return 200, {
                        "config": controller.config.public_dict(),
                        "status": controller.status(),
                    }
                return 405, {"error": "method not allowed"}

            if path == "/api/wifi":
                if method in ("GET", "HEAD"):
                    return 200, self._wifi_status()
                if method == "POST":
                    self._authorise()
                    return 200, self._join_wifi(self.read_json())
                return 405, {"error": "method not allowed"}

            if path == "/api/logo":
                if method in ("GET", "HEAD"):
                    found = find_logo(controller.config, controller.path)
                    if found is None:
                        return 404, {"error": "no logo has been uploaded"}
                    self._send_file(found)
                    return None
                if method == "PUT":
                    self._authorise()
                    return 200, self._store_logo()
                if method == "DELETE":
                    self._authorise()
                    removed = []
                    for suffix in LOGO_SUFFIXES:
                        candidate = logo_path(controller.path, suffix)
                        if candidate.is_file():
                            candidate.unlink()
                            removed.append(candidate.name)
                    if controller.config.display.logo:
                        controller.write(controller.config.merged({"display": {"logo": ""}}))
                    return 200, {"removed": removed}
                return 405, {"error": "method not allowed"}

            if path == "/api/identify" and method == "POST":
                self._authorise()
                controller.identify()
                return 200, {"identifying": True}

            if path == "/api/hosts":
                try:
                    return 200, {"hosts": self._hosts()}
                except MegalinkError as exc:
                    return 502, {"error": str(exc)}

            if path == "/api/ranges":
                host = self.query.get("host") or controller.config.host
                if not host:
                    raise ValueError("a host is required")
                try:
                    return 200, {"host": host, "ranges": self._ranges(host)}
                except MegalinkError as exc:
                    return 502, {"error": str(exc)}

            if path == "/api/lanes":
                host = self.query.get("host") or controller.config.host
                range_name = self.query.get("range") or controller.config.range
                if not host:
                    raise ValueError("a host is required")
                try:
                    return 200, {"lanes": self._lanes(host, range_name)}
                except MegalinkError as exc:
                    return 502, {"error": str(exc)}

            return None

    return Handler


def setup_page(
    controller: Any,
    find_reach: Any = None,
    port: int | None = None,
    read_network: Any = None,
) -> str:
    """The set-up screen, as a page for a display running a browser.

    Rebuilt on every request and told to reload itself, so it follows the
    address as it changes -- and once the display is configured, the browser
    is sent to the scores instead, and this page is simply never asked for.
    """
    # The port actually being listened on, which is the one that answers. The
    # configured one can be 0, meaning "whatever the system gives".
    if port is None:
        port = controller.config.web.port if controller.config.web.enabled else 0
    reach = (find_reach or address.find)(port)
    url = reach.url() if port else None
    name = html.escape(controller.config.name or reach.hostname)
    status = (read_network or network.read_status)() or {}
    spot = status.get("hotspot") if status.get("mode") == "hotspot" else None
    if isinstance(spot, dict) and spot.get("ssid") and port:
        ssid, password = str(spot["ssid"]), str(spot.get("password") or "")
        there = f"http://{spot.get('address') or network.HOTSPOT_ADDRESS}{'' if port == 80 else f':{port}'}/"
        body = (
            '<div class="row">'
            f'<div class="step"><div class="code">{qr.svg(qr.wifi(ssid, password))}</div>'
            "<p class=lead>1. Join its Wi-Fi</p>"
            f"<p class=url>{html.escape(ssid)}</p>"
            + (f"<p class=muted>password {html.escape(password)}</p>" if password else "")
            + "</div>"
            f'<div class="step"><div class="code">{qr.svg(there)}</div>'
            "<p class=lead>2. Then open</p>"
            f"<p class=url>{html.escape(there)}</p></div></div>"
            "<p class=muted>Your phone may say there is no internet; stay connected anyway.</p>"
        )
        return SETUP_PAGE.replace("{{BODY}}", body)
    if url is None:
        remaining = network.seconds_to_hotspot(status)

        def countdown(seconds: int) -> str:
            # Counted down by the page's script between reloads, which are only
            # every few seconds.
            return (
                f'<span data-countdown="{remaining or 0:.1f}">'
                f"{html.escape(network.seconds_phrase(seconds))}</span>"
            )

        body = (
            "<h2>Waiting for a network…</h2>"
            f"<p>{network.waiting_note(status, phrase=countdown)}</p>"
            f"<p class=muted>{html.escape(network.CABLE_NOTE)}</p>" + COUNTDOWN_SCRIPT
        )
    else:
        local = reach.local_url()
        body = (
            f'<div class="row"><div class="code">{qr.svg(url)}</div><div>'
            "<p class=lead>Scan with your phone, or open</p>"
            f"<p class=url>{html.escape(url)}</p>"
            + (f"<p class=muted>or {html.escape(local)}</p>" if local else "")
            + "<p class=muted>Your phone must be on the same network.<br>"
            f"This display is called {name}.</p></div></div>"
        )
    return SETUP_PAGE.replace("{{BODY}}", body)


#: Counts the hotspot countdown down second by second, and reloads the page at
#: the end of it to say what happened instead.
COUNTDOWN_SCRIPT = """<script>
(function () {
  var el = document.querySelector("[data-countdown]");
  if (!el) return;
  var end = Date.now() + parseFloat(el.dataset.countdown) * 1000;
  setInterval(function () {
    var left = Math.ceil((end - Date.now()) / 1000);
    if (left < 1) { location.reload(); return; }
    el.textContent = left + (left === 1 ? " second" : " seconds");
  }, 250);
})();
</script>"""


SETUP_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set up this display</title>
<style>
html,body{margin:0;height:100%;background:#2b2b2b;color:#f2f2f2;
  font-family:"DejaVu Sans",system-ui,sans-serif}
main{min-height:100%;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:2.5vh;padding:4vh 4vw;box-sizing:border-box}
h1{font-size:8vh;margin:0}
h2{font-size:5vh;margin:0}
.row{display:flex;align-items:center;gap:4vw}
.code{background:#fff;width:42vh;height:42vh;flex:none}
.code svg{width:100%;height:100%;display:block}
.step{display:flex;flex-direction:column;align-items:center;text-align:center}
.step .code{width:34vh;height:34vh;margin-bottom:2vh}
p{margin:.6vh 0;font-size:2.4vh}
.lead{font-size:3.6vh;font-weight:bold}
.url{font-size:3.4vh;font-weight:bold;color:#4aa3df}
.muted{color:#9a9a9a}
/* The hidden attribute loses to any display rule of the page's own, which
   left fields showing that the script had hidden. */
[hidden]{display:none!important}
</style></head><body><main>
<h1>Set up this display</h1>
{{BODY}}
</main></body></html>
"""


class ConfigServer:
    """Serves the configuration page for one display, in a background thread."""

    def __init__(
        self,
        controller: Controller,
        listener: Any = None,
        find_reach: Any = None,
        read_network: Any = None,
    ) -> None:
        self.controller = controller
        settings = controller.config.web
        #: The beacon listener behind ``/fleet``, if this display serves it.
        self.listener = listener
        self._server = Server(
            (settings.bind, settings.port),
            make_handler(
                controller, listener=listener, find_reach=find_reach, read_network=read_network
            ),
        )
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> ConfigServer:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="megalink-web", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> ConfigServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Megalink display</title>
<style>
:root{color-scheme:dark;--bg:#1d1f21;--panel:#2b2d30;--line:#3a3d41;--text:#f2f2f2;
--muted:#9aa0a6;--accent:#4aa3df;--good:#2fbf4f;--warn:#e0a02f;--bad:#e03a2f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px}
main{max-width:34rem;margin:0 auto}
h1{font-size:1.15rem;margin:0 0 .25rem}
.sub{color:var(--muted);font-size:.85rem;margin-bottom:1rem}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:14px;margin-bottom:14px}
h2{font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);
margin:0 0 .7rem}
label{display:block;margin-bottom:.8rem}
label span{display:block;font-size:.78rem;color:var(--muted);margin-bottom:.25rem}
select,input{width:100%;padding:.6rem;background:#1a1c1e;color:var(--text);
border:1px solid var(--line);border-radius:7px;font-size:1rem}
.row{display:flex;gap:10px}.row>*{flex:1}
button{width:100%;padding:.75rem;border:0;border-radius:7px;background:var(--accent);
color:#0d1b24;font-size:1rem;font-weight:600;cursor:pointer}
button.ghost{background:#3a3d41;color:var(--text)}
button+button{margin-top:8px}
dl{display:grid;grid-template-columns:auto 1fr;gap:.3rem .8rem;margin:0;font-size:.92rem}
dt{color:var(--muted)}dd{margin:0;text-align:right}
.big{font-size:1.6rem;font-weight:700;color:var(--good)}
.note{margin-top:.7rem;font-size:.85rem;min-height:1.2rem}
.ok{color:var(--good)}.err{color:var(--bad)}.stale{color:var(--warn)}
/* The hidden attribute loses to any display rule of the page's own, which
   left fields showing that the script had hidden. */
[hidden]{display:none!important}
</style></head><body><main>
<h1 id="name">Megalink display</h1>
<div class="sub" id="showing">loading…</div>
<div class="sub" id="fleetlink" hidden></div>

<section><h2>Now showing</h2>
<dl>
<dt>Shooter</dt><dd id="shooter">—</dd>
<dt>Total</dt><dd class="big" id="total">—</dd>
<dt>Shots</dt><dd id="shots">—</dd>
<dt>Feed</dt><dd id="feed">—</dd>
</dl></section>

<section><h2>What to display</h2>
<label><span>Club</span><select id="host"></select></label>
<label><span>Range</span><select id="range"></select></label>
<label><span>Firing point</span><select id="lane"></select></label>
<button id="save">Save</button>
<button class="ghost" id="identify">Identify this screen</button>
<div class="note" id="note"></div>
</section>

<section><h2>When nobody is shooting</h2>
<label><span>Message</span><input id="idle" placeholder="POSITION NOT IN USE"></label>
<label><span>Logo (PNG or GIF, shown instead of the message)</span>
<input id="logofile" type="file" accept="image/png,image/gif"></label>
<img id="logopreview" alt="" style="max-width:100%;max-height:9rem;display:none;
margin:.2rem 0 .8rem;background:#111;border:1px solid var(--line);border-radius:7px">
<button id="savelogo">Upload logo</button>
<button class="ghost" id="dellogo">Remove logo</button>
<div class="note" id="logonote"></div>
</section>

<section id="wifisec" hidden><h2>Wi-Fi</h2>
<div class="note" id="wifistate"></div>
<label><span>Network</span><select id="wifilist"></select></label>
<label id="wifiother" hidden><span>Name</span><input id="wifissid" autocomplete="off"></label>
<label><span>Password</span><input id="wifipass" type="password" autocomplete="off"
  placeholder="leave empty for an open network"></label>
<label><span>Country</span><input id="wificountry" maxlength="2" autocomplete="off"
  placeholder="two letters: GB, US, NO, CA…" style="text-transform:uppercase"></label>
<button id="wifijoin">Join</button>
<div class="note" id="wifinote"></div>
</section>

<section><h2>This screen</h2>
<label><span>Name</span><input id="dname" placeholder="hostname"></label>
<div class="row">
<label><span>Mode</span><select id="mode">
<option value="gui">Window</option><option value="terminal">Console</option>
<option value="browser">Web browser</option></select></label>
<label><span>Redraw (s)</span><input id="interval" type="number" step="0.1" min="0.1" max="60"></label>
</div>
<label><span>Second screen</span>
<input id="lane2" placeholder="firing point for the second HDMI (optional)"></label>
<label id="urlrow" hidden><span>Address</span>
<input id="url" placeholder="leave empty for Megalink's own page"></label>
<div class="note" id="modenote"></div>
<button class="ghost" id="save2">Save screen settings</button>
</section>
</main>
<script>
const $ = id => document.getElementById(id);
let cfg = null, hosts = [];

const say = (text, cls="") => { const n = $("note"); n.textContent = text; n.className = "note " + cls; };

// This display may require a token. Take it from the link that opened the page,
// remember it, and send it on every call -- a display with a token set would
// otherwise refuse its own configuration page.
const TOKEN_KEY = "megalink-token";
function token() {
  const fromUrl = new URLSearchParams(location.search).get("token");
  if (fromUrl) { try { localStorage.setItem(TOKEN_KEY, fromUrl); } catch (e) {} return fromUrl; }
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}
function authed(opts={}) {
  const secret = token();
  if (secret) opts.headers = Object.assign({}, opts.headers, {"X-Megalink-Token": secret});
  return opts;
}

// Every display can serve the dashboard for the whole range, so point at it
// from here rather than making anyone remember which machine to open.
// The address box only means anything in browser mode.
function showMode() {
  const browser = $("mode").value === "browser";
  $("urlrow").hidden = !browser;
  $("modenote").textContent = browser
    ? "Shows Megalink's own page in a full-screen browser. Needs chromium installed."
    : "";
}

// The display's Wi-Fi, when its hotspot fallback is running. Joining a network
// from the hotspot disconnects the phone doing it, so the page says what will
// happen before it happens.
async function showWifi() {
  let w;
  try { w = await api("/api/wifi"); } catch (e) { return; }
  if (!w.available) return;
  $("wifisec").hidden = false;
  const state = {
    client: "Connected.", hotspot: "No network: this display is running its own Wi-Fi, " + w.hotspot + ".",
    retrying: "Looking for a known network…", joining: "Joining " + w.joining + "…",
    waiting: "Starting up…",
  }[w.mode] || "";
  $("wifistate").textContent = state + (w.error ? " " + w.error : "");
  const list = $("wifilist"), keep = list.value;
  list.innerHTML = "";
  for (const n of w.networks) {
    const o = document.createElement("option");
    o.value = n.ssid; o.textContent = n.ssid + (n.secure ? "" : "  (open)");
    list.appendChild(o);
  }
  const other = document.createElement("option");
  other.value = ""; other.textContent = "Another network…";
  list.appendChild(other);
  if (keep) list.value = keep;
  $("wifiother").hidden = list.value !== "";
  // Radio rules differ by country, and a Pi with none set keeps to the ones
  // that are safe everywhere, which rules out much of 5 GHz.
  if (w.country && !$("wificountry").value) $("wificountry").value = w.country;
}

async function showFleet() {
  try {
    const peers = await api("/api/peers");
    if (!peers.fleet) return;
    const el = $("fleetlink");
    el.innerHTML = '<a href="/fleet">Manage all ' + peers.count +
      (peers.count === 1 ? " display" : " displays") + " on this range \u2192</a>";
    el.hidden = false;
  } catch (e) { /* an older display, or the beacon is off */ }
}

async function api(path, opts={}) {
  const r = await fetch(path, authed(opts));
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}

function fill(select, values, chosen) {
  select.innerHTML = "";
  for (const v of values) {
    const o = document.createElement("option");
    o.value = v.value; o.textContent = v.label;
    if (v.value === chosen) o.selected = true;
    select.appendChild(o);
  }
}

async function loadHosts() {
  try {
    hosts = (await api("/api/hosts")).hosts;
  } catch (e) { say("could not reach Megalink Live: " + e.message, "err"); return; }
  fill($("host"), hosts.map(h => ({value: h.host, label: h.name || h.host})), cfg.host);
  if (!hosts.some(h => h.host === cfg.host) && cfg.host) {
    const o = document.createElement("option");
    o.value = cfg.host; o.textContent = cfg.host + " (not streaming)"; o.selected = true;
    $("host").appendChild(o);
  }
  await loadRanges();
}

async function loadRanges() {
  const host = $("host").value;
  if (!host) return;
  let ranges = [];
  try { ranges = (await api("/api/ranges?host=" + encodeURIComponent(host))).ranges; }
  catch (e) { say(e.message, "err"); }
  fill($("range"), ranges.map(r => ({value: r.key, label: r.name || r.key})), cfg.range);
  await loadLanes();
}

async function loadLanes() {
  const host = $("host").value, range = $("range").value;
  if (!host) return;
  let lanes = [];
  try {
    lanes = (await api(`/api/lanes?host=${encodeURIComponent(host)}&range=${encodeURIComponent(range)}`)).lanes;
  } catch (e) { say(e.message, "err"); }
  if (!lanes.length) lanes = cfg.lane ? [cfg.lane] : [];
  fill($("lane"), lanes.map(l => ({value: l, label: l})), cfg.lane);
}

async function refresh() {
  let s;
  try { s = await api("/api/status"); } catch { return; }
  $("name").textContent = s.name;
  $("showing").textContent = s.error ? s.error : (s.host_name ? s.host_name + " · " + s.range_name : s.showing);
  $("shooter").textContent = s.shooter || "—";
  $("total").textContent = s.total || "—";
  $("shots").textContent = s.shots;
  const feed = $("feed");
  if (s.error) { feed.textContent = "error"; feed.className = "err"; }
  else if (!s.connected) { feed.textContent = "connecting"; feed.className = "stale"; }
  else if (s.age !== null && s.age < 5) { feed.textContent = "live"; feed.className = "ok"; }
  else { feed.textContent = Math.round(s.age) + "s ago"; feed.className = "stale"; }
}

async function save(patch, button) {
  button.disabled = true;
  try {
    await api("/api/config", {method: "PUT", headers: {"Content-Type": "application/json"},
                              body: JSON.stringify(patch)});
    cfg = await api("/api/config");
    say("saved", "ok");
    refresh();
  } catch (e) { say(e.message, "err"); }
  button.disabled = false;
}

$("host").onchange = loadRanges;
$("range").onchange = loadLanes;
$("save").onclick = () => save({host: $("host").value, range: $("range").value, lane: $("lane").value}, $("save"));
$("save2").onclick = () => save({
  beacon: {name: $("dname").value},
  display: {
    mode: $("mode").value,
    lane2: $("lane2").value.trim(),
    url: $("url").value.trim(),
    interval: parseFloat($("interval").value) || 0.5,
    idle_text: $("idle").value,
  },
}, $("save2"));

const logoNote = (t, cls="") => { $("logonote").textContent = t; $("logonote").className = "note " + cls; };

function showLogo() {
  fetch("/api/logo", {cache: "no-store"}).then(r => {
    const img = $("logopreview");
    if (!r.ok) { img.style.display = "none"; return; }
    return r.blob().then(b => { img.src = URL.createObjectURL(b); img.style.display = "block"; });
  }).catch(() => {});
}

$("logofile").onchange = () => {
  const f = $("logofile").files[0];
  if (!f) return;
  const img = $("logopreview");
  img.src = URL.createObjectURL(f);
  img.style.display = "block";
  logoNote(`${f.name} ready to upload`);
};

$("savelogo").onclick = async () => {
  const f = $("logofile").files[0];
  if (!f) return logoNote("choose a PNG or GIF first", "err");
  $("savelogo").disabled = true;
  try {
    const r = await fetch("/api/logo", authed({method: "PUT", body: f}));
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(b.error || r.statusText);
    logoNote(`uploaded ${b.stored} (${Math.round(b.bytes / 1024)} kB)`, "ok");
    showLogo();
  } catch (e) { logoNote(e.message, "err"); }
  $("savelogo").disabled = false;
};

$("dellogo").onclick = async () => {
  try {
    const r = await fetch("/api/logo", authed({method: "DELETE"}));
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    $("logopreview").style.display = "none";
    $("logofile").value = "";
    logoNote("removed", "ok");
  } catch (e) { logoNote(e.message, "err"); }
};
$("identify").onclick = async () => {
  try {
    await api("/api/identify", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: "{}"});
    say("look at the screen", "ok");
  }
  catch (e) { say(e.message, "err"); }
};

(async () => {
  cfg = await api("/api/config");
  $("dname").value = cfg.beacon.name || "";
  $("mode").value = cfg.display.mode;
  $("url").value = cfg.display.url || "";
  $("lane2").value = cfg.display.lane2 || "";
  showMode();
  $("interval").value = cfg.display.interval;
  $("idle").value = cfg.display.idle_text || "";
  showLogo();
  showFleet();
  showWifi();
  $("wifilist").onchange = () => { $("wifiother").hidden = $("wifilist").value !== ""; };
  $("wifijoin").onclick = async () => {
    const ssid = $("wifilist").value || $("wifissid").value.trim();
    if (!ssid) { $("wifinote").textContent = "Choose a network, or type its name."; return; }
    try {
      await api("/api/wifi", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ssid, password: $("wifipass").value,
                              country: $("wificountry").value.trim().toUpperCase()})});
      $("wifinote").textContent = "Joining " + ssid + ". If you are connected to this display's own " +
        "Wi-Fi, your phone will drop off it now. Join " + ssid + " yourself, and the display's screen " +
        "will show its new address. If the password was wrong, its own Wi-Fi comes back in under a minute.";
      $("wifipass").value = "";
    } catch (e) { $("wifinote").textContent = e.message; }
  };
  $("mode").onchange = showMode;
  await loadHosts();
  refresh();
  setInterval(refresh, 2000);
})();
</script></body></html>
"""
