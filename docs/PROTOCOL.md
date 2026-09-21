# The Megalink Live data format

Reverse-engineered from the public feed at <https://live.megalink.no>, and
cross-checked against several live ranges. This is the reference behind
`client.py`, `v1.py`, `v2.py`, `parse.py` and `targets.py`.

Megalink Live has no documented API. What follows is what the site actually does,
worked out from `main.bundle.js`, the databases themselves, and cross-checking
decoded values against the totals the ranges publish.

## Where the data lives

The site is a Firebase Realtime Database front end. There are two databases, and
**both are readable without credentials** — the site itself carries no auth token:

| | Database | Holds |
|---|---|---|
| arena | `mllivearena-default-rtdb.europe-west1.firebasedatabase.app` | `hosts_active`, and the current score tree |
| live | `mllive.firebaseio.com` | `hosts`, and the older score tree |

Reading needs no SDK. Firebase's REST interface serves any path with `.json`
appended, and the same URL streams server-sent events when asked for them:

```bash
curl 'https://mllivearena-default-rtdb.europe-west1.firebasedatabase.app/hosts_active.json'
curl -N -H 'Accept: text/event-stream' \
     'https://mllivearena-default-rtdb.europe-west1.firebasedatabase.app/data/stord-pk/1-10.json'
```

The stream sends `put` (replace the node at this path) and `patch` (merge into
it), a `keep-alive` about every 30 seconds, and replays the whole state as a root
`put` whenever it reconnects.

Note that the database *roots* deny reads — only the paths below do not. So
probing `/.json` tells you nothing; the tree has to be walked from the names the
bundle uses.

## Who is live

`hosts_active` on the arena database maps each club to the ranges it is
streaming, which is also where the range keys come from:

```json
{
  "stord-pk":   { "1-10": { "hostName": "Stord PK", "rangeName": "1-10", "eventName": "" } },
  "evenes-skl": { "100m": { ... }, "200m": { ... } }
}
```

## Two protocols

Ranges publish one of two shapes, distinguished by `protocolVersion`. Both are in
service; v2 is what live.megalink.no renders today, and every currently-active
range on the arena database is on it.

### v2 — `protocolVersion` 2.0.1 (current)

One node per range, on the arena database:

```
data/<host>/<range>/data/range          range metadata
data/<host>/<range>/data/fp/<point>     shooter, series, totals and timer
data/<host>/<range>/data/fpList         the firing points, in order
data/<host>/<range>/namelist/<point>    the entry list: name, club, class
data/<host>/<range>/serverClockSkew     range clock minus server clock, in ms
```

Each firing point is self-contained:

```json
{
  "fp": "9", "lane": 9, "relay": 2, "activeSeries": 3,
  "laneNotActive": false, "maxSeriesSize": 5, "cardSize": 30,
  "displayTitle": "NSF 25m NAIS",
  "shooter": { "name": "…", "club": "", "class": "", "gun": "PISTOL" },
  "cardTotals": {
    "total": "84-1x",
    "seriesName":   ["1. Serie 150S", "2. Serie 150S", …],
    "seriesTotals": ["41", "43", "0", …],
    "shotNr":       ["1-5", "6-10", …],
    "splitList":    ["84", "0", "0"]
  },
  "series": [null, {
    "name": " Prøve 150S", "type": "SIGHT", "decimal": false, "complete": true,
    "seriesTotals": { "total": "46-1x", "splitTotals": ["0", …] },
    "targetId": "ISSF25P_SMALL_RF", "targetName": "25m Rapid .22",
    "gaugeR": 2.8, "seriesSize": 100, "shotList": 10,
    "shots": [null, { "v": "9.4 ", "x": -4.229, "y": 0, "t": 11964 }, …]
  }],
  "timer": { "running": false, "timerStart": "1785707495671", "stages": [ … ] }
}
```

- `series.type` is `SIGHT`, `MATCH` or `SHOOTOFF`.
- `x`/`y` are **millimetres** from centre, alongside `gaugeR` (shot-hole radius).
- `t` is time into the series, but the unit varies by discipline — seconds in some
  ranges, milliseconds in others. `time` is the pre-formatted string and is the
  one to display.
