# Using it as a library

The package is importable, and the parsing is separate from the drawing.

```python
from megalink_viewer import MegalinkClient, RangeWatcher

client = MegalinkClient()
source = client.resolve("stord-pk", "1-10")     # picks the protocol for you

with RangeWatcher(source, client) as state:
    state.lane_view(9)                          # -> LaneView
```

`LaneView` gives you `range`, `shooter` and `result`; `result` has `total`,
`series`, `clock` and `active_series`; each `Series` has `shots`, `total` and
`inner_count`. Everything is decoded — numbers are numbers, not display strings.

For an installed display there is a layer above that: a `Config` says what to
show, and a `Controller` follows it while running — swapping the feed
subscription underneath the renderer when the file changes.

```python
from megalink_viewer import Controller

with Controller() as display:      # reads /etc/megalink/display.json and watches it
    display.lane_view()            # -> LaneView for the configured firing point
    display.status()               # -> what a dashboard shows
    display.set_lane(7)            # persists, so it survives a reboot
```

For plotting, a `Series` also knows its target:

```python
series = state.lane_view(9).result.active_series
series.face                 # -> TargetFace: ring radii, colours, numbering
series.positions_mm()        # -> [(Shot, x_mm, y_mm), ...]
series.hole_radius           # shot-hole radius in mm
series.face.score_at(12.4)   # what a shot 12.4mm out would score
```
