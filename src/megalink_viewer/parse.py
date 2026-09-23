"""Decoding of Megalink Live's wire format into plain Python values.

The live feed is a Firebase Realtime Database tree whose leaves are *display
strings* rather than numbers, so almost every interesting value needs decoding:

* Numbers arrive as strings and may carry an inner-ten annotation, either as an
  ``x`` suffix (ISSF: ``"10.6x"``) or a ``*`` prefix (DFS: ``"*10.5"``).
* Any string may carry a trailing ``Style:{...}`` blob that the web client
  strips before rendering (``'0.00Style:{"font":"italic"}'``).
* Totals are formatted per discipline: ``"346 (*4)"`` (DFS sum + inner count),
  ``"552-8x"`` (ISSF sum + x count), or a bare decimal ``"133.1"``.
* Some series report a time rather than a score (``"10.42s"``).
* Non-numeric shots are private-use characters (see :mod:`.symbols`).
* Firebase silently turns integer-keyed maps into JSON arrays, so collections
  are sometimes objects and sometimes lists with holes.
"""

from __future__ import annotations

import re
from typing import Any

from .symbols import INNER_MARKERS, NON_SCORING, Symbol, lookup

_STYLE_MARKER = "Style:{"

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
#: ``"346 (*4)"`` -- DFS style sum with a parenthesised inner-hit count.
_DFS_TOTAL_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*\(\*(\d+)\)\s*$")
#: ``"552-8x"`` -- ISSF style sum with an x count.
_ISSF_TOTAL_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*-\s*(\d+)x\s*$", re.IGNORECASE)
#: ``"10.42s"`` -- an elapsed time rather than a score.
_TIME_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*s\s*$", re.IGNORECASE)

Number = int | float


def strip_style(value: Any) -> str:
    """Drop the ``Style:{...}`` suffix Megalink appends to some display strings."""
    if value is None:
        return ""
    text = str(value)
    index = text.find(_STYLE_MARKER)
    if index != -1:
        text = text[:index]
    return text


def as_number(text: str) -> Number | None:
    """Return the first number in *text*, as an ``int`` when it has no fraction."""
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    raw = match.group(0)
    if "." in raw:
        return float(raw)
    return int(raw)


def entries(collection: Any) -> list[tuple[str, Any]]:
    """Normalise a Firebase collection to ``(key, value)`` pairs.

    Firebase represents a map whose keys are ``0..n`` as a JSON array, and pads
    the gaps with ``null`` -- series are 1-indexed, so index 0 is always empty.
    Both shapes reach us for ``ranges``, ``series`` and ``shots``.
    """
    if isinstance(collection, dict):
        return [(str(k), v) for k, v in collection.items() if v is not None]
    if isinstance(collection, list):
        return [(str(i), v) for i, v in enumerate(collection) if v is not None]
    return []


class ShotValue:
    """A decoded shot value.

    Megalink always measures to a tenth of a ring, but only a *decimal* series
    scores that way: on an integer-scored series (DFS, most Norwegian
    disciplines) the feed still sends ``"10.8"`` and the score is the ring it
    fell in, ``10``. :meth:`ring` applies that rule; ``score`` is the raw
    measurement and ``text`` is the number as Megalink spelled it, so a display
    can show the precision the shooter expects.

    ``score`` is ``None`` for a non-scoring marker (a turned target, a void
    shot); ``symbol`` is set only when the feed sent a marker glyph.
    """

    __slots__ = ("inner", "raw", "score", "symbol", "text")

    def __init__(
        self,
        raw: str,
        text: str,
        score: Number | None,
        inner: bool,
        symbol: Symbol | None,
    ) -> None:
        self.raw = raw
        self.text = text
        self.score = score
        self.inner = inner
        self.symbol = symbol

    @property
    def scored(self) -> bool:
        return self.score is not None

    def ring(self, decimal: bool) -> Number | None:
        """The value this shot contributes to a sum.

        A decimal series counts the measurement as-is; an integer series counts
        the ring, which means truncating rather than rounding -- ``10.8`` scores
        ``10``, not ``11``.
        """
        if self.score is None:
            return None
        if decimal:
            return self.score
        return int(self.score)

    def display(self) -> str:
        """A short, font-independent rendering suitable for a text display."""
        if self.symbol is not None and self.score is None:
            return self.symbol.ascii or "-"
        if self.score is None:
            return "-"
        return f"{self.text}x" if self.inner else self.text

    def __repr__(self) -> str:
        return f"ShotValue({self.display()!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ShotValue):
            return NotImplemented
        return (
            self.raw == other.raw
            and self.score == other.score
            and self.inner == other.inner
            and self.symbol == other.symbol
        )


