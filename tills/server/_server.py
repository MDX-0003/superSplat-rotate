#!/usr/bin/env python3
"""
Minimal HTTP/SSE server shared by train_daemon and fuse_server.

Zero dependencies beyond Python stdlib.  Not a general-purpose framework —
only the features actually needed by v8 daemons.
"""

import json
import queue
import threading
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Callable


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in its own thread.

    Without this, a long-lived SSE connection blocks all other requests
    (including page refreshes) because stdlib ``HTTPServer`` is single-threaded.
    """
    daemon_threads = True  # threads exit when main thread exits


# ── SSE wire format ──────────────────────────────────────────────────────────────

def sse_event(event: str, data: str) -> str:
    """Format a single SSE message.

    Args:
        event: Event type (e.g. ``"log"``, ``"status"``, ``"done"``).
        data: Payload string. Multi-line data is correctly prefixed.

    Returns:
        Wire-format string ready to write to an SSE response body.
    """
    lines = [f"event: {event}"]
    for line in data.split("\n"):
        lines.append(f"data: {line}")
    lines.append("")  # terminating blank line required by SSE spec
    return "\n".join(lines) + "\n"


# ── SSE event queue ──────────────────────────────────────────────────────────────

class SSEBroadcaster:
    """Thread-safe fan-out: one publisher → many SSE subscribers.

    Each subscriber gets its own ``queue.Queue``.  The publisher calls
    ``broadcast(event, data)`` and every subscriber receives a copy.
    """

    def __init__(self):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        """Register a new subscriber. Returns a Queue to read from."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Remove a subscriber (call on client disconnect)."""
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def broadcast(self, event: str, data: str) -> None:
        """Push an event to all subscribers."""
        message = sse_event(event, data)
        with self._lock:
            dead: list[queue.Queue] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(message)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self._subscribers.remove(q)
                except ValueError:
                    pass


# ── HTTP request handler ─────────────────────────────────────────────────────────

class SSEHandler(BaseHTTPRequestHandler):
    """Base handler with SSE support.

    Subclass and assign to ``routes`` and ``sse_paths`` class attributes.
    """

    # Override in subclass: dict of path → callable
    routes: dict[str, Callable] = {}

    # Override in subclass: set of SSE endpoint paths
    sse_paths: set[str] = set()

    # Set by create_server() after construction
    broadcaster: SSEBroadcaster | None = None

    def log_message(self, format, *args):
        """Suppress default access-log noise to stderr."""
        pass

    def handle(self):
        """Handle one or more requests, suppressing connection-abort noise.

        ``ConnectionAbortedError`` fires at the framework level (in
        ``self.rfile.readline()``) before any ``do_GET``/``do_POST``
        handler runs, so the only place to catch it is here.
        """
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # browser refreshed/navigated away — not an error

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in self.sse_paths:
            self._handle_sse()
            return

        handler = self.routes.get(path)
        if handler:
            try:
                body, content_type = handler(self)
                self._respond(200, body, content_type)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # client disconnected — nothing we can do
            except Exception:
                self._respond(500, traceback.format_exc(), "text/plain")
        else:
            self._respond(404, f"Not Found: {path}", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length else b""
        body_raw = body_bytes.decode("utf-8", errors="replace")

        # Parse as JSON if content-type indicates
        body = body_raw
        if "application/json" in self.headers.get("Content-Type", ""):
            try:
                body = json.loads(body_raw)
            except json.JSONDecodeError:
                pass

        handler = self.routes.get(path)
        if handler:
            try:
                result, content_type = handler(self, body)
                self._respond(200, result, content_type)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # client disconnected
            except Exception:
                self._respond(500, traceback.format_exc(), "text/plain")
        else:
            self._respond(404, f"Not Found: {path}", "text/plain")

    # ── internal helpers ──────────────────────────────────────────────────────

    def _handle_sse(self):
        """Set up an SSE stream."""
        if self.broadcaster is None:
            self._respond(500, "broadcaster not initialized", "text/plain")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = self.broadcaster.subscribe()
        try:
            while True:
                try:
                    message = q.get(timeout=1)
                    self.wfile.write(message.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # heartbeat: keep connection alive, also gives the
                    # thread a chance to notice client disconnect quickly
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.broadcaster.unsubscribe(q)

    def _respond(self, code: int, body, content_type: str):
        """Send an HTTP response. Silently drops on client disconnect."""
        if isinstance(body, (dict, list)):
            body_str = json.dumps(body, ensure_ascii=False)
            if "text/plain" in content_type:
                content_type = "application/json; charset=utf-8"
        else:
            body_str = str(body)

        body_bytes = body_str.encode("utf-8")

        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body_bytes)
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, OSError):
            pass  # client disconnected — not an error worth logging


# ── Server factory ───────────────────────────────────────────────────────────────

def create_server(host: str, port: int, handler_class: type,
                  broadcaster: SSEBroadcaster | None = None) -> HTTPServer:
    """Create and configure an HTTP server.

    Args:
        host: Bind address (e.g. ``"0.0.0.0"``, ``"127.0.0.1"``).
        port: Port number.
        handler_class: Subclass of ``SSEHandler`` with routes and sse_paths set.
        broadcaster: Shared SSE broadcaster instance.

    Returns:
        Configured ``HTTPServer`` (not yet started).
    """
    server = ThreadingHTTPServer((host, port), handler_class)
    # Set on the class so all request instances see it
    handler_class.broadcaster = broadcaster
    return server


def run_server(server: HTTPServer,
               ready_event: threading.Event | None = None):
    """Blocking call — runs the HTTP server until KeyboardInterrupt.

    Args:
        server: Configured ``HTTPServer``.
        ready_event: Optional event to signal when the server is accepting
                     connections.
    """
    host, port = server.server_address
    print(f"  HTTP server listening on http://{host}:{port}")
    if ready_event:
        ready_event.set()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print(f"  HTTP server stopped.")
