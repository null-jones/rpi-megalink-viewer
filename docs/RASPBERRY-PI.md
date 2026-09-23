# Running it on a Raspberry Pi

The deployment story in full: flashing a card, the boot service, the X
server, the fleet, and the several ways this went wrong before it went
right. [The README](../README.md) has the short version.

The point of this is a screen bolted beside a firing point that comes up showing
the right shooter and needs nothing from anybody afterwards. So a display gets
its whole identity from one small file, which it *watches* — change the file and
the screen follows within a couple of seconds. No restart, no SSH, no keyboard.

### Which image to flash

**Raspberry Pi OS Lite, 32-bit.** In Raspberry Pi Imager it is under *Raspberry
Pi OS (other)* → *Raspberry Pi OS Lite (32-bit)*, currently the Trixie
(Debian 13) release.

Two decisions, both driven by the Zero 2 W having 512MB of RAM:

- **Lite, not Desktop.** Nothing here wants a desktop. The boot service brings up
  a bare X server with this one client on it via `xinit` — no window manager, no
  file manager, no cursor. A desktop image would spend a large slice of the RAM
  on things that then have to be got out of the way.
- **32-bit, not 64-bit.** The Zero 2 W's CPU is 64-bit capable and the 64-bit
  image runs on it, but a 64-bit userland costs noticeably more memory for no
  benefit to this workload. Pick 64-bit only if you want one architecture across
  a fleet that also has Pi 4s or 5s in it.

Take the current release rather than *Legacy*: Trixie ships Python 3.13 and
Bookworm 3.11, and the test suite passes on both, so there is nothing to gain by
staying back. Either way `python3-tk` is a package away, which is all the window
needs.

Roughly what it costs: the feed, the configuration page and the beacon together
measure ~32MB of RSS on a development machine, and Tk plus a minimal X server
add perhaps as much again. Against 512MB with a Lite base, that is comfortable —
and `--mode terminal` drops X entirely if you would rather not run one.

### Install

Use Raspberry Pi Imager's settings screen for the hostname, user account, SSH
and Wi-Fi. Do use it for Wi-Fi in particular: from Bookworm onwards
NetworkManager replaced `wpa_supplicant.conf`, so a file dropped on the boot
partition is ignored.

Two of those settings matter to this software:

- **Give each Pi its own hostname** — `fp-09`, say. A display's name defaults to
  its hostname, so the fleet dashboard lists something meaningful without any
  further configuration.
- **Enable SSH**, because installing needs a shell once.

You will also want a mini-HDMI adapter; the Zero 2 W has no full-size port.

### Getting the code onto the Pi

Push it from the machine you work on, rather than pulling it on the Pi. That way
the Pi never holds a credential of yours, and it needs neither git nor a build
toolchain:

```bash
make push PI=pi@fp-09 HOST=stord-pk RANGE=1-10 LANE=9
```

or directly, with anything after the destination passed to the installer:

```bash
./deploy/push.sh pi@fp-09 --host stord-pk --range 1-10 --lane 9 --name firing-point-9
```

That builds the wheel here, copies the checkout across with `rsync` — about
700KB, since the virtual environment and caches are excluded — and runs the
installer over SSH. The Pi installs from the copied wheel with `--no-index`, so
it never reaches PyPI: a Zero 2 W is slow at building, and depending on the
internet at a range is a poor idea when the whole package is pure Python and
architecture-independent anyway.

Re-installing **does not change the display mode** unless you ask it to. It used
to pass `--mode gui` every time, which silently undid a switch to the console
made from the web page — and on a machine whose X server does not work, that put
the screen back to black on every push. Pass `MODE=terminal` to change it:

```bash
make push PI=pi@fp-09 MODE=terminal
```

`--no-install` copies without installing. Re-running it later is the update
path: `rsync` sends only what changed and the installer force-reinstalls, which
matters because the version number rarely moves between pushes.

Save yourself the password prompts first:

```bash
ssh-copy-id pi@fp-09
```

If you would rather not use the script, the same thing by hand:

