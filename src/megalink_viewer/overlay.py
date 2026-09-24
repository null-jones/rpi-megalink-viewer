"""A display's name flashed over a browser, when it is asked to identify itself.

In window mode the display draws its own screen, and flashes its name on it. In
browser mode the screen is Megalink's own page, which the display does not draw
and cannot write on. So a small window of its own goes on top of the browser for
as long as it is identifying itself, then closes -- which leaves the page as it
was, rather than reloading it in Chromium, which on a Pi takes a while.

Run as a process of its own for those few seconds, so that browser mode, which
otherwise needs no toolkit at all, only loads Tk while there is something to
show.
"""

from __future__ import annotations

import re
import sys
from typing import Any

#: The same colours as the window mode's own flash, so it looks the same.
ACCENT = "#4aa3df"
DARK = "#0d1b24"

_GEOMETRY = re.compile(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$")

#: How much of its screen the banner covers.
WIDTH_SHARE = 0.7
HEIGHT_SHARE = 0.28


def command(text: str, seconds: float, geometry: str | None = None) -> list[str]:
    """How to start one: ``geometry`` is the screen's, ``WxH+X+Y``, or all of it."""
    args = [
        sys.executable,
        "-m",
        "megalink_viewer",
        "identify-overlay",
        "--text",
        text,
        "--seconds",
        f"{max(1.0, seconds):.1f}",
    ]
    if geometry:
        args += ["--geometry", geometry]
    return args


def banner(screen: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Where the banner goes on a screen of ``(width, height, x, y)``: centred."""
    width, height, x, y = screen
    w, h = int(width * WIDTH_SHARE), int(height * HEIGHT_SHARE)
    return w, h, x + (width - w) // 2, y + (height - h) // 2


def parse_geometry(text: str) -> tuple[int, int, int, int] | None:
    """``WxH+X+Y`` as ``(width, height, x, y)``, or ``None`` if it is not one."""
    match = _GEOMETRY.match(text or "")
    if match is None:
        return None
    width, height, x, y = (int(part) for part in match.groups())
    return width, height, x, y


def show(text: str, seconds: float, geometry: str | None = None) -> None:  # pragma: no cover
    """Flash ``text`` for ``seconds``. Needs an X server; blocks until done."""
    import tkinter as tk
    from tkinter import font as tkfont

    root = tk.Tk()
    # Override-redirect before it is ever shown: nothing manages it, nothing
    # moves it, and it stays above the browser rather than behind it.
    root.overrideredirect(True)
    screen = parse_geometry(geometry or "") or (
        root.winfo_screenwidth(),
        root.winfo_screenheight(),
        0,
        0,
    )
    w, h, x, y = banner(screen)
    root.geometry(f"{w}x{h}+{x}+{y}")
    face = tkfont.Font(root=root, family="DejaVu Sans", size=-int(h * 0.42), weight="bold")
    # Shrunk to fit, for a long name on a small screen.
    while face.measure(text) > w * 0.92 and -face.cget("size") > 12:
        face.configure(size=int(face.cget("size") * 0.9))
    label = tk.Label(root, text=text, font=face, bg=ACCENT, fg=DARK)
    label.pack(fill="both", expand=True)
    state: dict[str, Any] = {"on": True}

    def blink() -> None:
        # Blink, as the window mode does, so it catches the eye down the line.
        state["on"] = not state["on"]
        label.configure(bg=ACCENT if state["on"] else DARK, fg=DARK if state["on"] else ACCENT)
        root.lift()
        root.after(500, blink)

    root.after(500, blink)
    root.after(int(max(1.0, seconds) * 1000), root.destroy)
    root.mainloop()
