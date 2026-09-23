"""QR codes for the screen, the web page and the console.

A display with nothing configured has to tell whoever is standing in front of
it how to reach its settings, and the easiest thing to ask of someone with no
experience of any of this is to point a phone at the screen. The encoding is
done by the vendored qrcodegen; this is only the part that turns its modules
into something each kind of display can draw.
"""

from __future__ import annotations

from ._vendor.qrcodegen import QrCode

#: The blank margin a scanner needs round the code, in modules. Four is what the
#: standard asks for; less and some phones will not find the code at all.
QUIET_ZONE = 4


def modules(text: str, border: int = QUIET_ZONE) -> list[list[bool]]:
    """The code as rows of modules, ``True`` for dark, quiet zone included.

    Medium error correction: a code on a screen is not going to be torn or
    smudged, but it may be looked at from an angle through glare, and a little
    redundancy costs only a few modules.
    """
    code = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    size = code.get_size()
    span = range(-border, size + border)
    # get_module answers False outside the code, which is the quiet zone.
    return [[code.get_module(x, y) for x in span] for y in span]


def wifi(ssid: str, password: str = "") -> str:
    """The text a phone's camera reads as "join this Wi-Fi network".

    Not a standard so much as a convention every phone follows. The special
    characters have to be escaped, or a network whose name contains one of them
    comes up as the wrong network or none at all.
    """

    def escape(value: str) -> str:
        for char in '\\;,:"':
            value = value.replace(char, "\\" + char)
        return value

    if password:
        return f"WIFI:T:WPA;S:{escape(ssid)};P:{escape(password)};;"
    return f"WIFI:T:nopass;S:{escape(ssid)};;"


def svg(text: str) -> str:
    """An SVG of the code, for a web page. Scales to whatever box it is put in.

    One path rather than a rectangle per module, which keeps a code of a few
    hundred modules to a few kilobytes, and crisp edges so a browser does not
    blur the module boundaries a scanner is looking for.
    """
    grid = modules(text)
    size = len(grid)
    runs = []
    for y, row in enumerate(grid):
        x = 0
        while x < size:
            if row[x]:
                start = x
                while x < size and row[x]:
                    x += 1
                runs.append(f"M{start} {y}h{x - start}v1h{start - x}z")
            else:
                x += 1
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'shape-rendering="crispEdges" role="img" aria-label="QR code">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path d="{"".join(runs)}" fill="#000"/></svg>'
    )


def terminal(text: str) -> list[str]:
    """The code as lines for a text console, two columns to a module.

    Drawn with background colours rather than block characters. The Linux
    console's font may have no block elements at all, but every terminal there
    has ever been can set a background colour, and two columns to one row keeps
    the modules roughly square in a console's tall character cells.
    """
    dark, light, reset = "\x1b[40m  ", "\x1b[47m  ", "\x1b[0m"
    return ["".join(dark if cell else light for cell in row) + reset for row in modules(text)]
