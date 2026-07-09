"""Tests for server/_server.py — SSE wire format and HTTP handler logic."""

import json
import queue
import threading
import time
from http.server import HTTPServer

from tills.server._server import (
    sse_event, SSEBroadcaster, SSEHandler, create_server,
)


# ── SSE wire format ──────────────────────────────────────────────────────────────

def test_sse_event_format():
    """SSE event must follow the text/event-stream wire format."""
    result = sse_event("log", "iter 1000/30000 loss=0.003")
    assert result == "event: log\ndata: iter 1000/30000 loss=0.003\n\n", \
        f"unexpected format: {repr(result)}"


def test_sse_event_multiline_data():
    """Multi-line data must have each line prefixed with 'data:'."""
    result = sse_event("error", "line1\nline2")
    lines = result.strip().split("\n")
    assert lines[0] == "event: error"
    assert lines[1] == "data: line1"
    assert lines[2] == "data: line2"
    assert result.endswith("\n\n")


def test_sse_event_special_chars():
    """JSON payloads must round-trip through SSE cleanly."""
    data = json.dumps({"pid": 123, "status": "running"})
    result = sse_event("status", data)
    assert "data: " in result
    assert result.endswith("\n\n")
    # The JSON should be recoverable
    lines = result.strip().split("\n")
    data_line = lines[1]  # "data: {...}"
    payload = data_line[6:]  # strip "data: " prefix
    parsed = json.loads(payload)
    assert parsed["pid"] == 123


def test_sse_event_empty_data():
    """Empty data should still produce valid SSE format."""
    result = sse_event("ping", "")
    assert result == "event: ping\ndata: \n\n"


# ── SSE Broadcaster ──────────────────────────────────────────────────────────────

def test_broadcast_single_subscriber():
    """A single subscriber receives all broadcast events."""
    bc = SSEBroadcaster()
    q = bc.subscribe()

    bc.broadcast("test", "hello")
    msg = q.get(timeout=1)
    assert "event: test" in msg
    assert "data: hello" in msg


def test_broadcast_multiple_subscribers():
    """Multiple subscribers each receive a copy of every event."""
    bc = SSEBroadcaster()
    q1 = bc.subscribe()
    q2 = bc.subscribe()
    q3 = bc.subscribe()

    bc.broadcast("update", "v1")
    for q in (q1, q2, q3):
        msg = q.get(timeout=1)
        assert "data: v1" in msg


def test_unsubscribe_removes_subscriber():
    """Unsubscribed queues should not receive future events."""
    bc = SSEBroadcaster()
    q = bc.subscribe()
    bc.unsubscribe(q)

    bc.broadcast("test", "should not arrive")
    try:
        q.get(timeout=0.2)
        assert False, "unsubscribed queue should be empty"
    except queue.Empty:
        pass  # expected


def test_broadcast_removes_dead_subscribers():
    """When a subscriber's queue is full, it should be auto-removed."""
    bc = SSEBroadcaster()
    # Create a queue with maxsize=0 (already full)
    full_q = queue.Queue(maxsize=1)
    full_q.put_nowait("blocking item")  # fill it
    with bc._lock:
        bc._subscribers.append(full_q)

    # Broadcast should not hang and should remove the dead subscriber
    bc.broadcast("test", "data")
    with bc._lock:
        assert full_q not in bc._subscribers


# ── SSEHandler routing ───────────────────────────────────────────────────────────

class _TestHandler(SSEHandler):
    """Minimal handler for testing route dispatch."""
    routes = {}
    sse_paths = set()

    @classmethod
    def configure(cls, routes_dict, sse_set):
        cls.routes = routes_dict
        cls.sse_paths = sse_set


def test_create_server():
    """create_server should return a configured HTTPServer."""
    bc = SSEBroadcaster()
    server = create_server("127.0.0.1", 0, SSEHandler, broadcaster=bc)
    assert isinstance(server, HTTPServer)
    assert server.server_address[0] == "127.0.0.1"
    server.server_close()