- `displayPlot: false` means the range does not want the shot plotted.
- `figure` numbers the target figure, for disciplines with more than one.

The published totals are the ones to trust. **`shots` is a rolling display
window** that can hold more shots than the series actually scores — a shooter who
fires extra sighters leaves them in the list — and `series` itself can hold fewer
series than the card. So `seriesTotals.total` and `cardTotals.total` are
authoritative, and adding the shots up is a cross-check, not a substitute.

### v1 — `protocolVersion` 0.0.1 (legacy)

One node per club, on the live database, with everything keyed
`<range>_<lane>`:

```
data/<host>/ranges/<range>              range metadata and the master clock
data/<host>/cards/<range>_<lane>        who is on the firing point
data/<host>/results/<range>_<lane>      series, shots and totals
data/<host>/clockSkew/<range>           range clock minus server clock
```

```json
{
  "activeSeries": "5",
  "totalSum": "238 (*5)",
  "clock": { "running": false },
  "series": [null, {
    "name": " Prøve", "sight": true, "decimal": false, "complete": true,
    "sum": "45", "splits": ["0", "0", "0", "0", "0"],
    "targetId": "NO_DFS_15M", "gaugeSize": 0.0674698783690671,
    "shots": [{ "value": "8.6", "x": 0.178…, "y": 0.157… }, …]
  }]
}
```

Differences that matter:

- `x`/`y` are a **fraction of the target's width**, not millimetres, matching
  `gaugeSize`.
- `sight: true` stands in for v2's `type: "SIGHT"`.
- `noPlot: true` is v2's `displayPlot: false`.
- `clockSkew` here is a huge number (around −10⁴ seconds… ×10⁶), not the tidy
  millisecond offset v2 uses. It still works the same way arithmetically.
- **The totals are irregular.** `series.sum` is the whole series on some cards and
  only the current group of ten on cards that split a long series; some ranges
  publish a sighting total of `0.00` (flagged italic) with the shots still
  present; and `totalSum` is the card total on most ranges but the current series
  on others. There is no rule that fits every range, so this package reports what
  the range publishes and offers `Series.shot_sum` for callers who would rather
  add the shots up.

## Decoding the values

Every number arrives as a display string, and several conventions are layered on
top.

**Shot values.** An inner ten is marked either ISSF-style with a trailing `x`
(`"10.6x"`) or DFS-style with a leading `*` (`"*10.5"`). Values are padded with
spaces to keep columns aligned (`"10.0 "`).

**Integer series truncate.** Megalink always measures to a tenth of a ring, but
only a series with `decimal: true` *scores* that way. On an integer series the
feed still sends `"10.8"` and the shot scores **10** — truncated, not rounded.
This is what makes the sums add up, and it is easy to get wrong:

```
"*10.8"  "9.6"  "9.6"  "9.9"  "10.1"     ->  10 + 9 + 9 + 9 + 10  =  47
```

which is exactly the `47` that range published for the series. Round instead and
you get 50.

**Totals** come in three shapes, by discipline:

| Form | Meaning | Example |
|---|---|---|
| `"238 (*5)"` | DFS: sum, then inner hits | 238 with 5 inners |
| `"552-8x"` | ISSF: sum, then x count | 552 with 8 x |
| `"133.1"` | decimal sum | 133.1 |
| `"10.42s"` | an elapsed time, not a score | 10.42 s |

**A `Style:` suffix may be appended to any string** and must be stripped before
parsing — `'0.00Style:{"font":"italic"}'` is `0.00`, shown in italic because the
range is flagging it as not counting.

**Non-numeric markers are private-use characters.** Megalink ships a font
(`assets/fonts/FreeSansDU-small.ttf`) whose cmap names them, so a viewer without
the font can still say what they mean:

