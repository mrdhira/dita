"""Dev-only HTTP mirror of the socket protocol. Off unless --http is passed.

This exists so a human can poke the worker with curl. It is not the production path:
the orchestrator always speaks the unix socket.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Tuple
from urllib.parse import urlparse

from .manager import ModelManager
from .server import dispatch

LOG = logging.getLogger(__name__)

MAX_BODY = 64 * 1024 * 1024


def make_server(manager: ModelManager, address: Tuple[str, int]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "inferences-ocr-dev"

        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            op = urlparse(self.path).path.strip("/") or "handshake"
            self._respond(dispatch(manager, {"op": op}, b""))

        def do_POST(self) -> None:  # noqa: N802
            op = urlparse(self.path).path.strip("/")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._respond({"ok": False, "error": {"code": "bad_request", "message": "body too large"}})
                return
            body = self.rfile.read(length)

            # /infer takes raw image bytes; every other op takes its JSON control block.
            if op == "infer":
                control, payload = {"op": "infer"}, body
            else:
                payload = b""
                try:
                    control = json.loads(body) if body else {}
                except json.JSONDecodeError as exc:
                    self._respond({"ok": False, "error": {"code": "bad_request", "message": str(exc)}})
                    return
                control["op"] = op
            self._respond(dispatch(manager, control, payload))

        def log_message(self, fmt: str, *args: object) -> None:
            LOG.debug("http %s - %s", self.address_string(), fmt % args)

        def _respond(self, response: dict) -> None:
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if response.get("ok") else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer(address, Handler)


def parse_address(value: str) -> Tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"--http expects host:port, got {value!r}")
    return host, int(port)
