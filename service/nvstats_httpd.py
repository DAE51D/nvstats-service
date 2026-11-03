#!/usr/bin/env python3
"""nvstats HTTP front-end (read-only, stdlib only -- no D-Bus needed).

  GET /health                 liveness + version, never needs the token
  GET /api/v1/sample          latest sample, minimal view (what a small
                              display needs); ?full=1 for the full sample
  GET /api/v1/stream          Server-Sent Events, one `sample` event per new
                              sample (same ?full=1 switch)

Process control (kill/restart) is deliberately not exposed here.

Configuration (environment):
  NVSTATS_HTTP_BIND    address to listen on          (default 127.0.0.1)
  NVSTATS_HTTP_PORT    port                          (default 8039)
  NVSTATS_TOKEN        if set, require `Authorization: Bearer <token>`
  NVSTATS_REDACT_ARGS  mask secret-looking CLI option values (default 1)

Run standalone (`python3 nvstats_httpd.py`) on headless hosts, or have
nvstats_service.py start it next to the D-Bus API (NVSTATS_HTTP=1).
"""

import hmac
import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"),
)

import nvstats_core as core  # noqa: E402

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8039
KEEPALIVE_SECONDS = 15
FIRST_SAMPLE_WAIT = 5

log = logging.getLogger("nvstats.http")


class HttpConfig:
    def __init__(self, bind, port, token, redact):
        self.bind, self.port, self.token, self.redact = bind, port, token, redact

    @classmethod
    def from_env(cls, env=os.environ):
        try:
            port = int(env.get("NVSTATS_HTTP_PORT", DEFAULT_PORT))
        except ValueError:
            port = DEFAULT_PORT
        return cls(
            env.get("NVSTATS_HTTP_BIND", DEFAULT_BIND),
            port,
            env.get("NVSTATS_TOKEN") or None,
            env.get("NVSTATS_REDACT_ARGS", "1").lower() not in ("0", "false", "no", ""),
        )


def make_handler(sampler, cfg):
    class Handler(BaseHTTPRequestHandler):
        server_version = "nvstats/" + core.VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # journal stays quiet on normal requests

        def _send(self, code, payload, headers=()):
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if not cfg.token:
                return True
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), ("Bearer " + cfg.token).encode())

        def _view(self, sample, full):
            if full:
                return core.view_full(sample, cfg.redact)
            return core.view_minimal(sample, cfg.redact)

        def do_GET(self):
            url = urlsplit(self.path)
            full = parse_qs(url.query).get("full", ["0"])[0].lower() in ("1", "true", "yes")
            if url.path == "/health":
                s = sampler.latest()
                return self._send(
                    200,
                    {
                        "status": "ok" if s else "starting",
                        "version": core.VERSION,
                        "git_commit": core.GIT_COMMIT,
                        "schema": core.SCHEMA,
                        "host": s["host"] if s else None,
                        "backend": s["gpu"]["backend"] if s else None,
                    },
                )
            if url.path not in ("/api/v1/sample", "/api/v1/stream"):
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(
                    401,
                    {"error": "unauthorized"},
                    [("WWW-Authenticate", 'Bearer realm="nvstats"')],
                )
            if url.path == "/api/v1/sample":
                sample = sampler.latest() or sampler.wait_newer(0, FIRST_SAMPLE_WAIT)
                if sample is None:
                    return self._send(503, {"error": "no sample yet"}, [("Retry-After", "1")])
                return self._send(200, self._view(sample, full))
            self._stream(full)

        def _stream(self, full):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            seq = 0
            try:
                while True:
                    sample = sampler.wait_newer(seq, KEEPALIVE_SECONDS)
                    if sample is None:
                        self.wfile.write(b": keepalive\n\n")
                    else:
                        seq = sample["seq"]
                        data = json.dumps(self._view(sample, full), separators=(",", ":"))
                        self.wfile.write(
                            f"id: {seq}\nevent: sample\ndata: {data}\n\n".encode()
                        )
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True  # open SSE streams must not block shutdown
    allow_reuse_address = True


def serve(sampler, cfg=None):
    """Create the HTTP server (not yet serving). Caller runs serve_forever()."""
    cfg = cfg or HttpConfig.from_env()
    server = Server((cfg.bind, cfg.port), make_handler(sampler, cfg))
    if cfg.bind not in ("127.0.0.1", "::1", "localhost") and not cfg.token:
        log.warning("listening on %s:%d without NVSTATS_TOKEN", cfg.bind, cfg.port)
    return server


def start_in_thread(sampler, cfg=None):
    server = serve(sampler, cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="nvstats: %(message)s")
    sampler = core.Sampler(detect_kill=False)
    sampler.start()
    server = serve(sampler)
    host, port = server.server_address[:2]
    log.info("v%s (%s) listening on %s:%d", core.VERSION, core.GIT_COMMIT, host, port)

    def on_signal(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    server.serve_forever()
    sampler.stop()


if __name__ == "__main__":
    main()