| Code point | Name | Meaning |
|---|---|---|
| U+E001 | `ISSF_TEN` | inner ten (ISSF) |
| U+E002 | `ISSF_ELEVEN` | inner eleven |
| U+E003 | `DFS_TEN` | ten (DFS) |
| U+E004 | `DFS_CENTER` | centre hit |
| U+E005 | `TURNED` | target turned |
| U+E006 | `FRAME` | hit in the frame |
| U+E007 | `DOUBLE` | double hit |
| U+E008 | `OUT_OF_SEQUENCE` | shot out of sequence |
| U+E009 | `INVALID` | invalid shot |
| U+E00A | `VOID` | no shot / void |
| U+E00B | `SUPER` | super shot |
| U+E00C | `HIDE` | hidden |
| U+E00D–F | `ERR_ADVANCE`, `ERR_TEMP`, `ERR_NOISE` | target errors |
| U+E010 | `GO_BAND` | go band |
| U+E011 | `MANUAL` | manually entered |
| U+E012 | `WARNING` | warning |
| U+E013–5 | `ERR_ENERGY_1…3` | energy errors |

U+FFFD also turns up occasionally, from ranges emitting something the encoding
cannot carry; it is treated as a non-scoring unknown.

## Auto-zoom

Each face in Megalink's table carries a `zoom` range — the visible **diameter**
in millimetres at the closest and widest views:

```json
"ISSF10R": {"zoom": {"min": 13,  "max": 55}}     // 10m air rifle, face is 45.5mm
"DFS200":  {"zoom": {"min": 200, "max": 1060}}   // 200m, face is 1000mm
```

The view is the group's reach with a margin, clamped to that range. Deriving it
from the rings instead would give a worse answer: a 300m face and a 10m one want
very different closest views, and the table already knows which. The maximum is
a little wider than the face, which is where the margin around a full target
comes from.

## Target geometry

The bundle carries a table of 196 target definitions, embedded as a
`JSON.parse('[...]')` string. `data/targets.json` here is a trimmed copy: the 71
ring-based faces, minus the irregular hunting and field silhouettes, which are
arbitrary artwork rather than rings.

A face describes itself like this:

```json
{"id": "ISSF10R", "name": "10m Air Rifle", "gauge": 4.5,
 "value": {"1": 45.5, "10": 0.5, "x": -0.5, "type": "ringLowHighX"},
 "draw": {"target": {"type": "value", "fillColor": "white", "value": 1},
          "aim":    {"type": "value", "fillColor": "black", "value": 4},
          "center": {"type": "value", "fillColor": "white", "value": 10},
          "value":  {"line": 0.1, "black": [1,2,3,4], "white": [5,6,7,8,9,10]},
          "number": {"type": "hv", "black": [1,2,3], "white": [4,5,6,7,8]}}}
```

Read that as: ring 1 is 45.5mm across and ring 10 is 0.5mm, evenly spaced between
(so 5mm of diameter per ring); the paper is white out to ring 1; the black aiming
area runs out to ring 4, which is 30.5mm; the ten is a white dot; ring lines are
dark over the white and light over the black; and rings 1–8 are numbered on both
axes. Those are the real dimensions of an ISSF 10m air rifle target.

Three geometry encodings appear:

| `value.type` | Meaning |
|---|---|
| `ringLowHighX` | diameters given for the outermost and innermost ring, interpolated evenly; `x` is the inner ten |
| `ringSpec` | every ring's diameter listed, for faces that are not evenly spaced |
| `ringLowHigh` | as `ringLowHighX` but with no inner ring |

Details worth knowing:

- `low`/`high` name the outermost and innermost rings, which are not always 1 and
  10 — the 25m rapid-fire face runs 5 to 10.
- **`x` is drawn as "ring 11"** in the `draw` lists. A negative `x` means the inner
  ten coincides with the ten ring and no separate ring is drawn.
- `draw.aim` can be `{"type": "shape", ...}` instead of a disc, for faces whose
  aiming mark is a silhouette. Those faces still draw their rings; the silhouette
  is skipped.
- `draw.number.type` is `hv`, `v` or `h` — the 25m pistol faces number only the
  vertical axis.
- `gauge` is the projectile diameter, and it is also the scoring rule: **a shot
  counts the best ring its edge touches**, so the gauge radius comes off the
  distance before the ring is decided.

### Shot coordinates

The two protocols disagree, and neither says so:

| | Units of `x`/`y` | Shot-hole size |
|---|---|---|
| v2 | **millimetres** from centre | `gaugeR`, a radius in mm |
| v1 | fraction of the target's outer **radius**, so ±1 spans the face | `gaugeSize`, a fraction of the outer **diameter** |