```bash
make build
rsync -az --exclude '.venv/' --exclude '__pycache__/' ./ pi@fp-09:megalink-viewer/
ssh -t pi@fp-09 'cd megalink-viewer && sudo ./deploy/install.sh --host stord-pk --lane 9'
```

Then, on the Pi (or done for you by the push above):

```bash
sudo ./deploy/install.sh --host stord-pk --range 1-10 --lane 9 --name firing-point-9
```

That installs the toolkit packages, creates a `megalink` service user, installs
the package into `/opt/megalink/venv`, writes `/etc/megalink/display.json`, and
enables a service that starts the display at boot. It prints the address of the
display's own configuration page when it finishes.

The club, range and firing point are all optional. A display with none of them
comes up saying "not configured" and still serves its web page, so a box can be
installed first and pointed at a range later.

For a bare **Pi OS Lite** with no X server, add `--mode terminal`: the same
display, rendered on the console.

### Starting at boot

The installer enables a systemd service, so the Pi comes up showing scores with
nothing plugged into it but power and a screen. Getting a *service* to own a
screen on a Pi takes more than enabling a unit, though, and the installer does
these too:

- **Lets a service start X.** Debian ships `allowed_users=console` in
  `/etc/X11/Xwrapper.config`, meaning only somebody logged in at the keyboard
  may start an X server. A systemd service is not that, so the installer writes
  `allowed_users=anybody`. Without it X refuses and the screen stays blank.
- **Does not set `NoNewPrivileges`.** Xorg starts through a setuid wrapper,
  which that would block. `ProtectSystem` is `full` rather than `strict` for a
  related reason: Xorg writes its log under the service user's home.
- **Takes tty1 back from the login prompt** while it is running, with
  `Conflicts=getty@tty1.service`. The getty is deliberately *not* disabled: if
  the display ever fails to start, a login prompt coming back is the difference
  between a Pi you can walk up to and a Pi showing a frozen boot message with no
  way in but the network. An earlier version of this disabled it, and produced
  exactly that.
- **Stops the screen blanking.** X is started with `-s 0 -dpms`, and the console
  path runs `setterm --blank 0 --powerdown 0`. A display that goes black after
  ten idle minutes is a broken display.
- **Puts the console back into text mode.** X switches the virtual terminal to
  graphics mode and is meant to switch it back on the way out; one that died
  while starting often has not, and the console then shows nothing at all — a
  blank screen that looks like a dead display but is a live one drawing into a
  terminal the kernel has stopped painting. The launcher resets it with a
  `KDSETMODE` ioctl before it starts, and again before falling back.
- **Sizes, maps and raises the window itself.** `-fullscreen` is a hint to a
  window manager, and a display on a bare X server has none — so the window is
  given the screen's dimensions explicitly and mapped and raised by hand.

The display logs where the window actually ended up, because one that reports
success and shows nothing has usually put itself somewhere unhelpful:

```
window 1920x1080+0+0 mapped=True viewable=True screen=1920x1080 fullscreen=True
```

If that line looks right and the screen is still dark, the problem is below the
application: X has set a video mode the monitor cannot show. `xsetroot -solid
red` settles it in one command — a red screen means X is driving the display and
the fault is the window; a dark one means it is not.
- **Puts the frames on the screen and the logs in the journal.** The unit sets
  `StandardOutput=tty` with `StandardError=journal`. Sending both to the journal
  makes the console display render beautifully into a log file nobody is reading
  while the screen keeps showing whatever the boot left behind.

The display logs its own progress as it starts, so a failure says how far it got
rather than leaving it to be inferred from a blank screen:

```
megalink 0.1.0 starting: config /etc/megalink/display.json, mode gui
showing: texas-christian-university · tcu · lane 2
configuration page on port 8080
feed: connected via protocol v2
opening a window on DISPLAY=:0
window open
```

The installer then waits a moment and says whether the service actually came up,
printing the log if it did not — "enabled" is not the same as "working" and
should not be reported as though it were.

```bash
systemctl status megalink-display
journalctl -u megalink-display -f
```

