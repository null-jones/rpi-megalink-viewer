"""Megalink's private-use-area shot markers.

Shot values in the live feed are display strings, and Megalink ships a custom
font (``assets/fonts/FreeSansDU-small.ttf`` on live.megalink.no) whose cmap maps
U+E001..U+E015 to named marker glyphs. Those code points appear verbatim in the
feed, so a viewer that does not have the font needs its own translation table.
The names below are the glyph names taken from that font; the ASCII fallbacks
and descriptions are ours.
"""

from __future__ import annotations

from typing import NamedTuple


class Symbol(NamedTuple):
    """A non-numeric shot marker."""

    name: str
    ascii: str
    description: str


#: Glyph name -> marker, keyed by the code point Megalink puts in the feed.
SYMBOLS = {
    "": Symbol("ISSF_TEN", "X", "inner ten (ISSF)"),
    "": Symbol("ISSF_ELEVEN", "XI", "inner eleven (ISSF)"),
    "": Symbol("DFS_TEN", "*", "ten (DFS)"),
    "": Symbol("DFS_CENTER", "**", "centre hit (DFS)"),
    "": Symbol("TURNED", "T", "target turned"),
    "": Symbol("FRAME", "#", "hit in the frame"),
    "": Symbol("DOUBLE", "2", "double hit"),
    "": Symbol("OUT_OF_SEQUENCE", "?", "shot out of sequence"),
    "": Symbol("INVALID", "!", "invalid shot"),
    "": Symbol("VOID", "-", "no shot / void"),
    "": Symbol("SUPER", "S", "super shot"),
    "": Symbol("HIDE", "", "hidden"),
    "": Symbol("ERR_ADVANCE", "E1", "advance error"),
    "": Symbol("ERR_TEMP", "E2", "temperature error"),
    "": Symbol("ERR_NOISE", "E3", "noise error"),
    "": Symbol("GO_BAND", "G", "go band"),
    "": Symbol("MANUAL", "M", "manually entered"),
    "": Symbol("WARNING", "W", "warning"),
    "": Symbol("ERR_ENERGY_1", "P1", "energy error 1"),
    "": Symbol("ERR_ENERGY_2", "P2", "energy error 2"),
    "": Symbol("ERR_ENERGY_3", "P3", "energy error 3"),
}

#: Markers that mean "this shot did not score", as opposed to a scoring
#: annotation such as an inner ten.
NON_SCORING = frozenset({"TURNED", "FRAME", "OUT_OF_SEQUENCE", "INVALID", "VOID", "HIDE"})

#: Markers that indicate an inner/centre hit rather than a fault.
INNER_MARKERS = frozenset({"ISSF_TEN", "ISSF_ELEVEN", "DFS_TEN", "DFS_CENTER"})


def lookup(char: str) -> Symbol | None:
    """Return the marker for a single private-use character, if it is one."""
    return SYMBOLS.get(char)
