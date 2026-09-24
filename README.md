<img src="assets/icon.svg" width="72" align="right" alt="">

# rpi-megalink-viewer

[![CI](https://github.com/null-jones/rpi-megalink-viewer/actions/workflows/ci.yml/badge.svg)](https://github.com/null-jones/rpi-megalink-viewer/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

A per-firing-point score display for [Megalink Live](https://live.megalink.no/),
small enough to run on a Raspberry Pi Zero 2 W.

Point one at a club, a range and a firing point, and it shows that shooter's card
on a screen beside them: the target face with their shots plotted on it, the
running total, the clock, and how the group is sitting. Bolt it to the bench,
give it power, and it comes up by itself.

**No dependencies.** Python 3.11+ and the standard library — Tkinter for the
window, `http.server` for the configuration page, `urllib` for the feed. Nothing
to compile, and nothing to break on an upgrade two years from now.

---

## Try it in thirty seconds

```bash
git clone git@github.com:null-jones/rpi-megalink-viewer.git
cd rpi-megalink-viewer
make install

uv run megalink hosts                      # which clubs are shooting now?
uv run megalink gui stord-pk 1-10 9        # open a window on one firing point
```

You do not need a Megalink system of your own — the feed is public, and there is
usually a range live somewhere.

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
│ │    │  │  ◍ ◍ ✛   │  │    │    │   2:  10.7     │   7:   9.8     │ │
│ │     ╰──────────────╯     │    ├────────────────┼────────────────┤ │
│ │        1  2  3  4        │    │   5:  10.1     │  10:   9.7     │ │
│ └──────────────────────────┘    ├────────────────┼────────────────┤ │
│ μ 45.0mm · σ 15.4mm ·           │       43       │███ 84 (1x) ████│ │
│ centre (+17.3, +48.5) mm        │              00:47              │ │
│ live · 15 shots · 1 inner · protocol v2                             │
└─────────────────────────────────────────────────────────────────────┘
```

## Put it on a Raspberry Pi

**The easy way: the ready-made image.** Download
`megalink-display-<version>.img.xz` from the
[latest release](https://github.com/null-jones/rpi-megalink-viewer/releases/latest),
write it to an SD card with [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
(*Choose OS* → *Use Custom*), put the card in the Pi and plug it in. That is
all: no laptop, no SSH, no terminal. The screen then says what to do next.

Set the Wi-Fi in Imager's settings if it offers them, and the display joins that
network. If not, it starts one of its own and shows a code that joins it. Either
way you end up on its settings page, picking the club, range and firing point
from lists. Every display names itself `megalink-` and four characters of its
serial number, so a bench of new ones do not all answer to the same name.

The image is 64-bit Raspberry Pi OS Lite (Trixie) for a Zero 2 W, 3, 4 or 5.
[docs/RASPBERRY-PI.md](docs/RASPBERRY-PI.md#the-ready-made-image) says what is
in it and how to build it yourself.

**By hand, from a laptop.** Flash **Raspberry Pi OS Lite**, set the Wi-Fi and
SSH in Raspberry Pi Imager, then:

```bash
make push PI=pi@fp-09 HOST=stord-pk RANGE=1-10 LANE=9 MODE=gui AUTOLOGIN=1
```

That copies this checkout over SSH — no GitHub credentials ever go on the Pi —
installs it, and sets up a boot service. The Pi comes up showing firing point 9
and keeps doing it through reboots and power cuts.

**A display that hasn't been set up says how.** Instead of an empty card it
shows its address and a QR code: scan it with a phone on the same network and
you land on its settings page, where the club, range and firing point are picked
from lists. The code carries the IP address rather than the `.local` name, which
plenty of phones can't resolve. The console and browser modes show the same.

**A display that can't find its Wi-Fi makes its own.** If it isn't on a
network 30 seconds after booting — a new range, a changed password, a card
flashed without Wi-Fi — it starts a Wi-Fi network of its own and shows two codes:
one that joins it, one that opens the settings page, where you pick the real
network from a list. Every few minutes, with nobody connected, it looks again
for a network it already knows.

Add `TOKEN=some-secret` to lock the displays down. Give every display on a range
the same one, so they can manage each other.

[docs/RASPBERRY-PI.md](docs/RASPBERRY-PI.md) has the rest — including what to do
when the screen stays black, which on a Lite image it will, once.

## Two firing points on one Pi

A **Raspberry Pi 4 Model B** and a **Pi 5** each have two HDMI sockets, and a
range often wants two positions covered by one box rather than two:

```bash
make push PI=pi@bench-3 HOST=stord-pk RANGE=1-10 LANE=9 LANE2=10 MODE=gui
```

Lane 9 goes on the left-hand screen, lane 10 on the right — "left" and "right"
meaning where they actually sit, not which socket they are plugged into, since
the outputs are ordered by position rather than by name.

It stays **one process**: one feed, one configuration page, one beacon, one entry
in the fleet dashboard. The second screen is a second window sharing the first
one's main loop, and each follows its own setting, so renumbering from the
dashboard moves them independently.

Works in `browser` mode too, with a Chromium window on each output. A Pi Zero
has one socket, so `lane2` is ignored there with a line in the log saying so.

## Three ways to show it

| Mode | What it does | Good for |
|---|---|---|
| `gui` | Draws the target and the card itself, in Tkinter | **The default.** Light enough for a Pi Zero 2 W |
| `browser` | Full-screen Chromium on Megalink's own page | A Pi 4. Always current, whatever the discipline |
| `terminal` | Draws to the console, no X server needed | A Pi with no display stack, and for debugging |

```bash
megalink config --mode browser
```

Changing the mode restarts the display by itself — including remotely, which is
how you rescue a screen whose X server has given up.

## Every display manages the range

There is no central server to run. Each display hears the others over UDP
broadcast, so **each one also serves the dashboard for all of them**, on the same
port as its own settings page:

```
http://fp-09.local:8080/         this firing point
http://fp-09.local:8080/fleet    every firing point on the range
```

Open whichever display you happen to be standing next to and renumber the whole
row, point them all at a different range, or make one flash its name so you can
find it. No machine is special, and losing one loses nothing but that screen.

**Set a token.** Without one, anyone on the range network can change any display
— and through `/fleet`, all of them. Pass the same `TOKEN=` to every display;
it's also what they use to manage each other. The installer reminds you if you
forget. [docs/RASPBERRY-PI.md](docs/RASPBERRY-PI.md#a-note-on-access) explains
exactly what an open display is and isn't exposed to.

## Configuration

Everything lives in one JSON file (`/etc/megalink/display.json` on a Pi), and
anything in it can be set from the web page, the CLI, or the fleet dashboard.

```bash
megalink config --host stord-pk --range 1-10 --lane 9
megalink config --lane2 10           # second HDMI output, on a Pi 4 or Pi 5
megalink config --mode gui --interval 0.5
megalink config                      # show what this machine is set to
```

A firing point with nobody on it says `POSITION NOT IN USE`, or shows your club
badge if you upload one.

## Documentation

| | |
|---|---|
| [docs/RASPBERRY-PI.md](docs/RASPBERRY-PI.md) | Flashing, the boot service, X on a Lite image, the fleet, and what goes wrong |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | The Megalink Live data format, reverse-engineered: both protocol generations, the scoring rules, the target geometry |
| [docs/DISPLAY.md](docs/DISPLAY.md) | Why the screen looks the way it does |
| [docs/LIBRARY.md](docs/LIBRARY.md) | Using the package from your own code |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Tests, linting, and how this was built |

## Development

```bash
make check     # lint, format check, and the full test suite
make test
```

The suite needs no network: it runs against captured feeds from several real
ranges in `tests/data/`.

Every pull request runs the checks in Debian Bookworm and Trixie containers,
with the Python and Tk each ships — which is what Raspberry Pi OS Bookworm and
Trixie are built on, and so what a display actually runs. A step fails the build
if the fonts there cannot scale, rather than letting the tests about fitting
type quietly skip. The deployment shell scripts are checked with `shellcheck` in
the same run, since a mistake in those surfaces at the range rather than here.

Pushing a `v*` tag builds the SD card image with `image/build.sh`, checks its
files with `image/check.sh`, boots it with `image/boot-test.sh` to see the
hotspot fallback start, and attaches it to the release.

## Licence

[MIT](LICENSE). Do what you like with it.

## What this is, and is not

An independent project, not affiliated with or endorsed by Megalink AS. It reads
the same public feed that [live.megalink.no](https://live.megalink.no/) serves to
any browser, and it only ever reads — nothing here writes to a range, a target or
a scoring system.