def parse_shot_value(value: Any) -> ShotValue:
    """Decode a single ``shots[].value`` string.

    Handles the ISSF ``x`` suffix, the DFS ``*`` prefix, the private-use marker
    glyphs, and the padding spaces Megalink uses to keep values aligned.
    """
    raw = strip_style(value)
    text = raw.strip()

    symbol = None
    # A marker glyph may stand alone or sit alongside a number.
    for char in text:
        found = lookup(char)
        if found is not None:
            symbol = found
            text = text.replace(char, "").strip()
            break

    inner = False
    if text.endswith(("x", "X")):
        inner = True
        text = text[:-1].strip()
    if text.startswith("*"):
        inner = True
        text = text.lstrip("*").strip()

    if symbol is not None:
        if symbol.name in INNER_MARKERS:
            inner = True
        elif symbol.name in NON_SCORING:
            return ShotValue(raw, text, None, inner, symbol)

    score = as_number(text) if text else None
    return ShotValue(raw, text, score, inner, symbol)


class Total:
    """A decoded sum, either of a series or of a whole card."""

    __slots__ = ("inner", "raw", "seconds", "value")

    def __init__(
        self,
        raw: str,
        value: Number | None,
        inner: int | None = None,
        seconds: float | None = None,
    ) -> None:
        self.raw = raw
        self.value = value
        self.inner = inner
        self.seconds = seconds

    def display(self, decimals: int | None = None) -> str:
        if self.seconds is not None:
            return f"{self.seconds:.2f}s"
        if self.value is None:
            return "-"
        text = f"{self.value:.{decimals}f}" if decimals else f"{self.value:g}"
        if self.inner:
            text += f" ({self.inner}x)"
        return text

    def __repr__(self) -> str:
        return f"Total({self.display()!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Total):
            return NotImplemented
        return (
            self.value == other.value
            and self.inner == other.inner
            and self.seconds == other.seconds
        )


def parse_total(value: Any) -> Total:
    """Decode a ``totalSum`` or ``series[].sum`` string."""
    raw = strip_style(value)
    text = raw.strip()
    if not text:
        return Total(raw, None)

    match = _DFS_TOTAL_RE.match(text)
    if match:
        return Total(raw, as_number(match.group(1)), int(match.group(2)))

    match = _ISSF_TOTAL_RE.match(text)
    if match:
        return Total(raw, as_number(match.group(1)), int(match.group(2)))

    match = _TIME_RE.match(text)
    if match:
        return Total(raw, None, seconds=float(match.group(1)))

    return Total(raw, as_number(text))


def slug(name: str) -> str:
    """Slugify a range name the way Megalink keys its database.

    Mirrors the web client: lower-case, then runs of whitespace and underscores
    become a single dash (``"50 M"`` -> ``"50-m"``).
    """
    return re.sub(r"[_\s]+", "-", name.strip().lower())


def loose_key(name: str) -> str:
    """A comparison key that ignores punctuation.

    The URL fragment on live.megalink.no is spelled differently from the
    database key for the same range (``.../50m`` addresses ``50-m``), so range
    lookups compare on this instead.
    """
    return re.sub(r"[^a-z0-9]+", "", name.strip().lower())
