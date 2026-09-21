from __future__ import annotations

import json
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"


def load(name: str) -> dict:
    """Load a fixture captured from the live Megalink feed."""
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


# -- protocol v1 (legacy `mllive` tree) -----------------------------------


@pytest.fixture
def v1_dfs() -> dict:
    """Nidaros SKL 15m: integer-scored DFS rifle, five-shot series, real shots."""
    return load("v1_nidaros_skl")


@pytest.fixture
def v1_issf() -> dict:
    """USA Shooting: decimal-scored ISSF rifle with a split 60-shot series."""
    return load("v1_usa_shooting")


@pytest.fixture
def v1_list_shaped() -> dict:
    """A club whose `ranges` node arrived as a JSON array, not an object."""
    return load("v1_list_shaped")


# -- protocol v2 (current `mllivearena` tree) -----------------------------


@pytest.fixture
def v2_pistol() -> dict:
    """Stord PK 1-10: live 25m pistol, named shooters, integer scoring."""
    return load("v2_stord_pk")


@pytest.fixture
def v2_rifle() -> dict:
    """Mulberry: 10m rifle, sighting series in progress, no names published."""
    return load("v2_mulberry")


@pytest.fixture
def v2_split_card() -> dict:
    """US Naval Academy airgun: an ISSF 60-shot card published as *one* series.

    The range states ``maxSeriesSize: 10`` and puts the six group totals in
    ``seriesTotals.splitTotals``; its own display shows six tens. The other two
    v2 fixtures publish their series already at group size, so this is the only
    one that exercises the dividing.
    """
    return load("v2_usna_airgun")


@pytest.fixture
def v2_hosts_active() -> dict:
    """The `hosts_active` node: every club streaming, and its live ranges."""
    return load("hosts_active")


# -- a client with the network replaced ------------------------------------


class FakeClient:
    """Stands in for :class:`~megalink_viewer.client.MegalinkClient`.

    Serves the captured fixtures, so everything above the network -- the
    controller, the web API, the fleet dashboard -- can be tested without one.
    """

    def __init__(self, trees=None, active=None, fail=None):
        from megalink_viewer.client import ARENA_DB, Source

        self._Source = Source
        self._db = ARENA_DB
        #: ``(host, range_key) -> tree``.
        self.trees = trees or {}
        self._active = active or {}
        #: Set to a message to make every resolve fail, as an unreachable
        #: backend would.
        self.fail = fail
        self.resolved = []

    def active(self):
        from megalink_viewer.client import MegalinkError, parse_active_hosts

        if self.fail:
            raise MegalinkError(self.fail)
        return parse_active_hosts(self._active)

    def resolve(self, host, wanted=""):
        from megalink_viewer.client import MegalinkError

        if self.fail:
            raise MegalinkError(self.fail)
        for candidate_host, candidate_range in self.trees:
            if candidate_host != host:
                continue
            if not wanted or wanted == candidate_range:
                self.resolved.append((host, candidate_range))
                return self._Source(
                    2, self._db, f"data/{host}/{candidate_range}", host, candidate_range
                )
        raise MegalinkError(f"{host}: no range matching {wanted!r}")

    def snapshot(self, source):
        return self.trees.get((source.host, source.range_key), {})

    def snapshot_v1(self, host):
        return {}

    def ranges(self, host):
        from megalink_viewer.client import MegalinkError
        from megalink_viewer.models import RangeInfo

        if self.fail:
            raise MegalinkError(self.fail)
        return [
            RangeInfo(key=range_key, name=range_key, host_name=host, protocol=2)
            for (candidate, range_key) in self.trees
            if candidate == host
        ]

    def source_lanes(self, source):
        return source.lanes(self.snapshot(source))

    def stream_source(self, source, stop=None):
        """Replay the snapshot as the single event a real stream opens with."""
        from megalink_viewer.client import Event

        yield Event("put", "/", self.snapshot(source))


@pytest.fixture
def fake_client(v2_pistol, v2_rifle):
    """A client serving two ranges from the captured fixtures."""
    return FakeClient(
        trees={
            ("stord-pk", "1-10"): v2_pistol,
            ("mulberry", "mulberry"): v2_rifle,
        },
        active={
            "stord-pk": {"1-10": {"hostName": "Stord PK", "rangeName": "1-10", "eventName": ""}},
            "mulberry": {"mulberry": {"hostName": "Mulberry", "rangeName": "Mulberry"}},
        },
    )


@pytest.fixture
def config_path(tmp_path):
    return tmp_path / "display.json"


def make_png(width: int = 40, height: int = 20, color: tuple = (200, 30, 30)) -> bytes:
    """A tiny valid PNG, for exercising the logo upload."""
    import struct
    import zlib

    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
