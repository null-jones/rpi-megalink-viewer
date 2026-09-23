"""Adapter for the current Megalink Live feed (``protocolVersion`` 2.0.1).

Lives on the ``mllivearena`` database, one node per range::

    data/<host>/<range>/data/range          range metadata
    data/<host>/<range>/data/fp/<point>     shooter, series, totals and timer
    data/<host>/<range>/data/fpList         the firing points, in order
    data/<range>/.../namelist/<point>       name, club and class per point
    data/<host>/<range>/serverClockSkew     range clock minus server clock

This generation is friendlier than v1: each firing point is self-contained, the
range publishes its own per-series and per-card totals, and the clock offset is
a plain millisecond figure. It is also what live.megalink.no renders today.
"""

from __future__ import annotations

import contextlib
from typing import Any

from .models import (
    MATCH,
    Clock,
    LaneResult,
    LaneView,
    RangeInfo,
    Series,
    Shooter,
    Shot,
    Stage,
)
from .parse import Total, entries, parse_shot_value, parse_total, strip_style

PROTOCOL = 2

#: Series kinds the range publishes directly.
_KINDS = {"SIGHT", "MATCH", "SHOOTOFF"}


def _range_node(tree: dict[str, Any]) -> dict[str, Any]:
    data = tree.get("data")
    if not isinstance(data, dict):
        return {}
    node = data.get("range")
    return node if isinstance(node, dict) else {}


def _skew(tree: dict[str, Any]) -> float:
    value = tree.get("serverClockSkew")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _stages(raw: Any) -> list[Stage]:
    return [
        Stage(
            type=str(stage.get("type") or ""),
            counting_up=bool(stage.get("countingUp")),
            offset=int(stage.get("offset") or 0),
            start_count=int(stage.get("startCount") or 0),
        )
        for _, stage in entries(raw)
        if isinstance(stage, dict)
    ]


def _clock(raw: Any, skew: float) -> Clock:
    raw = raw if isinstance(raw, dict) else {}
    # v2 sends the start time as a string holding epoch milliseconds.
    try:
        start = int(raw.get("timerStart"))
    except (TypeError, ValueError):
        start = 0
    return Clock(
        running=bool(raw.get("running")),
        start_ms=start or None,
        stages=_stages(raw.get("stages")),
        skew=skew,
    )


def _int(value: Any) -> int:
    """A whole number from the feed, or 0 -- these arrive as ints or strings."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _series(index: int, raw: dict[str, Any], split_size: int = 0) -> Series:
    kind = str(raw.get("type") or "").upper()
    totals = raw.get("seriesTotals")
    totals = totals if isinstance(totals, dict) else {}
    shots = []
    for _, shot in entries(raw.get("shots")):
        if not isinstance(shot, dict):
            continue
        shots.append(
            Shot(
                value=parse_shot_value(shot.get("v")),
                x=shot.get("x"),
                y=shot.get("y"),
                # `displayPlot: false` is this protocol's "do not plot".
                plotted=shot.get("displayPlot") is not False,
                elapsed=str(shot.get("time") or ""),
                figure=shot.get("figure"),
            )
        )
    return Series(
        index=index,
        name=strip_style(raw.get("name")).strip(),
        kind=kind if kind in _KINDS else MATCH,
        decimal=bool(raw.get("decimal")),
        complete=bool(raw.get("complete")),
        shots=shots,
        # Unlike v1, the range publishes a trustworthy per-series total.
        total=parse_total(totals.get("total")),
        splits=[parse_total(v) for _, v in entries(totals.get("splitTotals"))],
        target_id=str(raw.get("targetId") or ""),
        target_name=strip_style(raw.get("targetName")).strip(),
        protocol=PROTOCOL,
        # v2 states the shot-hole radius directly, in millimetres.
        gauge_radius=raw.get("gaugeR") if isinstance(raw.get("gaugeR"), (int, float)) else None,
        size=_int(raw.get("seriesSize")),
        # Card-scoped: the range states one group size for the whole card.
        split_size=split_size,
    )


def _card_summary(raw: Any) -> list[tuple[str, Total]]:
    """The range's own per-series summary of a card."""
    raw = raw if isinstance(raw, dict) else {}
    names = [strip_style(v).strip() for _, v in entries(raw.get("seriesName"))]
    totals = [parse_total(v) for _, v in entries(raw.get("seriesTotals"))]
    # Not strict: a feed caught between two writes can briefly hold more names
    # than totals, and that should show the pairs it has rather than take the
    # display down.
    return list(zip(names, totals, strict=False))


