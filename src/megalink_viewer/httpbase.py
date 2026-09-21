"""Shared pieces for the two little web servers in this package.

Both the per-display configuration page and the fleet dashboard are
``http.server`` handlers -- no framework, because a Pi Zero does not need one
and a dependency-free install is the whole point.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

#: Refuse bodies larger than this. Nothing legitimate here is big, and an
#: unbounded read is an easy way to exhaust a 512MB machine.
MAX_BODY_BYTES = 64 * 1024

#: How much of an oversized body to read and throw away before answering.
#: Replying without reading leaves the client writing into a closed socket, and
#: it then sees a broken pipe instead of the 413 explaining what went wrong.
#: Draining a bounded amount first means the usual mistake gets a real answer.
DRAIN_LIMIT = 4 * 1024 * 1024
DRAIN_CHUNK = 64 * 1024


class BodyTooLarge(ValueError):
    """Raised when a request body is over :data:`MAX_BODY_BYTES`."""


class Server(ThreadingHTTPServer):
    """A threading HTTP server that does not hold the process open."""

    daemon_threads = True
    allow_reuse_address = True


class JSONHandler(BaseHTTPRequestHandler):
    """Request handler with JSON helpers and quiet logging.

    Subclasses implement :meth:`route`, which returns ``(status, payload)`` for
    an API path, or ``None`` to fall through to the static page.
    """

    server_version = "megalink"
    #: Set by subclasses; the HTML served at ``/``.
    page = "<!doctype html><title>megalink</title>"
    #: Whether a response has already gone out. A route that writes its own
    #: body -- a logo, the fleet page -- returns ``None`` like any route that
    #: found nothing, and without this the dispatcher answers a second time.
    #: Harmless while these servers speak HTTP/1.0 and close after every reply,
    #: because the extra response dies with the socket. It would stop being
    #: harmless the moment anything set ``protocol_version`` to HTTP/1.1: the
    #: client reads the first response by its Content-Length and picks the
    #: second one up as the answer to its *next* request.
    _answered = False

    def log_message(self, fmt: str, *args: Any) -> None:
        # A display's journal should carry what the display is doing, not a line
        # for every poll of the status endpoint.
        return

    # -- helpers -----------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self._answered = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def send_html(self, status: int, html: str) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def read_json(self) -> Any:
        """Parse the request body, or raise ``ValueError`` with a usable message."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("bad Content-Length") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            self._drain(min(length, DRAIN_LIMIT))
            raise BodyTooLarge(
                f"request body too large ({length} bytes; the limit is {MAX_BODY_BYTES})"
            )
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"body is not valid JSON: {exc}") from exc

    def _drain(self, remaining: int) -> None:
        """Read and discard a body we are about to refuse."""
        while remaining > 0:
            chunk = self.rfile.read(min(DRAIN_CHUNK, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

    @property
    def route_path(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    @property
    def query(self) -> dict[str, str]:
        raw = parse_qs(urlparse(self.path).query)
        return {key: values[0] for key, values in raw.items() if values}

    # -- dispatch ----------------------------------------------------------

    def route(self, method: str) -> tuple[int, Any] | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _handle(self, method: str) -> None:
        try:
            result = self.route(method)
        except BodyTooLarge as exc:
            # Nothing more will be read from this connection, so close it.
            self.close_connection = True
            self.send_error_json(413, str(exc))
            return
        except ValueError as exc:
            self.send_error_json(400, str(exc))
            return
        except PermissionError as exc:
            self.send_error_json(403, str(exc) or "forbidden")
            return
        except Exception as exc:  # pragma: no cover - defensive
            self.send_error_json(500, f"{type(exc).__name__}: {exc}")
            return

        if result is not None:
            status, payload = result
            self.send_json(status, payload)
            return
        if self._answered:
            # The route sent its own body.
            return
        if method in ("GET", "HEAD") and self.route_path == "/":
            self.send_html(200, self.page)
            return
        self.send_error_json(404, "not found")

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")