Note the asymmetry inside v1: positions are relative to the radius while the hole
size is relative to the diameter. That is not a guess — it falls out of the
measurement below, and `gaugeSize` matches `gauge / outer diameter` exactly for
every legacy target (`0.0989010989010989` = 4.5 / 45.5, for instance, which is
also how the legacy target ids were mapped onto the modern ones).

### How this was checked

None of the above is documented, so it was established by measurement rather than
by reading. Since the feed publishes both a position *and* a score for every shot,
the geometry can be tested by throwing the score away, recomputing it from the
position, and comparing:

```
score = ring the shot's edge reaches, interpolating between ring radii
      = f(hypot(x, y) × scale − gauge/2)
```

Run over roughly 10,000 shots from every live range, that reproduces the published
value to a median of **0.05 of a ring** — which is the rounding in the feed's own
one-decimal values — across 15 target families and both protocols. It is also what
settled the open questions:

- **v1's scale.** Normalising by the outer *diameter* gave a median error of ~0.9
  of a ring; by the outer *radius*, 0.05. Not a close call.
- **Integer targets truncate.** Already known from the sums, and confirmed
  independently here.
- **A miss scores zero.** Extrapolating outside ring 1 gives negative numbers; the
  feed publishes `0.0`. The scoring floor is zero.
- **DFS readings run past ten.** Values like `10.8` are real (`xMethod: "10.5"`),
  so the reading is extrapolated inside the ten ring rather than clamped.
- **Two faces disagree with their own labels.** The Norwegian 3D-Score 100m face
  (`DFS100_3D`) has its own uneven ring spacing but its scores are quoted on the
  standard `DFS100` rings — 1.4 rings of error against its own geometry, 0.05
  against DFS100. `SCORING_ALIASES` records that. Separately, one range publishes
  `ISSF25P_BIG_PREC` while its shots score on the rapid-fire rings, which looks
  like a mislabel at the range rather than something to encode.

`TargetFace.score_at` is what does this. Nothing in the display needs it — the
feed sends the values — but it is the instrument that pins the geometry down, and
`tests/test_targets.py` runs it over every shot in the fixtures on every test run.

Two faces are *not* fully validated: `DGI_M96G` and `DDSM84` reproduce to only
~0.4–1.3 of a ring on the small samples available, so their drawn faces may be
imperfect. They are the exception, and they are the two rarest in the data.

## The clock

Both protocols describe the clock as a start time plus a list of stages, and the
number on the range display is worked out from them. This reimplements the web
client's walk (`Kr` in the bundle):

```
elapsed = now − skew − start

for each stage:
    limit = stage.offset + stage.startCount
    if stage.countingUp:
        skip an open-ended COMMAND stage (limit == 0)
        if elapsed > limit:  show elapsed − stage.offset
    else:
        if elapsed < limit:  show limit − elapsed   (rounded up)
show 0
```

`skew` translates the range's clock onto the database server's. A range sitting
idle reports `running: true` with no start time at all, which is a clock switched
on rather than one running since 1970 — hence the display's `standby`.

## URL slugs are not database keys

The site's own URLs do not spell ranges the way the database does:
`live.megalink.no/#!/hanebjerg-skyttecenter/50m` addresses the range keyed `50m`
in the v2 tree and `50-m` in the v1 tree, from a range named `50 M`. The database
key is `name.toLowerCase().replace(/[_\s]+/g, "-")`; the URL drops more. So range
lookups here compare with punctuation removed, and accept any of the three.

## What is not covered

- **Writing.** This is read-only, and the databases would refuse anyway.
- **Team cards.** The bundle has a `team` path alongside `shooter`, for team
  events. Nothing in the feed used it while this was written, so it is not
  decoded.
- **Irregular target faces.** The hunting and field silhouettes (running elk,
  reindeer, the DFS *finfelt* series) are defined as drawing programs — arcs,
  lines and bitmaps — rather than rings. Shots on those are plotted on a plain
  grid with a note saying the face is missing, rather than on a wrong face.
- **`res.megalink.no`.** Completed results live on a separate, unrelated service.
- **Displays on another network.** Discovery is LAN broadcast, so the fleet
  dashboard has to share a subnet with the displays. Reaching across a router
  would mean each display checking in with a central server instead — a bigger
  change than it sounds, and not what a range needs.
