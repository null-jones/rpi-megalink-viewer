"""Adapter for the original Megalink Live feed (``protocolVersion`` 0.0.1).

Lives on the ``mllive`` database, where one node per club holds every range::

    data/<host>/ranges/<range>          range metadata and the master clock
    data/<host>/cards/<range>_<lane>    who is on the firing point
    data/<host>/results/<range>_<lane>  their series, shots and totals
    data/<host>/clockSkew/<range>       range clock minus server clock

This protocol's totals are less regular than v2's, and vary with the program a
range is running: ``series.sum`` is the whole series on some cards and only the
current group of ten on cards that split a long series, and some ranges publish
a sighting total of zero (flagged italic) even though the shots are there. Since
there is no rule that fits every range, the numbers are reported as the range
publishes them, and :attr:`~.models.Series.shot_sum` is available alongside for
callers that would rather add the shots up themselves. ``splits`` is card-scoped
rather than series-scoped here, so it is passed through and never summed.
"""

from __future__ import annotations

from typing import Any

from . import targets
from .models import (
    MATCH,
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
from .parse import entries, parse_shot_value, parse_total, strip_style

PROTOCOL = 1


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
    timestamp = raw.get("timestamp")
    try:
        start = int(timestamp)
    except (TypeError, ValueError):
        start = 0
    return Clock(
        running=bool(raw.get("running")),
        # A range that reports `running` with `timestamp: 0` has a clock
        # configured but never started; that is not a clock running since 1970.
        start_ms=start or None,
        stages=_stages(raw.get("stages")),
        skew=skew,
    )


def _skew(tree: dict[str, Any], range_key: str) -> float:
    for key, value in entries(tree.get("clockSkew")):
        if key == range_key and isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _gauge_radius(raw: dict[str, Any]) -> float | None:
    """Shot-hole radius in millimetres, from this protocol's ``gaugeSize``.

    v1 reports the hole as a fraction of the target's outer *diameter*, while
    positions are a fraction of its outer *radius* -- so converting needs the
    face, and this returns ``None`` when the face is unknown and lets the caller
    fall back to the projectile diameter.
    """
    size = raw.get("gaugeSize")
    if not isinstance(size, (int, float)) or size <= 0:
        return None
    face = targets.face(str(raw.get("targetId") or ""))
    if face is None:
        return None
    return size * face.outer_radius


def _series(index: int, raw: dict[str, Any]) -> Series:
    decimal = bool(raw.get("decimal"))
    shots = [
        Shot(
            value=parse_shot_value(shot.get("value")),
            x=shot.get("x"),
            y=shot.get("y"),
            plotted=not shot.get("noPlot") and shot.get("x") is not None,
        )
        for _, shot in entries(raw.get("shots"))
        if isinstance(shot, dict)
    ]
    return Series(
        index=index,
        # Megalink pads series names with a leading space.
        name=strip_style(raw.get("name")).strip(),
        kind=SIGHT if raw.get("sight") else MATCH,
        decimal=decimal,
        complete=bool(raw.get("complete")),
        shots=shots,
        total=parse_total(raw.get("sum")),
        splits=[parse_total(v) for _, v in entries(raw.get("splits"))],
        target_id=str(raw.get("targetId") or ""),
        protocol=PROTOCOL,
        gauge_radius=_gauge_radius(raw),
    )


def ranges(tree: dict[str, Any]) -> list[RangeInfo]:
    """Every range a club is publishing, sorted by name."""
    found = []
    for key, raw in entries(tree.get("ranges")):
        if not isinstance(raw, dict):
            continue
        found.append(
            RangeInfo(
                key=key,
                name=strip_style(raw.get("name")).strip() or key,
                host_name=strip_style(raw.get("host")).strip(),
                gun_type=str(raw.get("gunType") or ""),
                relay=int(raw.get("relay") or 0),
                event=strip_style(raw.get("relayName")).strip(),
                title=strip_style(raw.get("seriesName")).strip(),
                protocol=PROTOCOL,
                version=str(raw.get("version") or ""),
                practice=bool(raw.get("practice")),
            )
        )
    found.sort(key=lambda r: r.name.lower())
    return found


def lanes(tree: dict[str, Any], range_key: str) -> list[str]:
    """Firing-point numbers on a range, in numeric order."""
    prefix = f"{range_key}_"
    found = set()
    for section in ("cards", "results"):
        for key, _ in entries(tree.get(section)):
            if key.startswith(prefix):
                found.add(key[len(prefix) :])
    return sorted(found, key=lambda v: (0, int(v)) if v.isdigit() else (1, 0))


def _section_get(section: Any, key: str) -> dict[str, Any] | None:
    if isinstance(section, dict):
        value = section.get(key)
        return value if isinstance(value, dict) else None
    for candidate, value in entries(section):
        if candidate == key and isinstance(value, dict):
            return value
    return None


def lane_view(tree: dict[str, Any], range_key: str, lane: int | str) -> LaneView:
    """Decode one firing point."""
    range_info = next((r for r in ranges(tree) if r.key == range_key), None)
    skew = _skew(tree, range_key)
    key = f"{range_key}_{lane}"

    card = _section_get(tree.get("cards"), key)
    shooter = None
    if card is not None:
        shooter = Shooter(
            name=strip_style(card.get("name")).strip(),
            club=strip_style(card.get("club")).strip(),
            class_name=strip_style(card.get("className")).strip(),
            category=strip_style(card.get("category")).strip(),
            gun=range_info.gun_type if range_info else "",
        )

    raw_result = _section_get(tree.get("results"), key)
    result = None
    if raw_result is not None:
        series = [
            _series(int(index), raw)
            for index, raw in entries(raw_result.get("series"))
            if index.isdigit() and isinstance(raw, dict)
        ]
        active = raw_result.get("activeSeries")
        try:
            active_index = int(active)
        except (TypeError, ValueError):
            active_index = None
        # A lane clock overrides the range clock when the range gives it stages.
        lane_clock = _clock(raw_result.get("clock"), skew)
        if not lane_clock.stages and range_info is not None:
            lane_clock = _clock((tree.get("ranges") or {}).get(range_key, {}).get("clock"), skew)
        result = LaneResult(
            total=parse_total(raw_result.get("totalSum")),
            series=series,
            active_series_index=active_index,
            clock=lane_clock,
        )

    return LaneView(str(lane), range_info, shooter, result)
