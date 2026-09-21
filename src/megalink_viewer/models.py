"""Protocol-neutral view of a Megalink range.

Megalink Live carries two generations of the same idea, and both are in use:

* **v1** (``protocolVersion`` ``0.0.1``) on the ``mllive`` database, where one
  node holds every range a club is running and scores hang off ``cards`` and
  ``results`` keyed ``<range>_<lane>``;
* **v2** (``protocolVersion`` ``2.0.1``) on the ``mllivearena`` database, where
  each range is its own node and each firing point carries its shooter, series,
  totals and timer together.

The classes here are what both shapes are decoded *into*, so the renderer and
the CLI never need to know which protocol a club is publishing. The adapters in
:mod:`.v1` and :mod:`.v2` do the translating.
"""

from __future__ import annotations

import math

from . import targets
from .parse import Number, ShotValue, Total

#: Series kinds. v2 names these directly; v1 has a ``sight`` flag we map onto
#: ``SIGHT``/``MATCH``.
SIGHT = "SIGHT"
MATCH = "MATCH"
SHOOTOFF = "SHOOTOFF"

#: Clock stage kinds.
STAGE_COMMAND = "COMMAND"
STAGE_SHOOT = "SHOOT"


class Shot:
    """One shot."""

    __slots__ = ("elapsed", "figure", "plotted", "value", "x", "y")

    def __init__(
        self,
        value: ShotValue,
        x: float | None = None,
        y: float | None = None,
        plotted: bool = True,
        elapsed: str = "",
        figure: int | None = None,
    ) -> None:
        self.value = value
        #: Position on the target. v1 reports a fraction of the target's width,
        #: v2 reports millimetres, so these are only meaningful next to the
        #: gauge size from the same protocol -- they are carried through rather
        #: than converted.
        self.x = x
        self.y = y
        #: False when the range told us not to plot this shot.
        self.plotted = plotted
        #: Pre-formatted time into the series, e.g. ``"10.43s"``; v2 only.
        self.elapsed = elapsed
        #: Target figure number, for disciplines with several figures.
        self.figure = figure

    def __repr__(self) -> str:
        return f"Shot({self.value.display()!r})"