**If X refuses, the display does not die.** The launcher falls back to the
console renderer and says so in the journal, along with the tail of the Xorg
log. That matters for more than the screen: a process that simply exited would
be restarted by systemd in a loop, taking the configuration page and the beacon
down with it every few seconds — and a display with broken X would become
invisible to the fleet dashboard and unreachable from a phone, which is exactly
when you need it to be reachable. Falling back keeps it manageable.

To see what X objected to:

```bash
journalctl -u megalink-display -n 60 --no-pager
sudo cat /var/log/Xorg.0.log            # or ~megalink/.local/share/xorg/Xorg.0.log
```

**The one package nobody tells you about.** On Raspberry Pi OS **Lite**, X will
start, read the monitor's EDID, report an active mode in `xrandr` — and put
nothing on the screen, for any client at all. The missing piece is
`/etc/X11/xorg.conf.d/99-v3d.conf`, which is the file that tells X the Pi's
display driver is `vc4`. It is written by the **`gldriver-test`** package, which
the Desktop images install and the Lite images deliberately do not, on the
reasonable grounds that Lite is meant to be used without a desktop.

The installer adds it. Note that the package writes that file from a service
that runs at boot, so a Pi that has not rebooted since installing it may still
have an empty `/etc/X11/xorg.conf.d` — the installer runs the generator itself
where it can, and tells you to reboot where it cannot.

The rest of the arrangement is the ordinary Pi kiosk one: `xinit` starts a
session that runs a **minimal window manager** and then the display. The window manager matters
more than it sounds — a bare X server will map a toolkit's window, report it
mapped and viewable, and leave it there with nothing to size it, raise it or
give it focus. `matchbox-window-manager` is about a megabyte, has no desktop,
panel or icons, and makes one application fill the screen, which is all a score
display needs.

Other things dealt with, because they are what goes wrong when X is started by a
service rather than a login session:

- **No `PrivateTmp`.** X puts its socket in `/tmp/.X11-unix`, created once at
  boot in the real `/tmp`. A private `/tmp` is empty, a non-root X cannot create
  the directory, and it exits with `_XSERVTransmkdir: ERROR: euid != 0` before
  it probes a screen. This one is easy to miss because X then shuts down
  *tidily* and reports success.
- **`vt1`** — Xorg cannot work out which virtual terminal to take outside a
  session, and gives no useful message when it is not told.
- **No `-keeptty`.** Xorg's manual calls it "only useful when debugging", and
  `startx` never passes it. It stops the server detaching from its controlling
  terminal, and with it the server can fail to take the virtual terminal at all
  — so the console keeps the scanout while X renders into a buffer that never
  reaches the screen. Every log says success, `xrandr` reports the mode active,
  and *no* client appears, not even `xsetroot`. It is a convincing imitation of
  about four other faults.
- **`allowed_users=anybody`** in `Xwrapper.config`, and no `NoNewPrivileges`,
  which would block the setuid wrapper.
- **`PAMName=login`**, applied as a drop-in for `gui` mode only, which registers
  a real login session with logind. This one is worth knowing about, because
  without it X *appears* to work. It starts,
  reads the monitor's EDID, lists every mode the display supports, reports
  success — and drives nothing. The only sign is one line in its log:

  ```
  (EE) systemd-logind: failed to get session: PID 1422 does not belong to any known session
  ```

  No session means no seat, and no seat means X never becomes DRM master. The
  screen stays black while every other log says the server started fine.

  It is written as a drop-in so the installer can take it back if the service
  will not start with it — a console display that works beats a window that
  might. But it is written **whatever mode is configured**, and so is the
  `Xwrapper.config` permission. The mode is changeable at runtime from the web
  page and the fleet dashboard; a prerequisite that only arrives when the
  installer happens to run with `gui` selected is missing exactly when somebody
  switches to a window, and there is nothing on screen to say why. Only the X
  *packages* stay conditional, because they are about 100MB.

The launcher decides whether X worked by **how long it lasted**, not by its exit
status: a server that cannot create its socket exits 0, so anything that comes
back inside ten seconds is treated as a failed start no matter what it claims.

### The zero-byte console script

Worth writing down, because it cost several rounds and impersonated four
different bugs. A venv's `bin/megalink` and `bin/pip` are small generated
scripts. If one is truncated to zero bytes but keeps its execute bit — an
interrupted write, a full or flaky SD card — then running it does not fail. An
empty file with `+x` is an empty shell script: **no output, exit status 0.**

