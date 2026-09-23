"""A dashboard for every display on a range.

Run this on a laptop on the same network as the displays:

    megalink fleet

It listens for the displays' announcements, lists what each one is showing, and
lets you change any or all of them at once -- which is the difference between
setting up twenty firing points and setting up one twenty times.

Bulk editing is the point. Select the displays, pick a club and range, and
either give them all the same firing point or number them consecutively, which
is how a row of positions is actually laid out.

Requests to the displays go out from this server rather than from the browser,
so the displays need no cross-origin headers and this page works over plain HTTP
on a closed network.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

from .beacon import DEFAULT_EXPIRY, Display, Listener
from .config import DEFAULT_BEACON_PORT
from .httpbase import JSONHandler, Server

#: Displays are small machines on Wi-Fi; give them room but do not hang the page.
REQUEST_TIMEOUT = 8.0


class FleetError(RuntimeError):
    """Raised when a display cannot be reached or refuses a change."""


def push_config(display: Display, patch: dict[str, Any], token: str = "") -> dict[str, Any]:
    """Send a configuration change to one display."""
    url = display.url
    if not url:
        raise FleetError(f"{display.name} did not say which port its page is on")
    body = json.dumps(patch).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Megalink-Token"] = token
    request = urllib.request.Request(f"{url}/api/config", data=body, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.load(exc).get("error", "")
        except Exception:
            detail = exc.reason if isinstance(exc.reason, str) else ""
        raise FleetError(f"{display.name}: HTTP {exc.code} {detail}".strip()) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FleetError(f"{display.name}: {exc}") from exc


def ping_identify(display: Display, token: str = "") -> None:
    """Ask one display to announce itself on screen."""
    url = display.url
    if not url:
        raise FleetError(f"{display.name} did not say which port its page is on")
    # JSON, though there is nothing in it: a display refuses any POST that a
    # web page could have forged, and an empty text body is exactly that.
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Megalink-Token"] = token
    request = urllib.request.Request(
        f"{url}/api/identify", data=b"{}", headers=headers, method="POST"
    )
    try:
        urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT).close()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        raise FleetError(f"{display.name}: {exc}") from exc


def plan_lanes(targets: list[str], start: str, step: int = 1) -> dict[str, str]:
    """Work out which firing point each selected display should show.

    A numeric start counts up from there, in the order the displays were
    selected, which matches how a row of positions is numbered. Anything
    non-numeric is applied to all of them unchanged.
    """
    try:
        first = int(start)
    except (TypeError, ValueError):
        return {identifier: str(start) for identifier in targets}
    return {identifier: str(first + index * step) for index, identifier in enumerate(targets)}


class Routes:
    """The dashboard's API, independent of which server is serving it.

    The same endpoints answer on a laptop running ``megalink fleet`` and on a
    display serving its own ``/fleet`` page, so they live here rather than
    inside a handler class.
    """

    def __init__(self, listener: Listener, token: str = "") -> None:
        self.listener = listener
        self.token = token

    def selected(self, payload: Any) -> list[Display]:
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        identifiers = payload.get("targets")
        if not isinstance(identifiers, list) or not identifiers:
            raise ValueError("no displays selected")
        found = []
        for identifier in identifiers:
            display = self.listener.find(str(identifier))
            if display is None:
                raise ValueError(f"{identifier} is not a display we can see")
            found.append(display)
        return found

    def displays(self) -> tuple[int, Any]:
        return 200, {"displays": [d.to_dict() for d in self.listener.displays()]}

    def apply(self, payload: Any) -> tuple[int, Any]:
        displays = self.selected(payload)
        patch = payload.get("patch")
        if not isinstance(patch, dict):
            raise ValueError("patch must be a JSON object")
        lanes = payload.get("lanes")
        per_display: dict[str, str] = {}
        if isinstance(lanes, dict) and lanes.get("start") not in (None, ""):
            step = lanes.get("step", 1)
            try:
                step = int(step)
            except (TypeError, ValueError):
                step = 1
            per_display = plan_lanes(
                [str(i) for i in payload["targets"]], str(lanes["start"]), step
            )

        results = []
        for display in displays:
            identifier = f"{display.address}:{display.web_port}"
            body = dict(patch)
            if identifier in per_display:
                body["lane"] = per_display[identifier]
            try:
                push_config(display, body, self.token)
            except FleetError as exc:
                results.append({"id": identifier, "ok": False, "error": str(exc)})
            else:
                results.append({"id": identifier, "ok": True, "lane": body.get("lane", "")})
        failed = [r for r in results if not r["ok"]]
        # 207: some displays may have taken the change and some not, and the
        # page needs to say which.
        return (207 if failed else 200), {"results": results}

    def identify(self, payload: Any) -> tuple[int, Any]:
        results = []
        for display in self.selected(payload):
            identifier = f"{display.address}:{display.web_port}"
            try:
                ping_identify(display, self.token)
            except FleetError as exc:
                results.append({"id": identifier, "ok": False, "error": str(exc)})
            else:
                results.append({"id": identifier, "ok": True})
        return 200, {"results": results}


def make_handler(listener: Listener, token: str = "") -> type[JSONHandler]:
    routes = Routes(listener, token)

    class Handler(JSONHandler):
        page = PAGE

        def route(self, method: str) -> tuple[int, Any] | None:
            path = self.route_path
            if path == "/healthz":
                return 200, {"ok": True, "displays": len(listener.displays())}
            # The same paths a display serves, so one page works on both.
            if path == "/api/fleet/displays":
                return routes.displays()
            if path == "/api/fleet/apply" and method == "POST":
                return routes.apply(self.read_json())
            if path == "/api/fleet/identify" and method == "POST":
                return routes.identify(self.read_json())
            return None

    return Handler


class FleetServer:
    """Serves the fleet dashboard and listens for displays."""

    def __init__(
        self,
        port: int = 8081,
        bind: str = "127.0.0.1",
        beacon_port: int = DEFAULT_BEACON_PORT,
        token: str = "",
        expiry: float = DEFAULT_EXPIRY,
    ) -> None:
        self.listener = Listener(beacon_port, expiry=expiry)
        self._server = Server((bind, port), make_handler(self.listener, token))
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> FleetServer:
        self.listener.start()
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="megalink-fleet", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self.listener.stop()

    def __enter__(self) -> FleetServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Megalink displays</title>
<style>
:root{color-scheme:dark;--bg:#1d1f21;--panel:#2b2d30;--line:#3a3d41;--text:#f2f2f2;
--muted:#9aa0a6;--accent:#4aa3df;--good:#2fbf4f;--warn:#e0a02f;--bad:#e03a2f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px}
main{max-width:70rem;margin:0 auto}
h1{font-size:1.2rem;margin:0 0 .2rem}
.sub{color:var(--muted);font-size:.85rem;margin-bottom:1rem}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
h2{font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);margin:0 0 .7rem}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th{text-align:left;font-weight:600;color:var(--muted);font-size:.72rem;
letter-spacing:.06em;text-transform:uppercase;padding:.4rem .5rem;border-bottom:1px solid var(--line)}
td{padding:.5rem;border-bottom:1px solid #333}
tr:last-child td{border-bottom:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
a{color:var(--accent)}
.pill{display:inline-block;padding:.1rem .45rem;border-radius:99px;font-size:.75rem}
.live{background:#123a1c;color:var(--good)}
.stale{background:#3d3113;color:var(--warn)}
.bad{background:#3d1614;color:var(--bad)}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
label{display:block;font-size:.78rem;color:var(--muted)}
label input,label select{display:block;margin-top:.25rem;padding:.5rem;background:#1a1c1e;
color:var(--text);border:1px solid var(--line);border-radius:7px;font-size:.95rem;min-width:9rem}
button{padding:.55rem .9rem;border:0;border-radius:7px;background:var(--accent);
color:#0d1b24;font-weight:600;cursor:pointer;font-size:.95rem}
button.ghost{background:#3a3d41;color:var(--text)}
.note{margin-top:.7rem;font-size:.86rem;min-height:1.2rem}
.ok{color:var(--good)}.err{color:var(--bad)}
.empty{color:var(--muted);padding:.6rem .2rem}
</style></head><body><main>
<h1>Megalink displays</h1>
<div class="sub" id="count">listening…</div>
<div class="sub" id="back" hidden><a href="/">Configure this display \u2192</a></div>

<section><h2>Displays</h2>
<table><thead><tr>
<th><input type="checkbox" id="all"></th><th>Name</th><th>Showing</th>
<th>Shooter</th><th class="num">Total</th><th class="num">Shots</th><th>Feed</th><th>Page</th>
</tr></thead><tbody id="rows"></tbody></table>
<div class="empty" id="empty">No displays have announced themselves yet.</div>
</section>

<section><h2>Change the selected displays</h2>
<div class="controls">
<label>Club<input id="host" placeholder="leave blank to keep"></label>
<label>Range<input id="range" placeholder="leave blank to keep"></label>
<label>Firing points<input id="lane" placeholder="e.g. 1"></label>
<label>Numbering<select id="mode">
<option value="series">count up per display</option>
<option value="same">same for all</option>
</select></label>
<button id="apply">Apply</button>
<button class="ghost" id="identify">Identify</button>
</div>
<div class="note" id="note"></div>
</section>
</main>
<script>
const $ = id => document.getElementById(id);
let selected = new Set();

const say = (t, cls="") => { $("note").textContent = t; $("note").className = "note " + cls; };

// A display may require a token. Take it from the link that opened this page,
// remember it, and send it on every call -- otherwise a fleet with a token set
// locks out the very pages meant to manage it.
const TOKEN_KEY = "megalink-token";
function token() {
  const fromUrl = new URLSearchParams(location.search).get("token");
  if (fromUrl) { try { localStorage.setItem(TOKEN_KEY, fromUrl); } catch (e) {} return fromUrl; }
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}
function setToken(value) {
  try { value ? localStorage.setItem(TOKEN_KEY, value) : localStorage.removeItem(TOKEN_KEY); }
  catch (e) {}
}

async function api(path, opts={}) {
  const secret = token();
  if (secret) opts.headers = Object.assign({}, opts.headers, {"X-Megalink-Token": secret});
  const r = await fetch(path, opts);
  const b = await r.json().catch(() => ({}));
  if (!r.ok && r.status !== 207) throw new Error(b.error || r.statusText);
  return b;
}

function feedPill(d) {
  if (d.error) return '<span class="pill bad">error</span>';
  if (!d.connected) return '<span class="pill stale">connecting</span>';
  if (d.age !== null && d.age < 5) return '<span class="pill live">live</span>';
  return '<span class="pill stale">' + Math.round(d.age || 0) + 's</span>';
}

// Served from a display as well as from a laptop; the link back only makes
// sense in the first case, so it is shown only when there is a page to go to.
(async () => {
  try {
    const here = await fetch("/api/status", {cache: "no-store"});
    if (here.ok) $("back").hidden = false;
  } catch (e) { /* the standalone dashboard has no display of its own */ }
})();

async function refresh() {
  let displays;
  try { displays = (await api("/api/fleet/displays")).displays; }
  catch (e) { $("count").textContent = "cannot reach the dashboard: " + e.message; return; }

  const seen = new Set(displays.map(d => d.id));
  selected = new Set([...selected].filter(id => seen.has(id)));

  $("count").textContent = displays.length === 1
    ? "1 display on this network" : displays.length + " displays on this network";
  $("empty").style.display = displays.length ? "none" : "block";

  $("rows").innerHTML = displays.map(d => `
    <tr>
      <td><input type="checkbox" data-id="${d.id}" ${selected.has(d.id) ? "checked" : ""}></td>
      <td>${esc(d.name)}</td>
      <td>${esc(d.error || d.showing || "—")}</td>
      <td>${esc(d.shooter || "—")}</td>
      <td class="num">${esc(d.total || "—")}</td>
      <td class="num">${d.shots ?? "—"}</td>
      <td>${feedPill(d)}</td>
      <td>${d.url ? `<a href="${d.url}" target="_blank" rel="noopener">open</a>` : "—"}</td>
    </tr>`).join("");

  for (const box of document.querySelectorAll("#rows input[type=checkbox]")) {
    box.onchange = () => {
      box.checked ? selected.add(box.dataset.id) : selected.delete(box.dataset.id);
      $("all").checked = selected.size === displays.length && displays.length > 0;
    };
  }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

$("all").onchange = () => {
  const boxes = [...document.querySelectorAll("#rows input[type=checkbox]")];
  selected = new Set($("all").checked ? boxes.map(b => b.dataset.id) : []);
  for (const b of boxes) b.checked = $("all").checked;
};

$("apply").onclick = async () => {
  if (!selected.size) return say("select at least one display", "err");
  const targets = [...selected];
  const patch = {};
  if ($("host").value.trim()) patch.host = $("host").value.trim();
  if ($("range").value.trim()) patch.range = $("range").value.trim();
  const lane = $("lane").value.trim();
  const body = {targets, patch};
  if (lane) {
    if ($("mode").value === "series") body.lanes = {start: lane, step: 1};
    else patch.lane = lane;
  }
  if (!Object.keys(patch).length && !body.lanes) return say("nothing to change", "err");
  try {
    const out = await api("/api/fleet/apply", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const bad = out.results.filter(r => !r.ok);
    say(bad.length ? `${out.results.length - bad.length} updated, ${bad.length} failed: ` +
        bad.map(b => b.error).join("; ")
      : `${out.results.length} display(s) updated`, bad.length ? "err" : "ok");
    refresh();
  } catch (e) { say(e.message, "err"); }
};

$("identify").onclick = async () => {
  if (!selected.size) return say("select at least one display", "err");
  try {
    const out = await api("/api/fleet/identify", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({targets: [...selected]})});
    const bad = out.results.filter(r => !r.ok);
    say(bad.length ? bad.map(b => b.error).join("; ") : "look at the screens", bad.length ? "err" : "ok");
  } catch (e) { say(e.message, "err"); }
};

refresh();
setInterval(refresh, 3000);
</script></body></html>
"""
