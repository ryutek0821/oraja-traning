"""Liveness, readiness, and version handlers for the Python Container."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def json_response(
    handler: BaseHTTPRequestHandler,
    payload: dict[str, Any],
    *,
    status: int = 200,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class HealthHandler(BaseHTTPRequestHandler):
    """Base handler shared by health-only and processing entrypoints."""

    ready = True

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path in {"/", "/healthz"}:
            json_response(
                self,
                {
                    "status": "ok",
                    "component": "python-processor",
                    "version": os.environ.get("BUILD_VERSION", "container-local"),
                },
            )
            return
        if self.path == "/readyz":
            json_response(
                self,
                {
                    "status": "ready" if self.ready else "not_ready",
                    "component": "python-processor",
                    "version": os.environ.get("BUILD_VERSION", "container-local"),
                },
                status=200 if self.ready else 503,
            )
            return
        if self.path == "/version":
            json_response(
                self,
                {
                    "service": "oraja-training/python-processor",
                    "version": os.environ.get("BUILD_VERSION", "container-local"),
                },
            )
            return
        json_response(self, {"error": {"code": "not_found"}}, status=404)

    def log_message(self, *_args: object) -> None:
        # Request paths may contain profile/object identifiers.  Do not emit
        # them to stdout, which is retained as an operational log.
        return


def serve(handler: type[BaseHTTPRequestHandler]) -> None:
    """Start the standard-library server used by the Cloudflare Container."""

    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()