def ranges(tree: dict[str, Any], key: str = "") -> list[RangeInfo]:
    """The range this node describes, as a one-item list.

    v2 addresses one range per node, so this exists to mirror :func:`.v1.ranges`
    and keep the caller protocol-agnostic.
    """
    node = _range_node(tree)
    if not node:
        return []
    data = tree.get("data") if isinstance(tree.get("data"), dict) else {}
    name = strip_style(node.get("rangeName")).strip()
    return [
        RangeInfo(
            key=key or name,
            name=name or key,
            host_name=strip_style(node.get("userName")).strip(),
            gun_type=str((data or {}).get("gunType") or ""),
            relay=_relay(tree),
            event=strip_style(node.get("eventId")).strip(),
            title=strip_style(node.get("displayTitle") or node.get("programName")).strip(),
            protocol=PROTOCOL,
            version=str(node.get("version") or ""),
        )
    ]


def _relay(tree: dict[str, Any]) -> int:
    """The relay the range is on, taken from whichever point reports one."""
    for _, point in _points(tree):
        try:
            relay = int(point.get("relay"))
        except (TypeError, ValueError):
            continue
        if relay:
            return relay
    return 0


def _points(tree: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    data = tree.get("data")
    if not isinstance(data, dict):
        return []
    return [(k, v) for k, v in entries(data.get("fp")) if isinstance(v, dict)]


def lanes(tree: dict[str, Any], range_key: str = "") -> list[str]:
    """Firing-point numbers on this range, in the order the range lists them."""
    data = tree.get("data") if isinstance(tree.get("data"), dict) else {}
    listed = [str(v) for _, v in entries((data or {}).get("fpList")) if v is not None]
    if listed:
        return listed
    found = [str(point.get("fp") or key) for key, point in _points(tree)]
    return sorted(found, key=lambda v: (0, int(v)) if v.isdigit() else (1, 0))


def _namelist_entry(tree: dict[str, Any], lane: str) -> dict[str, Any]:
    for key, value in entries(tree.get("namelist")):
        if not isinstance(value, dict):
            continue
        if str(value.get("fp") or key) == lane:
            return value
    return {}


def lane_view(tree: dict[str, Any], range_key: str, lane: int | str) -> LaneView:
    """Decode one firing point."""
    lane = str(lane)
    range_info = next(iter(ranges(tree, range_key)), None)
    skew = _skew(tree)

    point = None
    for key, candidate in _points(tree):
        if str(candidate.get("fp") or key) == lane:
            point = candidate
            break

    names = _namelist_entry(tree, lane)
    raw_shooter = point.get("shooter") if point else None
    raw_shooter = raw_shooter if isinstance(raw_shooter, dict) else {}
    shooter = Shooter(
        # The firing point carries the live name; the namelist is the entry
        # list, and fills in when the point has not been written yet.
        name=strip_style(raw_shooter.get("name") or names.get("name")).strip(),
        club=strip_style(raw_shooter.get("club") or names.get("club")).strip(),
        class_name=strip_style(raw_shooter.get("class") or names.get("class")).strip(),
        category=strip_style(raw_shooter.get("category")).strip(),
        gun=str(raw_shooter.get("gun") or (range_info.gun_type if range_info else "")),
        id_number=str(raw_shooter.get("IDnr") or ""),
    )

    if point is None:
        return LaneView(lane, range_info, shooter if shooter.occupied else None, None)

    not_in_use = bool(point.get("laneNotActive"))

    if range_info is not None and not range_info.relay:
        with contextlib.suppress(TypeError, ValueError):
            range_info.relay = int(point.get("relay") or 0)
    if range_info is not None and not range_info.title:
        range_info.title = strip_style(point.get("displayTitle")).strip()

    split_size = _int(point.get("maxSeriesSize"))
    series = [
        _series(int(index), raw, split_size)
        for index, raw in entries(point.get("series"))
        if index.isdigit() and isinstance(raw, dict)
    ]
    card_totals = point.get("cardTotals")
    card_totals = card_totals if isinstance(card_totals, dict) else {}
    try:
        active_index = int(point.get("activeSeries"))
    except (TypeError, ValueError):
        active_index = None

    result = LaneResult(
        total=parse_total(card_totals.get("total")),
        series=series,
        active_series_index=active_index,
        clock=_clock(point.get("timer"), skew),
        series_summary=_card_summary(card_totals),
    )
    return LaneView(lane, range_info, shooter, result, not_in_use=not_in_use)


def lane_not_active(tree: dict[str, Any], lane: int | str) -> bool:
    """Whether the range has marked a firing point as out of use."""
    lane = str(lane)
    for key, point in _points(tree):
        if str(point.get("fp") or key) == lane:
            return bool(point.get("laneNotActive"))
    return False