That is corrosive out of all proportion to the cause:

- `pip install` becomes a silent no-op that reports success, so every later
  install "works" while changing nothing;
- `megalink display` starts and stops instantly and cleanly, so systemd says
  `Deactivated successfully`;
- under `xinit` the client vanishes the moment X is ready, so X tears itself
  down and reports `Server terminated successfully (0)` — which reads exactly
  like a display-server problem and is not one;
- every failure has status 0, so nothing anywhere looks like an error.

The install and the launcher therefore never use the console scripts. Both go
through `python -m pip` and `python -m megalink_viewer`, which read the
installed package and cannot be fooled the same way. The installer then *checks*
that `python -m megalink_viewer --version` actually prints something, rebuilds
the environment from scratch if it does not, and refuses to finish if it still
does not. An install that reports success while leaving nothing runnable behind
is worse than one that fails.

`./deploy/diagnose.sh` on the Pi, or `make diagnose PI=user@host`, prints the
size of each console script along with everything else worth knowing.

The same class of damage reaches the configuration file, so an **empty** one is
read as no configuration rather than as an error. There is nothing in an empty
file to lose by starting from defaults, and refusing to start over one leaves a
display dead for a reason nobody at the range can act on. Malformed JSON is
still refused, because a typo holds intent and should not be silently dropped —
and the installer moves an unreadable file aside rather than letting it block
the configuration it was asked to write.

### Zero-byte files, again

The same failure has now arrived four times on the same machine, in four
different files, wearing a different disguise each time:

| Empty file | How it presented |
|---|---|
| `venv/bin/pip` | every install "succeeded" and changed nothing |
| `venv/bin/megalink` | the display started and stopped instantly, exit 0 |
| `/etc/megalink/display.json` | the installer refused to write a config |
| the systemd unit | `Loaded: masked`, service never ran, no logs at all |

An empty file with the execute bit is an empty shell script: success, no output.
An empty unit file is what systemd treats as masked. An empty config used to be
a fatal parse error. None of them look like a broken file; they look like four
unrelated bugs somewhere else.

So the installer now writes, `sync`s, and *checks* the unit file and the
configuration, rewrites them once if they came back empty, unmasks the service
if an earlier empty unit left it that way, and stops with a clear message rather
than continuing. `deploy/diagnose.sh` lists every file that should not be empty
and flags any that is.

Four in one session is not bad luck. If it keeps happening, the SD card is the
thing to replace — `dmesg` and the filesystem mount options are in the diagnostic
output for that reason.

### A pending signal at startup

Worth writing down too, because it also looked exactly like an X problem. A signal that is *pending* when a program
starts survives `exec`, so a SIGTERM aimed at the launcher shell — `xinit`
signals its process group on the way out — arrived at the display as though
somebody had asked it to stop. It shut down immediately, before drawing a frame,
and exited 0 while doing it, which made a working display look like a broken
one. `stop_flag` now discards anything already pending before it starts
listening, and the display logs why it stopped:

```
stopping: signal SIGTERM        # somebody really did ask
stopping: the window was closed # somebody closed it
discarded a SIGTERM left pending at startup
```

**Changing mode takes effect on its own.** The launcher decides between a window
and the console before the display process starts, so a mode change cannot be
applied in place — the display notices, exits, and systemd starts it again into
the new mode. That is what lets a Pi whose X server is broken be switched to the
console from the fleet dashboard, without anyone walking to the shed.

**If X runs but the screen stays dark**, the problem is below the application
and no amount of window handling will fix it. One command separates the two:

```bash
sudo -u megalink XAUTHORITY=/opt/megalink/.Xauthority DISPLAY=:0 xsetroot -solid red
```

A red screen means X is driving the monitor and the fault is the window. A dark
one means X has set a video mode the monitor cannot show — worth checking
against the boot console, which uses the kernel's framebuffer and a different
mode entirely. `hdmi_group`/`hdmi_mode` or `video=` in the Pi's boot
configuration are where that gets settled; console mode works meanwhile.

