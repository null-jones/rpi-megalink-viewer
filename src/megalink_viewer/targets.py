"""Target faces: ring geometry, colours and coordinate scales.

Megalink's own viewer draws target faces from a table embedded in its bundle,
which `data/targets.json` is a trimmed copy of -- the ring-based faces, with the
irregular hunting and field silhouettes left out because this package plots
shots on rings rather than reproducing arbitrary artwork.

Each entry describes a face the way Megalink does:

* ``value`` gives the ring diameters in millimetres. ``ringLowHighX`` names the
  outermost and innermost rings and interpolates evenly between them;
  ``ringSpec`` lists each ring; ``x`` is the inner-ten ring, drawn as "ring 11"
  and negative when the face has no separate inner ring.
* ``draw`` says how to paint it: a backing disc out to some ring, the black
  aiming area out to another, an optional centre disc, which ring lines are
  black and which white, and which rings carry printed numbers.
* ``gauge`` is the projectile diameter, which is also how a shot's score is
  decided -- a shot counts the best ring its *edge* touches.

The two feed generations report shot positions differently, so
:meth:`TargetFace.scale_for` converts both into millimetres:

* v2 reports millimetres already;
* v1 reports a fraction of the target's outer **radius**.

Both were established by reproducing the scores the ranges publish from the
positions they publish -- see ``tests/test_targets.py``.
"""

from __future__ import annotations

import json
import math
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

_DATA = Path(__file__).parent / "data" / "targets.json"

#: v1 target identifiers mapped to the modern ones. Confirmed by matching each
#: legacy series' ``gaugeSize`` against ``gauge / outer diameter``.
LEGACY_IDS = {
    "INT_ISSF_RIFLE_10M": "ISSF10R",
    "INT_ISSF_PISTOL_10M": "ISSF10P",
    "INT_ISSF_SILHOUETTE_PISTOL": "ISSF25P_SMALL_RF",
    "NO_DFS_15M": "DFS15",
    "NO_DFS_100M": "DFS100",
    "NO_DFS_200M": "DFS200",
    "NO_DFS_300M": "DFS300",
    "DK_DDS_RIFLE_50M_M90": "DGI_M90",
    "DK_DDS_RIFLE_15M_M84": "DDSM84",
    "USA_RIFLE_50FT": "US_ISSF_50FT_R",
    "C_TGT_NO_DVF_REINDEER": "NO_DVF_REINDEER",
}

#: Faces whose drawn rings differ from the rings their scores are quoted on.
#: The Norwegian "3D-Score" 100m face is printed with its own ring spacing but
#: scored on the standard 100m rings.
SCORING_ALIASES = {"DFS100_3D": "DFS100"}

BLACK = "black"
WHITE = "white"

#: The ring number Megalink uses in its draw lists to mean the inner-ten ring.
INNER_RING = 11


class Ring:
    """One ring on a face."""

    __slots__ = ("color", "drawn", "number", "number_color", "numbered", "radius")

    def __init__(
        self,
        number: int,
        radius: float,
        drawn: bool = True,
        color: str = BLACK,
        numbered: bool = False,
        number_color: str = BLACK,
    ) -> None:
        self.number = number
        #: Radius in millimetres.
        self.radius = radius
        self.drawn = drawn
        #: Colour of the ring line.
        self.color = color
        self.numbered = numbered
        self.number_color = number_color

    def __repr__(self) -> str:
        return f"Ring({self.number}, r={self.radius:.2f}mm)"