class Series:
    """One series: a named group of shots with its own total."""

    __slots__ = (
        "complete",
        "decimal",
        "gauge_radius",
        "index",
        "kind",
        "name",
        "offset",
        "parent",
        "protocol",
        "shots",
        "size",
        "split_size",
        "splits",
        "target_id",
        "target_name",
        "total",
    )

    def __init__(
        self,
        index: int,
        name: str,
        kind: str,
        decimal: bool,
        complete: bool,
        shots: list[Shot],
        total: Total,
        splits: list[Total] | None = None,
        target_id: str = "",
        target_name: str = "",
        protocol: int = 0,
        gauge_radius: float | None = None,
        size: int = 0,
        split_size: int = 0,
        offset: int = 0,
        parent: Series | None = None,
    ) -> None:
        self.index = index
        self.name = name
        #: :data:`SIGHT`, :data:`MATCH` or :data:`SHOOTOFF`.
        self.kind = kind
        #: Whether the series scores to a tenth of a ring.
        self.decimal = decimal
        self.complete = complete
        self.shots = shots
        #: The total as the range publishes it. v2 publishes a dependable
        #: per-series figure; on v1 this is whatever the range put in ``sum``,
        #: which on a split card is the current group of ten rather than the
        #: whole series -- compare :attr:`shot_sum` when that matters.
        self.total = total
        self.splits = splits or []
        self.target_id = target_id
        self.target_name = target_name
        #: Which feed generation this came from, which sets the unit that
        #: ``Shot.x``/``y`` are in.
        self.protocol = protocol
        #: Shot-hole radius in millimetres, where the range reported one.
        self.gauge_radius = gauge_radius
        #: How many shots the range says this series holds. Some ranges use a
        #: round 100 to mean "as many as the shooter likes", for sighters.
        self.size = size
        #: How many shots the range's own display puts in a group -- ISSF's ten,
        #: usually. A series longer than this is *shown* in groups of it.
        self.split_size = split_size
        #: How many shots of the parent series came before this group, so a
        #: group can number its shots as the card does: 11, 12, ... not 1, 2.
        self.offset = offset
        #: The series this is a group of, or ``None`` for a whole one.
        self.parent = parent

    @property
    def sight(self) -> bool:
        """Whether this is a sighting series, which does not count."""
        return self.kind == SIGHT

    @property
    def inner_count(self) -> int:
        return sum(1 for shot in self.shots if shot.value.inner)

    @property
    def shot_sum(self) -> Number | None:
        """The series score added up from its own shots.

        Applies this series' scoring rule to each shot, so an integer-scored
        series truncates to the ring. This is a cross-check on
        :attr:`total`, not a replacement for it: on v2 the ``shots`` list is a
        rolling display window that can hold more shots than the series scores,
        so the range's own figure remains the authority.
        """
        decimal = self.decimal
        values = [
            value
            for value in (shot.value.ring(decimal) for shot in self.shots)
            if value is not None
        ]
        if not values:
            return None
        summed = sum(values)
        return round(summed, 1) if decimal else int(summed)

    def shot_display(self) -> list[str]:
        """Each shot as text, keeping the precision Megalink sent."""
        return [shot.value.display() for shot in self.shots]

    @property
    def face(self) -> targets.TargetFace | None:
        """The target face this series was shot on, if it is a ring target."""
        return targets.face(self.target_id)

    @property
    def mm_per_unit(self) -> float:
        """Millimetres per unit of the coordinates in :attr:`shots`.

        v2 reports millimetres; v1 reports a fraction of the target's outer
        radius, so the scale depends on knowing the face.
        """
        if self.protocol == 2:
            return 1.0
        face = self.face
        return face.scale_for(1) if face is not None else 1.0

    @property
    def hole_radius(self) -> float:
        """Shot-hole radius in millimetres, from the range or from the face."""
        if self.gauge_radius:
            return self.gauge_radius
        face = self.face
        return face.gauge_radius if face is not None else 0.0

    def positions_mm(self) -> list[tuple[Shot, float, float]]:
        """Each plottable shot with its position converted to millimetres."""
        scale = self.mm_per_unit
        out = []
        for shot in self.shots:
            if not shot.plotted or shot.x is None or shot.y is None:
                continue
            out.append((shot, shot.x * scale, shot.y * scale))
        return out

    def groups(self) -> list[Series]:
        """This series as the range's own display divides it.

        Ranges publish an ISSF 60-shot card as *one* series of sixty with a
        ``maxSeriesSize`` of ten, and their display breaks it into the six groups
        everyone reads it as -- with the group totals in ``splitTotals``. A range
        that already publishes its series at that size (a 25m card of six fives)
        needs no dividing and comes back as itself.

        The groups keep the parent's name, because that is what the range calls
        what is being shot. The group totals belong in the strip above the
        target, which is where Megalink puts them.
        """
        size = self.split_size
        if size <= 0 or self.size <= size:
            return [self]
        # Trust the published group totals only when they account for the whole
        # series. A sighting series is often declared as a round 100 shots with
        # ten group totals whatever the group size really is, and dividing 100
        # by five to get twenty groups would not match those ten totals.
        totals = self.splits if len(self.splits) * size == self.size else []
        count = len(totals) or max(1, math.ceil(len(self.shots) / size))
        groups = []
        for number in range(count):
            first = number * size
            groups.append(
                Series(
                    index=self.index,
                    name=self.name,
                    kind=self.kind,
                    decimal=self.decimal,
                    complete=self.complete or first + size <= len(self.shots),
                    shots=self.shots[first : first + size],
                    total=totals[number] if number < len(totals) else self.total,
                    target_id=self.target_id,
                    target_name=self.target_name,
                    protocol=self.protocol,
                    gauge_radius=self.gauge_radius,
                    size=size,
                    offset=first,
                    parent=self,
                )
            )
        return groups

    def radial_stats(self) -> tuple[float, float] | None:
        """Mean and standard deviation of the shots' distance from the group's
        own centre, in millimetres.

        The dispersion of the group, stated as the two numbers that describe
        any spread: how far a shot lands from the middle on average, and how
        much that varies. Measured from the mean point of impact rather than
        from the centre of the target, so it says how *tight* the group is
        independently of where it sits -- :meth:`mpi_mm` says where.

        The standard deviation is the population one, dividing by ``n``: these
        shots are the group being described, not a sample from which some
        larger truth is being estimated. That is what makes the mu and sigma
        the display labels them with the right symbols.

        ``None`` until there are two shots to spread between.
        """
        placed = self.positions_mm()
        if len(placed) < 2:
            return None
        centre = self.mpi_mm()
        if centre is None:  # pragma: no cover - positions_mm decides both
            return None
        radii = [math.hypot(x - centre[0], y - centre[1]) for _shot, x, y in placed]
        mean = sum(radii) / len(radii)
        variance = sum((radius - mean) ** 2 for radius in radii) / len(radii)
        return mean, math.sqrt(variance)

    def mpi_mm(self) -> tuple[float, float] | None:
        """Mean point of impact, in millimetres from the centre of the target.

        Where the group actually sits, which is what a sight adjustment is made
        against -- a tight group in the wrong place reads on the card exactly
        like a loose one in the right place.
        """
        placed = self.positions_mm()
        if not placed:
            return None
        return (
            sum(x for _shot, x, _y in placed) / len(placed),
            sum(y for _shot, _x, y in placed) / len(placed),
        )

    def __repr__(self) -> str:
        return f"Series({self.index}, {self.name!r}, {len(self.shots)} shots)"


