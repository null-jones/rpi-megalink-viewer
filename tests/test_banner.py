"""What is running, for a text console: the logo, the name, and the project."""

from __future__ import annotations

import re

from megalink_viewer import banner
from megalink_viewer.render import branded


def plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TestTheLogo:
    """The icon, worked out from its shapes, in a console's eight colours."""

    def test_it_has_every_part_of_the_icon(self):
        cells = {cell for row in banner.logo() for cell in row}
        assert {"arc", "face", "ring", "shot"} <= cells

    def test_the_shot_is_up_and_to_the_right_as_on_the_icon(self):
        grid = banner.logo()
        shots = [
            (r, c) for r, row in enumerate(grid) for c, cell in enumerate(row) if cell == "shot"
        ]
        rows, columns = len(grid), len(grid[0])
        # Where it is centred; its edge reaches the middle, as on the icon.
        assert sum(r for r, _c in shots) / len(shots) < rows / 2
        assert sum(c for _r, c in shots) / len(shots) > columns / 2

    def test_the_arcs_are_either_side_and_not_above_or_below(self):
        grid = banner.logo()
        middle = grid[len(grid) // 2]
        assert middle[0] == "arc" and middle[-1] == "arc"
        assert "arc" not in grid[0][len(grid[0]) // 3 : 2 * len(grid[0]) // 3]

    def test_it_is_drawn_in_background_colours_two_columns_a_cell(self):
        lines = banner.logo_lines()
        assert all(line.endswith(banner.RESET) for line in lines)
        assert all(len(plain(line)) == 2 * banner.LOGO_CELLS for line in lines)
        assert "\x1b[41m" in "".join(lines)  # the red shot


class TestTheBanner:
    def test_it_says_what_this_is_and_where_it_is_from(self):
        text = plain("\n".join(banner.banner(140, address="http://10.0.0.9:8080/", name="fp-09")))
        assert "Megalink viewer" in text
        assert "http://10.0.0.9:8080/" in text and "fp-09" in text
        assert "https://github.com/null-jones/rpi-megalink-viewer" in text

    def test_with_room_the_logo_is_beside_the_words(self):
        lines = banner.banner(140)
        assert len(lines) == len(banner.logo())
        assert "\x1b[46m" in lines[len(lines) // 2]

    def test_on_a_narrow_console_only_the_words(self):
        lines = banner.banner(60)
        assert not any("\x1b[46m" in line for line in lines)
        assert "Megalink viewer" in plain(lines[0])

    def test_without_colour_no_codes_at_all(self):
        assert "\x1b" not in "".join(banner.banner(140, color=False))

    def test_the_footer_fits(self):
        assert "github.com/null-jones/rpi-megalink-viewer" in banner.footer(80)
        assert banner.footer(20) == "Megalink viewer"


class TestTheLoginScreen:
    """/etc/issue.d: what is on the screen while a display boots."""

    def test_the_address_and_name_are_filled_in_by_the_login_prompt(self):
        # agetty's own escapes, so they stay right after the file is written.
        text = banner.issue(8080)
        assert "http://\\4:8080/" in text
        assert "This display: \\n" in text

    def test_port_80_has_no_port(self):
        assert "http://\\4/" in banner.issue(80)

    def test_the_screen_is_cleared_of_boot_messages_first(self):
        assert banner.issue().startswith("\x1b[H\x1b[2J")

    def test_the_logo_is_on_it(self):
        assert "\x1b[46m" in banner.issue()


class TestTheConsoleScreens:
    def test_with_room_a_screen_gets_the_banner_above_it(self):
        lines = branded(["  SET UP THIS DISPLAY"], 140, 60, interactive=True)
        assert "Megalink viewer" in plain("\n".join(lines[: len(banner.logo())]))
        assert lines[-1] == "  SET UP THIS DISPLAY"

    def test_without_room_a_line_along_the_bottom(self):
        body = ["x"] * 50
        lines = branded(body, 140, 55, interactive=True)
        assert len(lines) == 55
        assert "github.com" in lines[-1] and lines[:50] == body

    def test_in_a_log_nothing_is_added(self):
        assert branded(["a", "b"], 140, 60, interactive=False) == ["a", "b"]


class TestTheCommand:
    def test_it_writes_the_login_screen(self, tmp_path, capsys):
        from megalink_viewer.cli import main

        assert main(["banner", "--issue", "--config", str(tmp_path / "display.json")]) == 0
        assert "\\4" in capsys.readouterr().out

    def test_plain_is_plain(self, tmp_path, capsys, monkeypatch):
        from megalink_viewer import address
        from megalink_viewer.cli import main

        monkeypatch.setattr(address, "find", lambda port: address.Reach("fp-09", [], port))
        assert main(["banner", "--plain", "--config", str(tmp_path / "display.json")]) == 0
        out = capsys.readouterr().out
        assert "\x1b" not in out and "Megalink viewer" in out