class TargetFace:
    """A target face, ready to draw and to measure against."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw
        self.id: str = raw.get("id", "")
        self.name: str = raw.get("name", "")
        self.org: str = raw.get("org", "")
        self.gun: str = raw.get("gun", "")
        self.distance = raw.get("range")
        self.distance_unit: str = raw.get("rangeUnit", "")
        #: Projectile diameter in millimetres.
        self.gauge: float = float(raw.get("gauge") or 0.0)
        self._value: dict[str, Any] = raw.get("value") or {}
        self._draw: dict[str, Any] = raw.get("draw") or {}
        self._radii = self._ring_radii()

    # -- geometry ----------------------------------------------------------

    @property
    def gauge_radius(self) -> float:
        return self.gauge / 2.0

    @property
    def lowest_ring(self) -> int:
        """The outermost scoring ring, which is the lowest-numbered one."""
        return min(self._radii) if self._radii else 1

    @property
    def highest_ring(self) -> int:
        return max(self._radii) if self._radii else 10

    @property
    def outer_radius(self) -> float:
        """Radius of the outermost scoring ring, in millimetres."""
        return abs(self._radii[self.lowest_ring]) if self._radii else 0.0

    @property
    def inner_radius(self) -> float | None:
        """Radius of the inner-ten ring, or ``None`` if the face has none.

        A negative diameter in the table means the inner ten coincides with the
        ten ring and is not drawn separately.
        """
        x = self._value.get("x")
        if not isinstance(x, (int, float)) or x <= 0:
            return None
        return x / 2.0

    def _ring_radii(self) -> dict[int, float]:
        """Ring number to radius, keeping the table's sign for interpolation."""
        value = self._value
        kind = value.get("type")
        radii: dict[int, float] = {}

        if kind == "ringSpec":
            for key, diameter in value.items():
                if key.isdigit() and isinstance(diameter, (int, float)):
                    radii[int(key)] = diameter / 2.0
            if radii:
                known = sorted(radii)
                for number in range(known[0], known[-1] + 1):
                    if number in radii:
                        continue
                    below = max(k for k in known if k < number)
                    above = min(k for k in known if k > number)
                    span = above - below
                    if span:
                        radii[number] = (
                            radii[below] + (number - below) * (radii[above] - radii[below]) / span
                        )
            return radii

        low = int(value.get("low", 1))
        high = int(value.get("high", 10))
        outer, inner = value.get(str(low)), value.get(str(high))
        if not isinstance(outer, (int, float)) or not isinstance(inner, (int, float)):
            return radii
        if high == low:
            return {low: outer / 2.0}
        for number in range(low, high + 1):
            radii[number] = (outer + (number - low) * (inner - outer) / (high - low)) / 2.0
        return radii

    def radius(self, ring: int) -> float | None:
        """Radius of *ring* in millimetres, or ``None`` if the face has no such ring."""
        if ring == INNER_RING:
            return self.inner_radius
        value = self._radii.get(ring)
        return None if value is None else abs(value)

    def rings(self) -> list[Ring]:
        """Every ring to draw, outermost first.

        Colours and numbering come from the face's own ``draw`` spec, so a face
        renders the way Megalink renders it: dark lines over the white paper,
        light lines over the black aiming area.
        """
        line = self._draw.get("value") or {}
        numbers = self._draw.get("number") or {}
        black_lines = set(line.get(BLACK) or ())
        white_lines = set(line.get(WHITE) or ())
        black_numbers = set(numbers.get(BLACK) or ())
        white_numbers = set(numbers.get(WHITE) or ())

        out = []
        for number in sorted(self._radii):
            radius = self.radius(number)
            # A non-positive radius is a ring the face does not really draw.
            if radius is None or radius <= 0:
                continue
            drawn = number in black_lines or number in white_lines
            out.append(
                Ring(
                    number=number,
                    radius=radius,
                    drawn=drawn,
                    color=WHITE if number in white_lines else BLACK,
                    numbered=number in black_numbers or number in white_numbers,
                    number_color=WHITE if number in white_numbers else BLACK,
                )
            )
        inner = self.inner_radius
        if inner and (INNER_RING in black_lines or INNER_RING in white_lines):
            out.append(
                Ring(
                    number=INNER_RING,
                    radius=inner,
                    drawn=True,
                    color=WHITE if INNER_RING in white_lines else BLACK,
                )
            )
        return out

    # -- painting ----------------------------------------------------------

    def _disc(self, key: str) -> tuple[float, str] | None:
        spec = self._draw.get(key) or {}
        if spec.get("type") != "value":
            return None
        radius = self.radius(int(spec.get("value", 0)))
        if radius is None or radius <= 0:
            return None
        return radius, str(spec.get("fillColor") or WHITE)

    @property
    def backing(self) -> tuple[float, str] | None:
        """Radius and colour of the paper disc, if the face draws one."""
        return self._disc("target")

    @property
    def aiming_mark(self) -> tuple[float, str] | None:
        """Radius and colour of the black aiming area, if it is a plain disc.

        Faces whose aiming mark is an arbitrary shape (the 25m pistol
        silhouette, for instance) report ``None``; their rings still draw.
        """
        return self._disc("aim")

    @property
    def centre(self) -> tuple[float, str] | None:
        """Radius and colour of the centre disc, if the face draws one."""
        return self._disc("center")

    @property
    def number_layout(self) -> str:
        """Where the face prints its ring numbers: ``"hv"``, ``"v"`` or ``"h"``.

        The 25m pistol faces number only the vertical axis, for instance, so
        printing all four would not match the paper.
        """
        numbers = self._draw.get("number") or {}
        kind = str(numbers.get("type") or "hv").lower()
        return kind if kind in {"hv", "v", "h"} else "hv"

    def number_offsets(self) -> list[tuple[int, int]]:
        """Unit directions to print ring numbers along."""
        layout = self.number_layout
        vertical = [(0, -1), (0, 1)]
        horizontal = [(-1, 0), (1, 0)]
        if layout == "v":
            return vertical
        if layout == "h":
            return horizontal
        return vertical + horizontal

    @property
    def line_width(self) -> float:
        """Ring line width in millimetres."""
        line = self._draw.get("value") or {}
        width = line.get("line")
        return float(width) if isinstance(width, (int, float)) and width > 0 else 0.1

    @property
    def extent(self) -> float:
        """Radius in millimetres that a drawing needs to cover the whole face."""
        candidates = [self.outer_radius]
        backing = self.backing
        if backing:
            candidates.append(backing[0])
        return max(candidates) if candidates else 1.0

    @property
    def zoom_range(self) -> tuple[float, float] | None:
        """How far in and out this face may be viewed, as radii in millimetres.

        Megalink carries a zoom range per target: the smallest is a close view
        of the middle, the largest takes in the whole face with a little margin.
        Working it out from the rings alone would give a worse answer -- a 300m
        face and a 10m one want very different closest views, and the table
        already knows which.
        """
        spec = self._draw.get("zoom") or {}
        low, high = spec.get("min"), spec.get("max")
        if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
            return None
        if low <= 0 or high <= low:
            return None
        return low / 2.0, high / 2.0

    def view_radius(self, shots_radius: float = 0.0, margin: float = 1.35) -> float:
        """The radius to draw out to, given how far the shots reach.

        This is Megalink's auto-zoom: a tight group is shown close up, and the
        view opens out as the group spreads, never closer than the face's own
        minimum nor wider than the whole target. Without a zoom range in the
        table it falls back to showing everything.
        """
        span = max(0.0, shots_radius) * margin
        bounds = self.zoom_range
        if bounds is None:
            return max(span, self.extent)
        closest, widest = bounds
        return max(closest, min(widest, span))

    # -- coordinates -------------------------------------------------------

    def scale_for(self, protocol: int) -> float:
        """Millimetres per unit of the shot coordinates a protocol reports."""
        if protocol == 2:
            return 1.0
        # v1 reports a fraction of the outer radius.
        return self.outer_radius or 1.0

    # -- scoring -----------------------------------------------------------

    def score_at(self, radius_mm: float) -> float | None:
        """The score a shot centred *radius_mm* from the middle would get.

        A shot counts the best ring its edge touches, so the gauge radius comes
        off the distance first. Readings continue *above* the innermost ring
        rather than clamping, because that is how the decimal and DFS
        ``10.5``-style values work, but a shot outside the outermost ring scores
        zero -- a miss is a miss, not a negative number.

        This is not needed to display a card, since the feed sends the values.
        It is what pins the geometry down: recovering the published values from
        the published positions is the check that the table has been read
        correctly. See ``tests/test_targets.py``.
        """
        if len(self._radii) < 2:
            return None
        effective = max(0.0, radius_mm - self.gauge_radius)
        numbers = sorted(self._radii)
        low, high = numbers[0], numbers[-1]
        for number in range(high, low, -1):
            inner = abs(self._radii[number])
            outer = abs(self._radii[number - 1])
            if outer <= inner:
                continue
            if effective <= outer:
                return number - (effective - inner) / (outer - inner)
        if effective > abs(self._radii[low]):
            return 0.0
        inner = abs(self._radii[high])
        outer = abs(self._radii.get(high - 1, 0.0))
        if outer > inner:
            return high - (effective - inner) / (outer - inner)
        return float(high)

    def score_of(self, x: float, y: float, protocol: int = 2) -> float | None:
        scale = self.scale_for(protocol)
        return self.score_at(math.hypot(x * scale, y * scale))

    def __repr__(self) -> str:
        return f"TargetFace({self.id!r}, {self.name!r}, {len(self._radii)} rings)"


@lru_cache(maxsize=1)
def _table() -> dict[str, dict[str, Any]]:
    raw = json.loads(_DATA.read_text(encoding="utf-8"))
    return {entry["id"]: entry for entry in raw}


@cache
def face(target_id: str) -> TargetFace | None:
    """Look up a target face by identifier, from either feed generation.

    Returns ``None`` for a face this package does not carry -- the irregular
    hunting and field silhouettes, mostly -- so callers can fall back to
    plotting shots without a face behind them.
    """
    if not target_id:
        return None
    resolved = LEGACY_IDS.get(target_id, target_id)
    entry = _table().get(resolved)
    return TargetFace(entry) if entry else None


def scoring_face(target_id: str) -> TargetFace | None:
    """The face whose rings a target's *scores* are quoted on.

    Usually the same face it draws; see :data:`SCORING_ALIASES`.
    """
    resolved = LEGACY_IDS.get(target_id, target_id)
    return face(SCORING_ALIASES.get(resolved, resolved))


def known_ids() -> list[str]:
    return sorted(_table())
