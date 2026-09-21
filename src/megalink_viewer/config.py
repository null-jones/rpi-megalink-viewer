"""Configuration for an installed display.

A display on a firing point has to be reconfigurable without a keyboard: which
club, which range, which firing point. That lives in one small JSON file which
the display watches, the on-device web page edits, and a fleet dashboard can
push to over the network.

JSON rather than TOML or YAML on purpose. The file is written by software, not
just read, and JSON is the only human-legible format the standard library can
both read and write on every Python this package supports -- ``tomllib`` only
arrived in 3.11 and only reads.

Resolution order for the file's location:

1. ``$MEGALINK_CONFIG``, if set;
2. ``/etc/megalink/display.json``, if it exists -- the system install;
3. ``$XDG_CONFIG_HOME/megalink/display.json`` (or ``~/.config/...``).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import tempfile
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

CONFIG_ENV = "MEGALINK_CONFIG"
SYSTEM_PATH = Path("/etc/megalink/display.json")
USER_RELATIVE = Path("megalink/display.json")

#: How the scores are shown. ``gui`` needs an X server; ``terminal`` runs on the
#: bare console, which is all a Pi OS Lite install has.
#: How a display draws the scores. "gui" draws them itself, "terminal" writes
#: them to the console, "browser" puts Megalink's own page on the screen.
MODES = ("gui", "terminal", "browser")

DEFAULT_WEB_PORT = 8080
DEFAULT_BEACON_PORT = 45455


class ConfigError(ValueError):
    """Raised when a configuration value cannot be used."""


@dataclass
class DisplaySettings:
    """How this screen behaves."""

    mode: str = "gui"
    fullscreen: bool = True
    #: Seconds between redraws. Half a second keeps the clock honest without
    #: making a Pi Zero work for it.
    interval: float = 0.5
    #: Override the detected screen size *in pixels*, for a window on a display
    #: that reports its size wrongly. Not used by the console, which measures
    #: itself in characters and asks the terminal.
    width: int | None = None
    height: int | None = None
    #: What to show when nobody is on this firing point.
    idle_text: str = "POSITION NOT IN USE"
    #: What to point a browser-mode display at. Empty builds a live.megalink.no
    #: address from the club, range and firing point; set it to show something
    #: else -- a local MLLiveArena server, or a page of several firing points.
    url: str = ""
    #: A picture to show instead of that text -- a club badge, usually. Uploaded
    #: through the configuration page, which writes it next to the config file.
    logo: str = ""

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ConfigError(f"display.mode must be one of {', '.join(MODES)}")
        if self.url and not self.url.startswith(("http://", "https://")):
            raise ConfigError("display.url must start with http:// or https://")
        if len(self.idle_text) > 120:
            raise ConfigError("display.idle_text is too long for a screen")
        if not 0.05 <= float(self.interval) <= 60:
            raise ConfigError("display.interval must be between 0.05 and 60 seconds")
        for name in ("width", "height"):
            value = getattr(self, name)
            if value is not None and not 1 <= int(value) <= 10_000:
                raise ConfigError(f"display.{name} is out of range")


@dataclass
class WebSettings:
    """The configuration page served by the display itself."""

    enabled: bool = True
    #: 0 means "ask the operating system for a free port", which is mostly
    #: useful for tests and for running two displays on one machine.
    port: int = DEFAULT_WEB_PORT
    #: Listening address. The point of the page is to be reachable from a phone
    #: on the same network, so this defaults to every interface.
    bind: str = "0.0.0.0"
    #: Optional shared secret. When set, changing anything requires it in an
    #: ``X-Megalink-Token`` header. Empty means an open network is assumed.
    token: str = ""

    def validate(self) -> None:
        if not 0 <= int(self.port) <= 65535:
            raise ConfigError("web.port must be a valid port number")


@dataclass
class BeaconSettings:
    """The heartbeat that lets a fleet dashboard find this display."""

    enabled: bool = True
    port: int = DEFAULT_BEACON_PORT
    #: Seconds between announcements.
    interval: float = 10.0
    #: What to call this display; defaults to the machine's hostname.
    name: str = ""
    #: Where to broadcast. Empty works it out; set it when the network needs a
    #: particular broadcast address, or to unicast straight at a dashboard.
    address: str = ""

    def validate(self) -> None:
        if not 1 <= int(self.port) <= 65535:
            raise ConfigError("beacon.port must be a valid port number")
        if not 1 <= float(self.interval) <= 3600:
            raise ConfigError("beacon.interval must be between 1 and 3600 seconds")


@dataclass
class Config:
    """Everything a display needs to know."""

    host: str = ""
    #: Range key, display name, or the slug from a Live URL -- all are accepted.
    range: str = ""
    lane: str = ""
    display: DisplaySettings = field(default_factory=DisplaySettings)
    web: WebSettings = field(default_factory=WebSettings)
    beacon: BeaconSettings = field(default_factory=BeaconSettings)

    # -- naming ------------------------------------------------------------

    @property
    def name(self) -> str:
        """A human label for this display."""
        return self.beacon.name or socket.gethostname()

    @property
    def configured(self) -> bool:
        """Whether there is enough here to show something."""
        return bool(self.host and self.lane)

    def describe(self) -> str:
        if not self.configured:
            return "not configured"
        parts = [self.host]
        if self.range:
            parts.append(self.range)
        parts.append(f"lane {self.lane}")
        return " · ".join(parts)

    # -- validation --------------------------------------------------------

    def validate(self) -> None:
        for name in ("host", "range", "lane"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ConfigError(f"{name} must be text")
            if len(value) > 200:
                raise ConfigError(f"{name} is implausibly long")
        self.display.validate()
        self.web.validate()
        self.beacon.validate()

    # -- serialising -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """As :meth:`to_dict`, without the shared secret.

        What the web API hands out. A token that travels back to every browser
        that asks is not a token.
        """
        data = self.to_dict()
        data["web"]["token"] = bool(data["web"].get("token"))
        return data

    @classmethod
    def from_dict(cls, data: Any) -> Config:
        """Build a config from parsed JSON, ignoring keys it does not know.

        Tolerant on purpose: an older display reading a newer file should keep
        working rather than refuse to start.
        """
        if not isinstance(data, dict):
            raise ConfigError("configuration must be a JSON object")
        sections = {
            "display": DisplaySettings,
            "web": WebSettings,
            "beacon": BeaconSettings,
        }
        kwargs: dict[str, Any] = {}
        for spec in fields(cls):
            if spec.name in sections:
                continue
            if spec.name in data:
                kwargs[spec.name] = data[spec.name]
        for key, section in sections.items():
            raw = data.get(key)
            if isinstance(raw, dict):
                known = {f.name for f in fields(section)}
                kwargs[key] = section(**{k: v for k, v in raw.items() if k in known})
        config = cls(**kwargs)
        config.coerce()
        return config

    def coerce(self) -> None:
        """Nudge values into the right types.

        A hand-edited file, or a form post, may put a number where a string
        belongs. Lanes especially: they are text here because some ranges label
        firing points with letters.
        """
        self.host = str(self.host or "").strip()
        self.range = str(self.range or "").strip()
        self.lane = str(self.lane or "").strip()
        self.display.mode = str(self.display.mode or "gui").strip().lower()
        self.display.interval = float(self.display.interval)
        self.display.fullscreen = bool(self.display.fullscreen)
        self.display.idle_text = str(self.display.idle_text or "").strip()
        self.display.logo = str(self.display.logo or "").strip()
        self.display.url = str(self.display.url or "").strip()
        for name in ("width", "height"):
            value = getattr(self.display, name)
            setattr(self.display, name, None if value in (None, "", 0) else int(value))
        self.web.enabled = bool(self.web.enabled)
        self.web.port = int(self.web.port)
        self.web.bind = str(self.web.bind or "0.0.0.0").strip()
        self.web.token = str(self.web.token or "")
        self.beacon.enabled = bool(self.beacon.enabled)
        self.beacon.port = int(self.beacon.port)
        self.beacon.interval = float(self.beacon.interval)
        self.beacon.name = str(self.beacon.name or "").strip()
        self.beacon.address = str(self.beacon.address or "").strip()

    def merged(self, patch: Any) -> Config:
        """A copy with *patch* applied over it, section by section.

        The web API sends whatever the operator changed rather than the whole
        document, so a partial update must not blank out everything else.
        """
        if not isinstance(patch, dict):
            raise ConfigError("update must be a JSON object")
        data = self.to_dict()
        for key, value in patch.items():
            if key in ("display", "web", "beacon") and isinstance(value, dict):
                data[key] = {**data.get(key, {}), **value}
            elif key in {f.name for f in fields(Config)}:
                data[key] = value
        updated = Config.from_dict(data)
        updated.validate()
        return updated

    def replace(self, **changes: Any) -> Config:
        return replace(self, **changes)


#: Image formats Tk can display without a third-party library.
LOGO_SUFFIXES = {".png": "image/png", ".gif": "image/gif"}


def logo_path(config_path: Path | None = None, suffix: str = ".png") -> Path:
    """Where an uploaded logo is kept: beside the configuration."""
    base = Path(config_path) if config_path is not None else default_path()
    return base.parent / f"logo{suffix}"


def find_logo(config: Config, config_path: Path | None = None) -> Path | None:
    """The logo to show, whether it was configured explicitly or just uploaded."""
    if config.display.logo:
        candidate = Path(config.display.logo).expanduser()
        return candidate if candidate.is_file() else None
    for suffix in LOGO_SUFFIXES:
        candidate = logo_path(config_path, suffix)
        if candidate.is_file():
            return candidate
    return None


def default_path() -> Path:
    """Where the configuration lives on this machine."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    if SYSTEM_PATH.exists():
        return SYSTEM_PATH
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / USER_RELATIVE


def load(path: Path | None = None) -> Config:
    """Read the configuration, falling back to defaults when there is none."""
    target = Path(path) if path is not None else default_path()
    if not target.exists():
        return Config()
    body = target.read_text(encoding="utf-8")
    if not body.strip():
        # An empty file is not a configuration with a mistake in it; it is no
        # configuration at all, and there is nothing in it to lose by starting
        # from defaults. Refusing to start over an empty file leaves a display
        # dead for a reason nobody can act on -- and empty files do happen, from
        # an interrupted write or a tired SD card.
        return Config()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{target} is not valid JSON: {exc}") from exc
    config = Config.from_dict(data)
    config.validate()
    return config


def save(config: Config, path: Path | None = None) -> Path:
    """Write the configuration, atomically.

    A display reads this file while a web request writes it, and a half-written
    file read at the wrong moment would take the screen down. Writing to a
    neighbouring temporary file and renaming means a reader sees either the old
    document or the new one.
    """
    config.validate()
    target = Path(path) if path is not None else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n"
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".display-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return target