**If it still refuses**, start the display from a real login session instead of
a service:

```bash
make push PI=pi@fp-09 MODE=gui AUTOLOGIN=1
```

`PAMName=login` asks systemd to synthesise a session, which is enough on most
machines and not on all. An autologin gives X the session a login actually
creates, which nothing has to synthesise — and it satisfies
`allowed_users=console` honestly, because the user genuinely is one.

The session runs the same launcher the service does, so the mode, the web page,
the beacon and the fleet dashboard all behave identically. What changes is how
you bounce it:

```bash
sudo systemctl restart getty@tty1        # instead of megalink-display
journalctl -t megalink-display -f        # -t, not -u: it is no longer a unit
```

The session's output is piped through `systemd-cat`, because a login shell
writes to its terminal and X has that terminal in graphics mode — anything sent
there is invisible. Without it an autologin display has no log at all.

`AUTOLOGIN=1` is remembered by nothing: pass `--service` (or push without it) to
go back to the systemd service, and the installer undoes the getty override and
the profile.

Or sidestep X altogether with `--mode terminal`: no display server, no wrapper
configuration, nothing to go wrong at boot. You lose the plotted target and keep
the scores.

### Pre-configuring the card

If you would rather the Pi arrive already knowing what it is, write the
configuration onto the boot partition before first boot:

```bash
./deploy/provision-sdcard.sh --boot /Volumes/bootfs \
    --host stord-pk --range 1-10 --lane 9 --name firing-point-9 \
    --repo https://example.com/megalink-viewer.git
```

That drops a `megalink.json` and a first-boot script on the card. It can write
Wi-Fi settings too (`--ssid`, `--psk`, `--country`), for a card flashed without
Imager — though on Bookworm that file is ignored and Imager is the right answer.

One step is not automatic: Raspberry Pi OS only runs *its own* `firstrun.sh`, so
the script tells you the one line to add to it, or you can just run it once over
SSH. Automating that means editing a file the OS generates, which is more likely
to break between releases than to help.

### The configuration file

```json
{
  "host": "stord-pk",
  "range": "1-10",
  "lane": "9",
  "display": { "mode": "gui", "fullscreen": true, "interval": 0.5 },
  "web":     { "enabled": true, "port": 8080, "bind": "0.0.0.0", "token": "" },
  "beacon":  { "enabled": true, "port": 45455, "interval": 10, "name": "firing-point-9" }
}
```

`range` accepts the database key, the range's display name, or the slug from a
Live URL. `lane` is text, not a number, because some ranges label firing points
with letters. `display.width`/`height` override the detected screen size for a
panel that reports it wrongly. Setting `web.port` to 0 asks the OS for a free
port, which is how you run two displays on one machine.

Edit it by hand, or from the command line:

```bash
sudo -u megalink /opt/megalink/venv/bin/megalink config --lane 7
```

JSON rather than TOML because the file is *written* by software as well as read,
and JSON is the only human-legible format the standard library can do both with
on every Python this supports.

### The display's own web page

Every display serves a page at `http://<display>:8080/`. Open it on a phone at
the firing point and you get what it is showing right now — shooter, total,
shots, whether the feed is live — and dropdowns to change the club, range and
firing point, populated from the live host list. There is an **Identify** button
that makes that screen flash its name, for when twenty of them look alike.

Its API is small enough to script against:

```
GET  /api/status              what this display is showing
GET  /api/config              the stored configuration
PUT  /api/config              merge a partial update and adopt it
GET  /api/hosts               clubs currently streaming
GET  /api/ranges?host=        that club's live ranges
GET  /api/lanes?host=&range=  the firing points on a range
POST /api/identify            make this screen announce itself
GET  /healthz                 liveness, for a watchdog
```

```bash
curl -X PUT http://firing-point-9:8080/api/config \
     -H 'Content-Type: application/json' -d '{"lane": "7"}'
```

Updates are merged, not replaced, so sending one field cannot blank the rest.

### The fleet dashboard

From a laptop on the same network:

```bash
megalink fleet --open
```

