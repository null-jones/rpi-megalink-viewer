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
        """Change the selected displays.

        ``patch`` goes to all of them. ``each``, keyed by display, is merged
        over it for that display alone: how the page gives each display its
        own firing points -- worked out where the person can see them, in the
        order the displays are listed, before anything is sent.
        """
        displays = self.selected(payload)
        patch = payload.get("patch", {})
        if not isinstance(patch, dict):
            raise ValueError("patch must be a JSON object")
        each = payload.get("each") or {}
        if not isinstance(each, dict) or not all(isinstance(v, dict) for v in each.values()):
            raise ValueError("each must map displays to JSON objects")
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
            own = each.get(identifier, {})
            for key, value in own.items():
                if key in ("display", "web", "beacon") and isinstance(value, dict):
                    body[key] = {**body.get(key, {}), **value}
                else:
                    body[key] = value
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


def make_handler(listener: Listener, token: str = "", lookups: Any = None) -> type[JSONHandler]:
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
            # The clubs, ranges and firing points to choose from, as a display's
            # own page has them, so nobody types Megalink's names for them.
            if lookups is not None:
                return lookups.route(path, self.query)
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
        client: Any = None,
    ) -> None:
        from .lookups import Lookups

        self.listener = Listener(beacon_port, expiry=expiry)
        lookups = Lookups(client) if client is not None else None
        self._server = Server((bind, port), make_handler(self.listener, token, lookups))
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
<title>Range displays</title>
<style>
:root{color-scheme:dark;--bg:#1d1f21;--panel:#2b2d30;--line:#3a3d41;--text:#f2f2f2;
--muted:#9aa0a6;--accent:#4aa3df;--good:#2fbf4f;--warn:#e0a02f;--bad:#e03a2f;--field:#1a1c1e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px}
main{max-width:72rem;margin:0 auto}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:.4rem 1rem;margin-bottom:1rem}
h1{font-size:1.35rem;margin:0}
.sub{color:var(--muted);font-size:.9rem}
header a{margin-left:auto;font-size:.9rem}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
h2{font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);margin:0 0 .7rem}
a{color:var(--accent)}
input,select{padding:.5rem;background:var(--field);color:var(--text);border:1px solid var(--line);
border-radius:7px;font-size:.95rem}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px}
button{padding:.55rem .9rem;border:0;border-radius:7px;background:var(--accent);
color:#0d1b24;font-weight:600;cursor:pointer;font-size:.95rem;white-space:nowrap}
button.ghost{background:#3a3d41;color:var(--text)}
button:disabled{opacity:.45;cursor:default}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.toolbar input{flex:1 1 14rem}
.pill{display:inline-block;padding:.1rem .5rem;border-radius:99px;font-size:.75rem;white-space:nowrap}
.live{background:#123a1c;color:var(--good)}.stale{background:#3d3113;color:var(--warn)}
.bad{background:#3d1614;color:var(--bad)}
/* One row per display: a grid rather than a table, so on a phone at the range
   each display becomes a card instead of a table running off the side. */
.row{display:grid;grid-template-columns:2rem minmax(0,1.2fr) minmax(0,1fr) 15.5rem minmax(0,1.1fr) 6rem;
gap:.4rem .8rem;align-items:center;padding:.6rem .3rem;border-bottom:1px solid #34373b}
.row:last-child{border-bottom:0}
.row.head{color:var(--muted);font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;padding-top:0}
.row.picked{background:#23303a;border-radius:8px}
.name{font-weight:600}.small{color:var(--muted);font-size:.8rem}
.lanes{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.lanes input{width:4.2rem;text-align:center;font-variant-numeric:tabular-nums}
.lanes .tag{font-size:.7rem;color:var(--muted);margin-right:-3px}
.lanes button{padding:.4rem .6rem;font-size:.85rem}
.plan{color:var(--accent);font-weight:600;font-size:.85rem;white-space:nowrap}
.check{width:1.15rem;height:1.15rem}
.empty{color:var(--muted);padding:1rem .3rem}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));gap:12px}
.fields label{display:block;font-size:.8rem;color:var(--muted)}
.fields select,.fields input[type=text],.fields input:not([type]){display:block;width:100%;margin-top:.3rem}
fieldset{border:0;margin:0;padding:0;grid-column:1/-1;display:flex;flex-wrap:wrap;
gap:.2rem 1.6rem;align-items:center}
fieldset legend{width:100%}
legend{font-size:.8rem;color:var(--muted);margin-bottom:.3rem;padding:0}
.fields .choice{display:flex;align-items:center;gap:.5rem;margin:.35rem 0;color:var(--text);
font-size:.95rem;white-space:nowrap}
.choice input[type=radio]{margin:0}
.fields .choice input:not([type]){display:inline-block;width:5rem;margin:0;padding:.35rem .5rem}
.hint{color:var(--muted);font-size:.82rem;margin-top:.6rem}
.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:14px}
.note{font-size:.88rem}
.ok{color:var(--good)}.err{color:var(--bad)}
@media (max-width:900px){
  .row.head{display:none}
  .row{grid-template-columns:2rem 1fr auto;grid-template-areas:
    "check name feed" ". showing showing" ". lanes lanes" ". now now"}
  .row>.c-check{grid-area:check}.row>.c-name{grid-area:name}.row>.c-feed{grid-area:feed}
  .row>.c-showing{grid-area:showing}.row>.c-lanes{grid-area:lanes}.row>.c-now{grid-area:now}
}
/* The hidden attribute loses to any display rule of the page's own, which
   left fields showing that the script had hidden. */
[hidden]{display:none!important}
</style></head><body><main>
<header>
<h1>Range displays</h1>
<span class="sub" id="count">listening for displays…</span>
<a id="back" href="/" hidden>This display's own settings →</a>
</header>

<section>
<div class="toolbar">
<input id="filter" placeholder="Find a display by name, club or firing point" autocomplete="off">
<select id="sort" aria-label="Order">
<option value="lane">In firing-point order</option>
<option value="name">In name order</option>
</select>
<button class="ghost" id="selall">Select all</button>
<button class="ghost" id="selnone">Select none</button>
<button class="ghost" id="identify" disabled>Identify selected</button>
</div>
</section>

<section>
<div class="row head"><span></span><span>Display</span><span>Showing</span>
<span>Firing point</span><span>Now</span><span>Feed</span></div>
<div id="rows"></div>
<div class="empty" id="empty">No displays have been heard yet. Each one announces itself
every few seconds to the network it is on, so this device has to be on that network too.</div>
</section>

<section id="bulk">
<h2>Set up the selected displays</h2>
<div class="sub" id="selcount">Tick displays above, or Select all, to set several up at once.</div>
<div class="fields" style="margin-top:.8rem">
<label>Club
<select id="host"><option value="">Keep each display's club</option></select>
<input id="hosttext" placeholder="club, as Megalink names it" hidden></label>
<label>Range
<select id="range" disabled><option value="">Keep each display's range</option></select>
<input id="rangetext" placeholder="range" hidden></label>
<fieldset><legend>Firing points</legend>
<label class="choice"><input type="radio" name="lanemode" value="keep" checked> Keep them as they are</label>
<label class="choice"><input type="radio" name="lanemode" value="count"> Number them from
<input id="start" value="1" inputmode="numeric" aria-label="first firing point"></label>
<label class="choice"><input type="radio" name="lanemode" value="same"> All show
<input id="same" inputmode="numeric" aria-label="firing point for all"></label>
</fieldset>
</div>
<div class="hint">Numbered in the order they are listed above, which the preview beside each
firing point shows before anything changes. A display with two screens takes two numbers:
screen 1, then screen 2.</div>
<div class="actions">
<button id="apply" disabled>Apply to the selected displays</button>
<span class="note" id="note"></span>
</div>
</section>
</main>
<script>
const $ = id => document.getElementById(id);
let displays = [], selected = new Set(), rows = new Map(), order = "";

const say = (t, cls="") => { $("note").textContent = t; $("note").className = "note " + cls; };
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// A display may require a token. Take it from the link that opened this page,
// remember it, and send it on every call -- otherwise a fleet with a token set
// locks out the very pages meant to manage it.
const TOKEN_KEY = "megalink-token";
function token() {
  const fromUrl = new URLSearchParams(location.search).get("token");
  if (fromUrl) { try { localStorage.setItem(TOKEN_KEY, fromUrl); } catch (e) {} return fromUrl; }
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}
async function api(path, opts={}) {
  const secret = token();
  if (secret) opts.headers = Object.assign({}, opts.headers, {"X-Megalink-Token": secret});
  const r = await fetch(path, opts);
  const b = await r.json().catch(() => ({}));
  if (!r.ok && r.status !== 207) throw new Error(b.error || r.statusText);
  return b;
}
const post = (path, body) => api(path, {method: "POST",
  headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});

function feedPill(d) {
  if (!d.host || !d.lane) return '<span class="pill stale">not set up</span>';
  if (d.error) return '<span class="pill bad" title="' + esc(d.error) + '">error</span>';
  if (!d.connected) return '<span class="pill stale">connecting</span>';
  if (d.age !== null && d.age < 5) return '<span class="pill live">live</span>';
  return '<span class="pill stale">' + Math.round(d.age || 0) + 's old</span>';
}
const two = d => (d.screens || 0) >= 2 || !!d.lane2;
// Firing points compared as numbers where they are numbers: 2 before 10.
const laneKey = l => { const n = parseFloat(l); return isNaN(n) ? [1, String(l || "")] : [0, n]; };
function compare(a, b) {
  if ($("sort").value === "name") return String(a.name).localeCompare(String(b.name), undefined, {numeric: true});
  const x = laneKey(a.lane), y = laneKey(b.lane);
  return x[0] - y[0] || (x[0] ? x[1].localeCompare(y[1]) : x[1] - y[1]) ||
    String(a.name).localeCompare(String(b.name), undefined, {numeric: true});
}
function shown() {
  const q = $("filter").value.trim().toLowerCase();
  return displays.filter(d => !q || [d.name, d.showing, d.host, d.range, d.lane, d.lane2, d.shooter]
    .some(v => String(v ?? "").toLowerCase().includes(q))).sort(compare);
}

// The firing points the bulk change would give each selected display, in the
// order they are listed: what the preview shows, and what Apply sends.
function plan() {
  const mode = document.querySelector("input[name=lanemode]:checked").value;
  const out = new Map();
  if (mode === "keep") return out;
  if (mode === "same") {
    const lane = $("same").value.trim();
    if (!lane) return out;
    for (const d of shown()) if (selected.has(d.id)) out.set(d.id, two(d) ? [lane, lane] : [lane]);
    return out;
  }
  let next = parseInt($("start").value, 10);
  if (isNaN(next)) return out;
  for (const d of shown()) {
    if (!selected.has(d.id)) continue;
    out.set(d.id, two(d) ? [String(next), String(next + 1)] : [String(next)]);
    next += two(d) ? 2 : 1;
  }
  return out;
}

function makeRow(d) {
  const row = document.createElement("div");
  row.className = "row";
  row.innerHTML = `
    <span class="c-check"><input type="checkbox" class="check" aria-label="select"></span>
    <span class="c-name"><div class="name"></div><div class="small who"></div></span>
    <span class="c-showing"></span>
    <span class="c-lanes lanes">
      <input class="lane" inputmode="numeric" aria-label="firing point">
      <span class="tag second">2nd</span><input class="lane2 second" inputmode="numeric"
        aria-label="firing point on the second screen" placeholder="same">
      <button class="save" hidden>Save</button><span class="plan"></span>
    </span>
    <span class="c-now"></span>
    <span class="c-feed"></span>`;
  const box = row.querySelector(".check");
  box.onchange = () => { box.checked ? selected.add(d.id) : selected.delete(d.id); update(); };
  const lane = row.querySelector(".lane"), lane2 = row.querySelector(".lane2");
  const saveBtn = row.querySelector(".save");
  for (const input of [lane, lane2]) {
    input.oninput = () => { input.dataset.dirty = "1"; saveBtn.hidden = false; };
    input.onkeydown = e => { if (e.key === "Enter") saveBtn.click(); };
  }
  saveBtn.onclick = async () => {
    const patch = {lane: lane.value.trim()};
    if (two(row._d)) patch.display = {lane2: lane2.value.trim()};
    if (!patch.lane) return say("a display needs a firing point", "err");
    saveBtn.disabled = true;
    try {
      const out = await post("/api/fleet/apply", {targets: [d.id], each: {[d.id]: patch}});
      const bad = out.results.filter(r => !r.ok);
      if (bad.length) throw new Error(bad[0].error);
      delete lane.dataset.dirty; delete lane2.dataset.dirty; saveBtn.hidden = true;
      say(`${row._d.name} now shows firing point ${patch.lane}` +
          (patch.display && patch.display.lane2 ? ` and ${patch.display.lane2}` : ""), "ok");
      setTimeout(refresh, 400);
    } catch (e) { say(e.message, "err"); }
    saveBtn.disabled = false;
  };
  return row;
}

function fill(row, d) {
  row._d = d;
  row.classList.toggle("picked", selected.has(d.id));
  row.querySelector(".check").checked = selected.has(d.id);
  row.querySelector(".name").innerHTML = d.url
    ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.name)}</a>` : esc(d.name);
  const bits = [d.address, d.mode === "browser" ? "web browser" : d.mode === "terminal" ? "console" : "",
                two(d) ? "two screens" : ""].filter(Boolean);
  row.querySelector(".who").textContent = bits.join(" · ");
  const where = d.host_name ? [d.host_name, d.range_name] : d.host ? [d.host, d.range] : [];
  const unset = !d.host || !d.lane;
  row.querySelector(".c-showing").innerHTML = unset
    ? '<span class="small">Not set up yet</span>'
    : d.error ? `<span class="err">${esc(d.error)}</span>`
    : esc(where.filter(Boolean).join(" · "));
  const lane = row.querySelector(".lane"), lane2 = row.querySelector(".lane2");
  // Never over what somebody is typing.
  if (!lane.dataset.dirty && document.activeElement !== lane) lane.value = d.lane || "";
  if (!lane2.dataset.dirty && document.activeElement !== lane2) lane2.value = d.lane2 || "";
  for (const el of row.querySelectorAll(".second")) el.hidden = !two(d);
  const now = d.shooter ? `${esc(d.shooter)}` + (d.total ? ` · <b>${esc(d.total)}</b>` : "") +
    (d.shots ? ` <span class="small">(${d.shots} shots)</span>` : "") : '<span class="small">nobody shooting</span>';
  row.querySelector(".c-now").innerHTML = now;
  row.querySelector(".c-feed").innerHTML = feedPill(d);
}

function update() {
  const list = shown(), planned = plan(), holder = $("rows");
  for (const [id, row] of rows) if (!displays.some(d => d.id === id)) { row.remove(); rows.delete(id); }
  const typing = holder.contains(document.activeElement);
  const wanted = list.map(d => d.id).join("|");
  for (const d of list) {
    if (!rows.has(d.id)) rows.set(d.id, makeRow(d));
    const row = rows.get(d.id);
    fill(row, d);
    const p = planned.get(d.id);
    row.querySelector(".plan").textContent = p ? "→ " + p.join(" · ") : "";
  }
  // Put the rows in order, unless that would take the cursor from somebody.
  if (wanted !== order && !typing) {
    for (const row of holder.children) row.hidden = true;
    for (const d of list) { const row = rows.get(d.id); row.hidden = false; holder.appendChild(row); }
    order = wanted;
  }
  for (const [id, row] of rows) row.hidden = !list.some(d => d.id === id);
  const live = displays.filter(d => d.connected && !d.error).length;
  $("count").textContent = displays.length === 1 ? "1 display, " + (live ? "live" : "not live")
    : `${displays.length} displays, ${live} live`;
  $("empty").hidden = displays.length > 0;
  const n = [...selected].length;
  $("selcount").textContent = n ? (n === 1 ? "1 display selected." : n + " displays selected.")
    : "Tick displays above, or Select all, to set several up at once.";
  $("apply").disabled = !n;
  $("identify").disabled = !n;
  $("apply").textContent = n ? `Apply to ${n === 1 ? "1 display" : n + " displays"}` : "Apply to the selected displays";
}

async function refresh() {
  try { displays = (await api("/api/fleet/displays")).displays; }
  catch (e) { $("count").textContent = "cannot reach the dashboard: " + e.message; return; }
  const seen = new Set(displays.map(d => d.id));
  selected = new Set([...selected].filter(id => seen.has(id)));
  update();
}

// Clubs and ranges from the list Megalink publishes, as on a display's own
// page. Where that cannot be had -- no internet on the range -- they are typed.
async function loadHosts() {
  try {
    const hosts = (await api("/api/hosts")).hosts;
    for (const h of hosts) {
      const o = document.createElement("option");
      o.value = h.host; o.textContent = h.name || h.host; $("host").appendChild(o);
    }
  } catch (e) { $("host").hidden = true; $("hosttext").hidden = false; $("range").hidden = true;
                $("rangetext").hidden = false; }
}
$("host").onchange = async () => {
  const host = $("host").value, range = $("range");
  range.innerHTML = '<option value="">Keep each display&#39;s range</option>';
  range.disabled = !host;
  if (!host) return;
  try {
    const ranges = (await api("/api/ranges?host=" + encodeURIComponent(host))).ranges;
    range.innerHTML = "";
    for (const r of ranges) {
      const o = document.createElement("option");
      o.value = r.key; o.textContent = r.name || r.key; range.appendChild(o);
    }
  } catch (e) { say(e.message, "err"); }
};

$("apply").onclick = async () => {
  const targets = shown().filter(d => selected.has(d.id)).map(d => d.id);
  if (!targets.length) return say("select at least one display", "err");
  const patch = {};
  const host = $("host").hidden ? $("hosttext").value.trim() : $("host").value;
  const range = $("range").hidden ? $("rangetext").value.trim() : $("range").value;
  if (host) patch.host = host;
  if (range) patch.range = range;
  const each = {};
  for (const [id, lanes] of plan()) {
    const d = displays.find(x => x.id === id);
    each[id] = {lane: lanes[0]};
    if (two(d)) each[id].display = {lane2: lanes[1]};
  }
  if (!Object.keys(patch).length && !Object.keys(each).length) return say("nothing to change", "err");
  $("apply").disabled = true;
  try {
    const out = await post("/api/fleet/apply", {targets, patch, each});
    const bad = out.results.filter(r => !r.ok);
    say(bad.length ? `${out.results.length - bad.length} updated, ${bad.length} failed: ` +
        bad.map(b => b.error).join("; ")
      : (out.results.length === 1 ? "1 display updated" : `${out.results.length} displays updated`),
      bad.length ? "err" : "ok");
    document.querySelector("input[name=lanemode][value=keep]").checked = true;
    setTimeout(refresh, 400);
  } catch (e) { say(e.message, "err"); }
  update();
};

$("identify").onclick = async () => {
  try {
    const out = await post("/api/fleet/identify", {targets: [...selected]});
    const bad = out.results.filter(r => !r.ok);
    say(bad.length ? bad.map(b => b.error).join("; ") : "look at the screens: each is flashing its name",
        bad.length ? "err" : "ok");
  } catch (e) { say(e.message, "err"); }
};
$("selall").onclick = () => { for (const d of shown()) selected.add(d.id); update(); };
$("selnone").onclick = () => { selected.clear(); update(); };
for (const el of [$("filter"), $("sort")]) el.oninput = update;
$("sort").onchange = update;
for (const el of document.querySelectorAll("input[name=lanemode]")) el.onchange = update;
// Clicking into, or typing a number in, one of the boxes chooses that way of
// numbering: nobody should have to find the radio button first.
for (const [box, mode] of [[$("start"), "count"], [$("same"), "same"]]) {
  const choose = () => {
    document.querySelector(`input[name=lanemode][value=${mode}]`).checked = true;
    update();
  };
  box.onfocus = choose;
  box.oninput = choose;
}

// Served from a display as well as from a laptop; the link back only makes
// sense in the first case, so it is shown only when there is a page to go to.
fetch("/api/status", {cache: "no-store"}).then(r => { if (r.ok) $("back").hidden = false; }).catch(() => {});
loadHosts();
refresh();
setInterval(refresh, 3000);
</script></body></html>
"""