class Shooter:
    """Whoever is on the firing point."""

    __slots__ = ("category", "class_name", "club", "gun", "id_number", "name")

    def __init__(
        self,
        name: str = "",
        club: str = "",
        class_name: str = "",
        category: str = "",
        gun: str = "",
        id_number: str = "",
    ) -> None:
        self.name = name
        self.club = club
        self.class_name = class_name
        self.category = category
        self.gun = gun
        self.id_number = id_number

    @property
    def occupied(self) -> bool:
        """Whether anyone is identified on this point.

        Plenty of clubs publish scores without names, so an unoccupied card is
        not the same as an unused lane.
        """
        return bool(self.name or self.club)

    def __repr__(self) -> str:
        return f"Shooter({self.name!r}, {self.club!r})"


class Stage:
    """One stage of a range clock: a preparation command, or shooting time."""

    __slots__ = ("counting_up", "offset", "start_count", "type")

    def __init__(self, type: str, counting_up: bool, offset: int = 0, start_count: int = 0) -> None:
        self.type = type
        self.counting_up = counting_up
        self.offset = offset
        #: Stage length in milliseconds; 0 means open-ended.
        self.start_count = start_count

    def __repr__(self) -> str:
        return f"Stage({self.type!r}, {self.start_count}ms)"


class Clock:
    """A range clock, with the offset needed to read it against server time."""

    __slots__ = ("running", "skew", "stages", "start_ms")

    def __init__(
        self,
        running: bool = False,
        start_ms: int | None = None,
        stages: list[Stage] | None = None,
        skew: Number = 0,
    ) -> None:
        self.running = running
        #: When the clock started, on the range's own clock. ``None`` when the
        #: range reports a clock it has never started.
        self.start_ms = start_ms
        self.stages = stages or []
        #: Range clock minus server clock, in milliseconds.
        self.skew = skew or 0

    def elapsed_ms(self, now_ms: float) -> float | None:
        if self.start_ms is None:
            return None
        return now_ms - self.skew - self.start_ms

    def remaining_ms(self, now_ms: float) -> int | None:
        """The number the range display is showing, in milliseconds.

        Reimplements the web client's stage walk: a counting-up stage shows the
        time since it began, a counting-down stage the time left, and an
        open-ended ``COMMAND`` stage is a placeholder to be skipped. Returns
        ``None`` when there is nothing to show, and ``0`` once the clock has run
        past its last stage.
        """
        if not self.running:
            return None
        elapsed = self.elapsed_ms(now_ms)
        if elapsed is None:
            return None
        for stage in self.stages:
            limit = stage.offset + stage.start_count
            if stage.counting_up:
                if stage.type == STAGE_COMMAND and limit == 0:
                    continue
                if elapsed > limit:
                    return int(elapsed - stage.offset)
            elif elapsed < limit:
                # The client rounds up so a fresh second shows immediately.
                return int(limit - elapsed + 999)
        return 0

    def __repr__(self) -> str:
        return f"Clock(running={self.running})"


class RangeInfo:
    """A range: a set of firing points shooting the same relay."""

    __slots__ = (
        "event",
        "gun_type",
        "host_name",
        "key",
        "name",
        "practice",
        "protocol",
        "relay",
        "title",
        "version",
    )

    def __init__(
        self,
        key: str,
        name: str,
        host_name: str = "",
        gun_type: str = "",
        relay: int = 0,
        event: str = "",
        title: str = "",
        protocol: int = 0,
        version: str = "",
        practice: bool = False,
    ) -> None:
        self.key = key
        self.name = name
        self.host_name = host_name
        #: ``RIFLE``, ``PISTOL``, ``ANY`` or empty.
        self.gun_type = gun_type
        self.relay = relay
        #: Competition name, where the range publishes one.
        self.event = event
        #: The range's own caption for what is being shot.
        self.title = title
        #: 1 or 2 -- which generation of the feed this came from.
        self.protocol = protocol
        #: Megalink software version running the range.
        self.version = version
        self.practice = practice

    def __repr__(self) -> str:
        return f"RangeInfo({self.key!r}, {self.name!r}, relay={self.relay}, v{self.protocol})"