Every display broadcasts a small UDP packet every ten seconds saying who it is
and what it is showing; the dashboard listens and lists them. Select some and
change them together — which is the difference between setting up twenty firing
points and setting up one twenty times.

The useful part is **consecutive numbering**: select the displays in order, set
the club and range, put `1` in the firing-point box with "count up per display",
and they take lanes 1, 2, 3… in one action. Each row also links to that
display's own page, and Identify works across a selection.

If one display is switched off, the others still take the change and the page
says which one failed rather than silently doing half the job.

Broadcast rather than mDNS because a range network is one flat subnet, and the
alternative is a dependency (`zeroconf`) or a system service (`avahi`) on a
machine chosen for being small. The cost is that broadcast does not cross a
router, so the dashboard has to sit on the same network as the displays. Where
the heuristics fail, `beacon.address` pins the target — including unicasting
straight at a dashboard's address.

### A note on access

With no `web.token` set, anyone who can reach a display can reconfigure it. That
is the right default for a closed range network and the wrong one for anything
else. Set a shared secret to require it:

```bash
sudo -u megalink /opt/megalink/venv/bin/megalink config --token "$(openssl rand -hex 16)"
megalink fleet --token <the same secret>
```

Reads stay open either way; only changes need the token. There are deliberately
no permissive CORS headers on the display API — the fleet dashboard talks to
displays from its own server, not from your browser, so a page you happen to be
visiting cannot reach them.

### What it costs

The display is one HTTPS connection to the feed plus two idle listeners: a few
MB of RSS and essentially no CPU between shots. It survives network drops and
range restarts on its own — the stream reconnects with a backoff, and the status
line shows how stale the data is, so a dead link is visible rather than silent.

```bash
journalctl -u megalink-display -f
```

**Not yet run on real hardware.** Everything above is exercised by the test
suite and by running the display, both web servers and the beacon together on a
development machine; the install and provisioning scripts are checked for syntax
and their copy and offline-install paths verified locally, but nothing has run
on an actual Pi.

If something needs fixing on the first go, expect it to be the X-on-boot path
rather than the display itself — the Wi-Fi, the feed, the web page, the beacon
and the fleet dashboard are all plain networking and have been run end to end.
Starting a display server from a service is the part that varies between Pi OS
releases, which is why `--mode terminal` and the autologin fallback are both
documented above.

---

## Two HDMI outputs on a Pi 4 or Pi 5

Both boards have two micro-HDMI sockets. Raspberry Pi OS joins them into a
single X screen side by side, so showing a different firing point on each is a
matter of putting each window on the right part of that screen.

```bash
megalink config --lane 9 --lane2 10
```

`xrandr --listmonitors` is what says where each output starts:

```
Monitors: 2
 0: +*HDMI-1 1920/530x1080/300+0+0  HDMI-1
 1: +HDMI-2 1920/530x1080/300+1920+0  HDMI-2
```

The screens are ordered **left to right by position**, not in the order xrandr
happens to list them, so "the first screen" means the one on the left of the
bench and keeps meaning that when somebody swaps the cables over.

**The windows are placed directly, not by the window manager.** matchbox makes a
window fill the whole X screen, and across two sockets that is *both* monitors:
both firing points would pile onto one screen and leave the other blank. So a
two-screen display sets `overrideredirect` and its own geometry, and matchbox
leaves it alone. In browser mode the same problem applies to `--kiosk`, which is
swapped for `--start-fullscreen` plus an explicit `--window-position`.

Chromium also needs **a separate profile per window** — given one profile it
opens the second address as a tab in the existing window, on whichever screen
that window is already on.

It remains one process: one feed, one configuration page, one beacon, one entry
in the fleet. Each window follows its own setting, so a bulk renumber from the
dashboard moves them independently.

If `lane2` is set but only one screen is attached, the display says so in the
journal and shows one firing point. A Pi Zero has one socket, so the setting is
harmless there.

**Not yet verified on hardware.** The parsing, the placement and the two-window
lifecycle are covered by tests, but no Pi 4 or Pi 5 has run this yet. The first
thing to check is that both windows land on their own monitor rather than
stacking on one, and `xrandr --listmonitors` on the machine itself is the place
to start if they do not.
