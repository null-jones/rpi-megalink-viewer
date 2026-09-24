"""Build the printable setup guide: docs/guide/guide.md to a US Letter PDF.

    python3 docs/guide/build.py [OUT.pdf] [--html OUT.html]

Needs Python-Markdown and WeasyPrint, which the display itself does not, so it
is run in a Debian container (docs/guide/tools/pdf.sh) or by CI rather than
being a dependency of the package. The Markdown stays readable on GitHub: the
cover, the contents and the page numbers are added here, a screenshot's alt
text becomes its caption, and GitHub's "> [!TIP]" notes become boxes.
"""

from __future__ import annotations

import datetime
import html
import os
import re
import struct
import sys
import tomllib
from pathlib import Path

import markdown
from weasyprint import HTML

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROJECT_URL = "https://github.com/null-jones/rpi-megalink-viewer"
#: Screenshots narrower than this, in pixels, are of a phone: the settings
#: pages are taken 430 wide at twice the scale, the screens 1280 and up.
PHONE_WIDTH = 1000

#: GitHub's alert blockquotes, and what each is called in print.
ALERTS = {"NOTE": "Note", "TIP": "Tip", "IMPORTANT": "Important", "WARNING": "Warning",
          "CAUTION": "Caution"}


def version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def built_on() -> str:
    """Today, or the date reproducible builds ask for."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = (
        datetime.datetime.fromtimestamp(int(epoch), datetime.UTC)
        if epoch
        else datetime.datetime.now(datetime.UTC)
    )
    return when.strftime("%B %Y").replace(" 0", " ")


def alerts(text: str) -> str:
    """``> [!TIP]`` blockquotes to boxes, with the Markdown inside kept."""
    out, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        match = re.match(r"^> \[!(\w+)\]\s*$", lines[i])
        if match and match.group(1).upper() in ALERTS:
            kind = match.group(1).upper()
            body = []
            i += 1
            while i < len(lines) and lines[i].startswith(">"):
                body.append(lines[i][1:].removeprefix(" "))
                i += 1
            out += [
                f'<div class="callout {kind.lower()}" markdown="1">',
                f'<p class="callout-title">{ALERTS[kind]}</p>',
                "",
                *body,
                "",
                "</div>",
            ]
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", head[16:24])


def figures(body: str) -> str:
    """An image on its own becomes a figure, its alt text the caption.

    A phone's screenshot -- narrower than any screen's -- is printed about the
    size of a phone rather than across the page.
    """

    def figure(match: re.Match[str]) -> str:
        tag = match.group(1)
        src = re.search(r'src="([^"]+)"', tag).group(1)
        alt = html.unescape(re.search(r'alt="([^"]*)"', tag).group(1))
        size = png_size(HERE / src)
        kind = "phone" if size and size[0] < PHONE_WIDTH else "wide"
        caption = f"<figcaption>{html.escape(alt)}</figcaption>" if alt else ""
        return f'<figure class="{kind}">{tag}{caption}</figure>'

    return re.sub(r"<p>(<img [^>]+>)</p>", figure, body)


def contents(body: str, intro: str) -> str:
    """The chapters, each with the page it starts on, and what the guide is.

    The introduction goes here, under the contents, rather than on a page of
    its own before the first chapter, which starts a page.
    """
    items = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', body)
    rows = "".join(
        f'<li><a href="#{anchor}">{title}</a></li>' for anchor, title in items
    )
    return (
        f'<nav class="contents"><h2 class="toc-title">Contents</h2><ol>{rows}</ol>'
        f'<div class="intro">{intro}</div></nav>'
    )


def build(out: Path, html_out: Path | None = None) -> None:
    source = (HERE / "guide.md").read_text("utf-8")
    title_match = re.match(r"^# (.+)\n", source)
    title = title_match.group(1) if title_match else "Setup guide"
    source = source[title_match.end():] if title_match else source
    body = markdown.markdown(
        alerts(source),
        extensions=["tables", "attr_list", "md_in_html", "toc", "sane_lists"],
        extension_configs={"toc": {"permalink": False}},
    )
    body = figures(body)
    # What comes before the first chapter is the introduction.
    first = body.find("<h2")
    intro, body = (body[:first], body[first:]) if first > 0 else ("", body)
    icon = (ROOT / "assets" / "icon.svg").read_text("utf-8")
    cover = f"""
<section class="cover">
  <div class="logo">{icon}</div>
  <h1>{html.escape(title)}</h1>
  <p class="subtitle">A score display for Megalink Live, on a Raspberry Pi,
  from an empty SD card to a screen beside every firing point.</p>
  <p class="meta">Version {version()} &middot; {built_on()}</p>
  <p class="meta">{PROJECT_URL}</p>
</section>"""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<link rel="stylesheet" href="style.css"></head>
<body>{cover}{contents(body, intro)}<main>{body}</main></body></html>"""
    if html_out is not None:
        html_out.write_text(page, "utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=page, base_url=str(HERE)).write_pdf(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    args = sys.argv[1:]
    html_out = None
    if "--html" in args:
        at = args.index("--html")
        html_out = Path(args[at + 1])
        del args[at : at + 2]
    target = Path(args[0]) if args else ROOT / "dist" / f"megalink-viewer-guide-{version()}.pdf"
    build(target, html_out)