class LaneResult:
    """The scores on one firing point."""

    __slots__ = ("active_series_index", "clock", "series", "series_summary", "total")

    def __init__(
        self,
        total: Total,
        series: list[Series],
        active_series_index: int | None = None,
        clock: Clock | None = None,
        series_summary: list[tuple[str, Total]] | None = None,
    ) -> None:
        self.total = total
        self.series = series
        self.active_series_index = active_series_index
        self.clock = clock or Clock()
        #: ``(name, total)`` per series as the range itself summarises the card;
        #: v2 publishes this, v1 does not.
        self.series_summary = series_summary or []

    @property
    def active_series(self) -> Series | None:
        if self.active_series_index is not None:
            for series in self.series:
                if series.index == self.active_series_index:
                    return series
        return self.series[-1] if self.series else None

    @property
    def display_series(self) -> list[Series]:
        """The card as a display reads it: every series in groups of ten.

        A range that publishes a 60-shot card as one long series is showing six
        groups of ten on its own screen, and so should this. Series the range
        already publishes at group size come through untouched.

        Groups past the last shot are left out. They are still in
        :meth:`Series.groups`, because the strip shows the whole card's shape --
        but there is nothing to look at on an empty target, and stepping back
        through a card should not mean walking past eight blank sighting groups
        first.
        """
        shown = []
        for series in self.series:
            groups = series.groups()
            last = 0
            for position, group in enumerate(groups):
                if group.shots:
                    last = position
            shown.extend(groups[: last + 1])
        return shown

    @property
    def active_display_group(self) -> Series | None:
        """The group being shot: the last one with a shot in it.

        Not the last group of the card -- a shooter halfway through their
        second ten is on the second group, and the four empty ones after it are
        not what they want to look at.
        """
        series = self.active_series
        if series is None:
            return None
        groups = series.groups()
        for group in reversed(groups):
            if group.shots:
                return group
        return groups[0] if groups else None

    @property
    def counting_series(self) -> list[Series]:
        """Series that count towards the total, i.e. excluding sighters."""
        return [s for s in self.series if not s.sight]

    @property
    def shot_count(self) -> int:
        return sum(len(s.shots) for s in self.series)

    @property
    def counting_shot_count(self) -> int:
        return sum(len(s.shots) for s in self.counting_series)

    def __repr__(self) -> str:
        return f"LaneResult(total={self.total.display()!r}, series={len(self.series)})"


class LaneView:
    """Everything a per-position display needs for one firing point."""

    __slots__ = ("lane", "not_in_use", "range", "result", "shooter")

    def __init__(
        self,
        lane: str,
        range: RangeInfo | None = None,
        shooter: Shooter | None = None,
        result: LaneResult | None = None,
        not_in_use: bool = False,
    ) -> None:
        self.lane = str(lane)
        self.range = range
        self.shooter = shooter
        self.result = result
        #: The range has marked this firing point as out of use.
        self.not_in_use = not_in_use

    @property
    def shooter_name(self) -> str:
        """A display name, falling back through the fields clubs leave blank."""
        if self.shooter is None:
            return f"Lane {self.lane}"
        return self.shooter.name or self.shooter.club or f"Lane {self.lane}"

    @property
    def present(self) -> bool:
        """Whether the feed knows anything about this firing point."""
        return self.result is not None or (self.shooter is not None and self.shooter.occupied)

    @property
    def idle(self) -> bool:
        """Whether nobody is shooting on this point.

        Either the range says so outright, or there is nobody named and nothing
        has been shot. A club that publishes no names still counts as busy the
        moment a shot lands.
        """
        if self.not_in_use:
            return True
        occupied = self.shooter is not None and self.shooter.occupied
        shots = self.result.shot_count if self.result is not None else 0
        return not occupied and shots == 0

    def __repr__(self) -> str:
        return f"LaneView(lane={self.lane!r}, shooter={self.shooter_name!r})"


def rank_on_relay(lane: str, views: dict[str, LaneView]) -> tuple[int, int] | None:
    """Where a firing point stands on the relay: ``(place, field)``.

    Ranked on the card total, inner tens breaking a tie, and counting only the
    points actually being shot -- an empty lane is not somebody in last place.
    Ties share a place and consume the ones below, so two seconds are followed
    by a fourth, as a results list has it.

    ``None`` when this point has no total of its own, or when it is the only
    one on the relay and a placing would say nothing.
    """
    scores: dict[str, tuple[float, int]] = {}
    for key, view in views.items():
        if view.idle or view.result is None:
            continue
        total = view.result.total
        if total.value is None:
            continue
        scores[str(key)] = (float(total.value), total.inner or 0)
    mine = scores.get(str(lane))
    if mine is None or len(scores) < 2:
        return None
    ahead = sum(1 for other in scores.values() if other > mine)
    return ahead + 1, len(scores)
