"""Lightweight viewer for Megalink Live shooting scores.

Megalink Live (https://live.megalink.no/) publishes electronic-target scores
through public Firebase Realtime Databases. This package reads that feed over
plain HTTP -- snapshots and server-sent event streams, no SDK and no
dependencies -- decodes its display strings into numbers, and renders a single
firing point for a per-position display.

Two generations of the feed are in service and both are supported; see
:mod:`.v1` and :mod:`.v2`. :mod:`.gui` puts a target face and a score table in
a window; :mod:`.render` does the same for a terminal.

For an installed display -- a screen bolted to a firing point -- :mod:`.config`
holds what it should show, :mod:`.controller` follows that file while running,
:mod:`.webconfig` serves the page that edits it, :mod:`.beacon` announces the
display on the network, and :mod:`.fleet` is the dashboard that finds and
bulk-edits all of them.
"""

from __future__ import annotations

__version__ = "0.1.0"

from . import beacon, targets, v1, v2
from .client import (
    ARENA_DB,
    LIVE_DB,
    ActiveRange,
    Event,
    MegalinkClient,
    MegalinkError,
    Source,
    apply_event,
)
from .config import Config, ConfigError
from .controller import Controller
from .models import (
    MATCH,
    SHOOTOFF,
    SIGHT,
    Clock,
    LaneResult,
    LaneView,
    RangeInfo,
    Series,
    Shooter,
    Shot,
    Stage,
)
from .parse import ShotValue, Total, parse_shot_value, parse_total
from .targets import TargetFace
from .watch import RangeState, RangeWatcher, watch_lane

__all__ = [
    "ARENA_DB",
    "LIVE_DB",
    "MATCH",
    "SHOOTOFF",
    "SIGHT",
    "ActiveRange",
    "Clock",
    "Config",
    "ConfigError",
    "Controller",
    "Event",
    "LaneResult",
    "LaneView",
    "MegalinkClient",
    "MegalinkError",
    "RangeInfo",
    "RangeState",
    "RangeWatcher",
    "Series",
    "Shooter",
    "Shot",
    "ShotValue",
    "Source",
    "Stage",
    "TargetFace",
    "Total",
    "__version__",
    "apply_event",
    "beacon",
    "parse_shot_value",
    "parse_total",
    "targets",
    "v1",
    "v2",
    "watch_lane",
]
