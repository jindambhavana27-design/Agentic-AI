"""stdlib HTTP adapter.

The only job here is translating ``BaseHTTPRequestHandler`` into the
transport-neutral :class:`~service.app.Request`/:class:`~service.app.Response`
pair. Keeping the adapter this thin is what makes the application testable
without a socket, and what would make a swap to ASGI a contained change.
"""

from __future__ import annotations

import signal
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .app import Application, Request, parse_target
from .config import Config
from .observability import configure_logging, get_logger

_log = get_logger("server")

MAX_REQUEST_BYTES = 1 * 1024 * 1024


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "url-shortener"
    sys_version = ""  # do not advertise the Python version

    application: Application  # injected via the server instance

    def _run(self, method: str) -> None:
        try:
            path, query = parse_target(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_REQUEST_BYTES:
                self._send(413, b'{"error":{"code":"payload_too_large"}}',
                           {"Content-Type": "application/json"})
                return
            body = self.rfile.read(length) if length > 0 else b""
            request = Request(
                method=method,
                path=path,
                query=query,
                headers={k.lower(): v for k, v in self.headers.items()},
                body=body,
                remote_addr=self.client_address[0] if self.client_address else "unknown",
            )
            response = self.server.application.handle(request)
            self._send(response.status, response.body, response.headers)
        except (BrokenPipeError, ConnectionResetError):  # client hung up
            return
        except Exception as exc:  # pragma: no cover - last-resort guard
            _log.exception("handler crashed: %s" % exc)
            try:
                self._send(500, b'{"error":{"code":"internal_error"}}',
                           {"Content-Type": "application/json"})
            except Exception:
                return

    def _send(self, status: int, body: bytes, headers: dict) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        self._run("GET")

    def do_POST(self) -> None:
        self._run("POST")

    def do_DELETE(self) -> None:
        self._run("DELETE")

    def do_PUT(self) -> None:
        self._run("PUT")

    def do_PATCH(self) -> None:
        self._run("PATCH")

    def do_HEAD(self) -> None:
        self._run("GET")

    def log_message(self, fmt: str, *args) -> None:
        # Access logging is handled by the application in structured form.
        return


class ShortenerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: Config, application: Optional[Application] = None) -> None:
        self.application = application or Application(config)
        self.config = config
        super().__init__((config.host, config.port), _Handler)

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()

    @property
    def bound_port(self) -> int:
        return self.server_address[1]

    def shutdown_gracefully(self) -> None:
        self.shutdown()
        self.server_close()
        self.application.close()


def serve(config: Optional[Config] = None) -> None:
    config = config or Config.from_env()
    configure_logging(config.log_level, config.service_name)
    server = ShortenerServer(config)
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    _log.info(
        "listening",
        extra={"host": config.host, "port": server.bound_port, "base_url": config.public_base},
    )

    stop = threading.Event()

    def _on_signal(signum, _frame):
        _log.info("shutdown signal received", extra={"signal": signum})
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _on_signal)

    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        # Drain in-flight analytics and close the store before exiting so a
        # rolling restart does not lose buffered writes.
        server.shutdown_gracefully()
        _log.info("stopped")


if __name__ == "__main__":  # pragma: no cover
    try:
        serve()
    except Exception as exc:
        print("fatal: %s" % exc, file=sys.stderr)
        sys.exit(1)
