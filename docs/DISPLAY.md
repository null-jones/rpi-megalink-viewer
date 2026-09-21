# How the display is built

Why the screen looks the way it does. Nearly every decision here was a
reaction to something that looked wrong on a real firing point.

A per-position score display for [Megalink Live](https://live.megalink.no/), small
enough for a Raspberry Pi Zero 2 W.

Point it at a club, a range and a firing point, and you get that shooter's card in
a window — the target face with their shots plotted on it, the running total, the
clock, and the score table — updating live:

```bash
megalink gui stord-pk 1-10 9
```

```
┌─────────────────────────────────────────────────────────────────────┐
│ NSF 25m NAIS · Relay: 2               Stord PK              [ 9 ]   │
│ Etai Moredehi Bogen                                       1st of 2  │
│ ┌───────────────┬───────┬───────┬───────┬───────┬───────┬───────┐   │
│ │ 2. Serie 150S │    46 │    41 │    38 │       │       │       │   │
│ └───────────────┴───────┴───────┴───────┴───────┴───────┴───────┘   │
│ ┌──────────────────────────┐    ┌────────────────┬────────────────┐ │
│ │        1  2  3  4        │    │   1:   9.4     │   6:  10.2     │ │
│ │     ╭──────────────╮     │    ├────────────────┼────────────────┤ │
│ │    │   ╭────────╮   │    │    │   2:  10.7     │   7:   9.8     │ │
│ │    │  │  ◍ ◍ ✛   │  │    │    ├────────────────┼────────────────┤ │
│ │    │  │    ◉     │  │    │    │   3:   8.9     │   8:  10.4     │ │
│ │    │   ╰────────╯   │    │    ├────────────────┼────────────────┤ │
│ │     ╰──────────────╯     │    │   4:   9.1     │   9:   9.0     │ │
│ │        1  2  3  4        │    ├────────────────┼────────────────┤ │
│ └──────────────────────────┘    │   5:  10.1     │  10:   9.7     │ │
│ 2. Serie 150S · 25m Rapid .22   ├────────────────┼────────────────┤ │
│ μ 45.0mm · σ 15.4mm ·           │       43       │███ 84 (1x) ████│ │
│ centre (+17.3, +48.5) mm        ├─────────────────────────────────┤ │
│                                 │              00:47              │ │
│                                 └─────────────────────────────────┘ │
│ live · 15 shots · 1 inner · protocol v2                             │
└─────────────────────────────────────────────────────────────────────┘
```

**The shots are not numbered on the plot.** The order they were fired in is in
the grid beside the target; repeating it over the target buries the group under
digits, which is the one thing the plot is there to show. The `--numbers` flag
and the `display.numbers` setting that turned the labels on are gone with them.

The newest shot is red and the rest green. The drawing order follows the paper:
ring numbers belong to the target, so a shot lands **on top of** them, while the
**ring lines are drawn over the shots** — a tight group never hides a ring, and
the markers stay fully saturated instead of washing out.

Ring numbers are sized to the gap they sit in rather than capped, so they stay
readable across a firing point, and the ring lines are held to hairlines however
far the view zooms in: converting the face's millimetre width faithfully gives a
seven-pixel stripe at close zoom, which swamps the target.

The target face is drawn to the real ring dimensions of whatever target the range
is using — a 10m air rifle face has its 0.5mm ten and 30.5mm black, a DFS 200m
face its 100mm ten and 400mm black — so a shot's position on screen means what it
means on paper.

**Everything scales with the window.** Enlarge it and the total, the grid, the
ring numbers and the shot values all grow with it; the type never sits small in
a big frame, and it stays readable down to a 480×320 Pi screen.

The two totals are sized to their box rather than to the window, because the
text in them runs from `84` to `594.8 (23x)` depending on the discipline while
the box stays a fixed half of the panel — a size taken from the window alone
either clips the long total or wastes the box on the short one. Both are set
from one font, so neighbouring figures never come out at different sizes.

**A 60-shot card is shown as six tens**, even when the range publishes it as one
series. Some do: the US Naval Academy's airgun range sends a single `60 Shots`
series with `maxSeriesSize: 10` and the six group totals in
`seriesTotals.splitTotals`, and its own display divides it. A viewer that took
the series at face value piles all sixty shots onto one target and reads the
whole card as a single line in the strip — unreadable long before the card is
finished.

`Series.groups()` does the dividing, and only when the range asks for it: a 25m
card that already publishes six series of five comes back untouched. The group
totals are trusted only when they account for the whole series, because a
sighting series is often declared as a round 100 shots with ten group totals
whatever the group size really is — dividing on those would invent groups the
range never showed.

The groups keep the parent's name, so the strip reads `60 Shots | 95 95 7 0 0 0`
as Megalink's does; the shots are numbered as the card numbers them, so the third
group runs 21 to 30 rather than 1 to 10; and the total beside the card total is
the *series* total, not the group's — the group totals are already in the strip.
`↑`/`↓` step through the groups. Groups past the last shot are left out of that
walk, though the strip still shows the card's full shape.

**Sighters are marked.** A sighting series does not go on the card, and
`counting_series` has always excluded it from the total — but the screen used to
show its total in the same box, the same size and the same colour as a counting
one, so the two figures disagreed with nothing on screen to explain why. The
strip now says `SIGHTERS` and the series total turns amber while the series does
not count. It is set apart rather than hidden: a shooter still wants to see it.

**The clock has a box of its own** under the two totals. During a timed match it
is the second thing a shooter looks for after the score, and the one thing they
cannot get by looking downrange through a scope. It used to be 12pt type in the
header corner — smaller than the shot values beside it. `Clock.remaining_ms()`
walks the range's own stage list, offsets and clock skew included, so the reading matches
the range display rather than approximating it.

**The group is measured.** Under the target, the spread stated as the two
numbers that describe any spread — the mean distance of a shot from the group's
own centre and the standard deviation of that distance — followed by where the
group sits, as a signed offset from the middle of the target:

```
μ 45.0mm · σ 15.4mm · centre (+17.3, +48.5) mm
```

The spread is measured from the mean point of impact rather than from the centre
of the target, so it says how *tight* the group is regardless of where it landed;
the coordinates say where. They are signed in the sense the plot is drawn — x to
the right, y upwards — which tells you which way to move a sight without anyone
having to agree on what "low left" means.

The standard deviation is the population one, dividing by `n` rather than `n-1`:
these shots *are* the group being described, not a sample from which some larger
truth is being estimated. That is what makes μ and σ the right symbols for it
rather than x̄ and s.

The mean point of impact is also crossed on the target itself, in a colour no shot
uses so it can never be counted as one. Neither figure appears until there are two
shots to spread between.

None of this is in the feed; it falls out of the shot coordinates the target
already plots. It is what the card cannot tell you — a tight group in the wrong
place scores exactly like a loose one in the right place.

**The standing on the relay** sits under the lane badge, in the slot the clock
left: `1st of 2`, ranked on the card total with inner tens breaking ties. Only
points actually being shot are counted, so an empty lane is not somebody in last
place, and a tie shares a place and consumes the one below it, as a results list
has it. Working it out means reading every other firing point on the range, so it
is recomputed every two seconds rather than on every frame.

### Every display manages the range

There is no fleet server. Each display already hears every other display's
beacon, so each one also *serves* the dashboard, at `/fleet` on the same port as
its own configuration page:

```
http://fp-09.local:8080/         this firing point
http://fp-09.local:8080/fleet    all of them
```

Open any display and you can renumber the row, point them all at a different
range, or make one of them flash its name so you can find it. Which display you
happen to open does not matter, and there is no machine whose loss takes the
dashboard with it. `megalink fleet` still runs the same page on a laptop for
anyone who would rather work from one.

This costs a display one UDP socket, one thread and a small table of peers.
Twenty displays announcing every ten seconds is two packets a second on a flat
range network, and nothing is shared between them: each owns its own
configuration file, so there is no split brain to resolve and no election to
hold. A display that dies stops announcing and ages out of the others' lists
after 45 seconds.

**Give every display the same token.** `--token` was previously a way to lock
down one display; it is now what lets the displays manage each other, since a
display's own token is what it presents when pushing a change to a neighbour:

```bash
make push PI=pi@fp-09 HOST=stord-pk RANGE=1-10 LANE=9 TOKEN=range-secret
```

Reading the list of displays needs no token; changing anything does — a display
is no less worth protecting because the request arrived by way of its neighbour.
Both pages take the token from `?token=…` once and remember it, so the link you
hand someone carries it and the pages keep working. With no token set the whole
thing is open, which is the sensible default for a closed range network and the
behaviour that was there before.

**A firing point with nobody on it says so.** Rather than an empty card, which
reads as a broken display, the screen is covered with `POSITION NOT IN USE` —
or whatever the range would rather it said — and a club badge if one has been
uploaded from the configuration page. The badge and the message sit at the
middle of the screen, and the badge is scaled to fill its share of it in either
direction: Tk resizes an image by whole numbers only, so a badge is zoomed and
then subsampled to reach a fractional scale, and the zoom is bounded so the
intermediate copy stays within a Pi Zero's 512MB.

Keys: `←`/`→` change firing point, `↑`/`↓` look back through the card's series
(the last one follows the live one again), `f` or `F11` fullscreen, `q` or `Esc`
quit.

### Or let Megalink draw it

A third mode puts **Megalink's own page on the screen** in a full-screen
browser, instead of drawing the scores ourselves:

```bash
make push PI=pi@fp-09 HOST=stord-pk RANGE=1-10 LANE=9 MODE=browser
```

Everything behind the screen is unchanged. The configuration page, the beacon
and the fleet dashboard run exactly as they do in the other modes, so a display
in browser mode is still discovered by its neighbours, still managed from any
other display's `/fleet`, and still switched to another firing point from the
dashboard — it simply reloads the page instead of redrawing a canvas. Changing
the mode makes the process exit so systemd restarts it, which is how a screen
switches between drawing and browsing without anyone visiting it.

Two reasons to want it: the page is always current, including any discipline
this package has not been taught to draw; and pointing it somewhere else is one
setting away —

```bash
megalink config --mode browser --url http://192.168.1.50:8080/#!/local/range
```

— which is the shape a range with no internet will eventually need, once there
is a local MLLiveArena server to point at.

Bear in mind that Chromium with a live single-page app wants a few hundred
megabytes, which a Pi Zero 2 W does not have spare once it is running an X
server. This suits a Pi 4.

**Nothing may put a question on the screen.** These displays have no keyboard and
no mouse, so a dialogue waiting for an answer does not interrupt the display — it
*becomes* the display, until somebody drives out to the range and dismisses it by
hand. Most of the kiosk switches exist for that one reason:

| | |
|---|---|
| `--disable-session-crashed-bubble`, `--hide-crash-restore-bubble` | "restore pages?" after a power cut, and a screen switched off at the wall is shut down uncleanly *every* time |
| `--test-type` | the yellow "unsupported command-line flag" infobar, which `--disable-infobars` no longer covers |
| `--deny-permission-prompts`, `--disable-notifications` | a page asking for notifications, the camera or a location |
| `--noerrdialogs`, `--no-first-run`, `--no-default-browser-check` | the rest of the ways a browser assumes somebody is sitting in front of it |

There is a test asserting every one of them, because losing any single switch
costs a screen and the loss is invisible until the range is being used.

The browser profile lives under `/tmp`, so it is thrown away on reboot and never
wears out the SD card.

There is also a terminal display, for a Pi with no X server. It redraws over
itself rather than scrolling, so a half-second refresh is readable on a console:

```bash
megalink watch stord-pk 1-10 9
```

```
1-10 · Stord PK · relay 2                                               LANE 9
──────────────────────────────────────────────────────────────────────────────
Etai Moredehi Bogen
NSF 25m NAIS · pistol

███ ███ ███
  █   █   █
███ ███ ███
█     █ █
███ ███ ███
TOTAL   2 inner   35 shots

  1. Serie 150S  8.4  6.9  10.4  8.7  9.7                                   41
  2. Serie 150S  8.8  10.5x  10.0  8.5  7.5                            43 (1x)
▸ 10S  6.3  8.7  7.2  9.1  8.3                                              38

──────────────────────────────────────────────────────────────────────────────
clock 00:47 running                                                       live
```

**No runtime dependencies.** The whole package is standard library — no Firebase
SDK, no HTTP client, no plotting library, nothing to compile. That is the point:
on a Pi Zero, every wheel that needs a toolchain is a liability. The window is
Tkinter, which is stdlib; on Raspberry Pi OS the toolkit itself comes from
`apt install python3-tk`.
