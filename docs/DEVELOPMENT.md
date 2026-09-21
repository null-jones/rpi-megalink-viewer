# Development

```bash
make check     # lint, format check, and the test suite
make test
make format
```

Tests run entirely from snapshots of real ranges in `tests/data/` — captured live,
covering both protocols, integer and decimal scoring, split cards, unnamed
shooters and empty lanes — so nothing touches the network.

### Drawing order

The face is painted the way Megalink paints one — paper, black aiming area,
centre disc — then the ring numbers, then the shots, and then the ring lines
*over* the shots. That order is the whole trick: the ring numbers belong to the
paper, so a shot lands on top of them the way it does on a real target, and the
rings stay unbroken across a group, so a shot can still be judged against them, while each marker stays a
solid colour. It avoids needing transparency at all, which matters because Tk
canvas items have no alpha channel — the only see-through fill the canvas offers
is a coarse stipple.

### Stopping cleanly

`megalink display` has to exit when systemd sends it SIGTERM, and a plain signal
handler does not manage it: a Python handler only runs when the interpreter
regains control, and Tk's main loop does not reliably give it back — on macOS it
never does, so the window would ignore the signal until it was killed. The stop
flag therefore *blocks* SIGINT and SIGTERM and waits for them in a dedicated
thread, which works whatever the main thread is busy with.

Blocking a signal is per-thread and inherited at creation, so this has to happen
before any other thread starts. Get that order wrong and the signal is delivered
to a thread that never blocked it, where the default action kills the process
before anything can react — which is exactly what happened the first time.

### Testing the window

The window is tested against a real Tk instance by reading back what was drawn:
that the rings are concentric, that their on-screen radii are in the same ratio as
their millimetre radii, that each shot lands at the pixel its coordinates imply
(with the y axis flipped, since a target's y grows upward and a canvas's does
not), and that ring numbers appear on the axes the face specifies and are dropped
rather than overlapped when the rings are too close together to fit them. Those
tests skip themselves where Tk cannot open a display, so a headless CI still
passes.

The drawing order is tested directly rather than inferred from a screenshot: a
Tk canvas is a display list, and `find_all` returns items in the order they were
painted, so the tests assert that every ring line sits above every marker and
that markers sit above the face and the ring numbers. The scaling is tested by
resizing the window and asserting every type size — and the table row height,
which would otherwise clip the larger type — grows with it.

The stream handling is tested against the two event shapes a range in progress
actually sends, captured off a live 25m range:

```
patch /data/fp/5              {"cardTotals": {…, "total": "178-4x"}}
put   /data/fp/5/series/15    {…, "seriesTotals": {"total": "94-3x"}, "shots": […]}
```

That second one matters more than it looks. The `series` node arrives in the
initial snapshot as a **JSON array** — Firebase's encoding for an integer-keyed
map — and the event addresses it by index. Walking into it as though it were an
object, or as though it were empty, silently discards every series the lane had
accumulated, so a lane would appear to reset to one series on the first shot of
each new group. `as_mapping` in `client.py` exists for exactly this, and the
replay test in `tests/test_watch.py` pins it down.
