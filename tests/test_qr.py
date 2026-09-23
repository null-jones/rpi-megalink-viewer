"""QR codes for a display to show how it can be reached.

That the codes *scan* was checked with an independent reader (zxing-cpp) when
this was written; these pin down the shape of what is drawn, which is the part
this package does rather than the vendored encoder.
"""

from __future__ import annotations

import re

from megalink_viewer import qr

URL = "http://192.168.1.23:8080/"


def finder_at(grid, x, y):
    """A 7x7 finder pattern -- the three squares a scanner locks on to."""
    for dy in range(7):
        for dx in range(7):
            ring = max(abs(dx - 3), abs(dy - 3))
            if grid[y + dy][x + dx] != (ring != 2):
                return False
    return True


class TestModules:
    def test_the_code_is_square(self):
        grid = qr.modules(URL)
        assert len({len(row) for row in grid}) == 1
        assert len(grid) == len(grid[0])

    def test_the_quiet_zone_is_left_blank(self):
        grid = qr.modules(URL)
        edge = qr.QUIET_ZONE
        for row in grid[:edge] + grid[-edge:]:
            assert not any(row)
        for row in grid:
            assert not any(row[:edge]) and not any(row[-edge:])

    def test_the_three_finder_patterns_are_in_the_corners(self):
        grid = qr.modules(URL)
        edge, size = qr.QUIET_ZONE, len(grid)
        assert finder_at(grid, edge, edge)
        assert finder_at(grid, size - edge - 7, edge)
        assert finder_at(grid, edge, size - edge - 7)
        # ...and not the fourth, which is how a scanner knows which way is up.
        assert not finder_at(grid, size - edge - 7, size - edge - 7)

    def test_a_longer_address_needs_a_bigger_code(self):
        assert len(qr.modules(URL + "x" * 60)) > len(qr.modules(URL))

    def test_no_border_can_be_asked_for(self):
        assert len(qr.modules(URL, border=0)) == len(qr.modules(URL)) - 2 * qr.QUIET_ZONE


class TestWifi:
    def test_a_protected_network(self):
        assert qr.wifi("Range", "secret") == "WIFI:T:WPA;S:Range;P:secret;;"

    def test_an_open_network(self):
        assert qr.wifi("CRPC Guest") == "WIFI:T:nopass;S:CRPC Guest;;"

    def test_special_characters_are_escaped(self):
        # Unescaped, a name with a semicolon ends early and the phone offers to
        # join a network that does not exist.
        assert qr.wifi('a;b,c:d"e\\f', "") == r"WIFI:T:nopass;S:a\;b\,c\:d\"e\\f;;"


class TestSvg:
    def test_it_is_an_svg_of_the_right_size(self):
        size = len(qr.modules(URL))
        out = qr.svg(URL)
        assert out.startswith("<svg") and out.endswith("</svg>")
        assert f'viewBox="0 0 {size} {size}"' in out

    def test_every_dark_module_is_drawn_once(self):
        # The path is one run per stretch of dark modules in a row; adding the
        # runs back up must give exactly the dark modules there are.
        grid = qr.modules(URL)
        path = re.search(r'<path d="([^"]+)"', qr.svg(URL)).group(1)
        drawn = sum(int(width) for width in re.findall(r"h(\d+)v1", path))
        assert drawn == sum(sum(row) for row in grid)

    def test_edges_stay_crisp(self):
        assert 'shape-rendering="crispEdges"' in qr.svg(URL)


class TestTerminal:
    def test_one_line_per_row_of_modules(self):
        assert len(qr.terminal(URL)) == len(qr.modules(URL))

    def test_each_module_is_two_columns_wide(self):
        # Console cells are about twice as tall as they are wide.
        line = qr.terminal(URL)[0]
        visible = re.sub(r"\x1b\[[0-9;]*m", "", line)
        assert len(visible) == 2 * len(qr.modules(URL))

    def test_every_line_resets_its_colour(self):
        assert all(line.endswith("\x1b[0m") for line in qr.terminal(URL))

    def test_it_uses_no_block_characters(self):
        # The Linux console font may not have any.
        assert not any(ch in "".join(qr.terminal(URL)) for ch in "█▀▄")
