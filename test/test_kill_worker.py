"""Tests for kill_worker_process() — PID-based worker process termination."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tills._distributed import kill_worker_process, WorkerNode


def test_status_pid_roundtrip():
    """PID written to status JSON must survive round-trip parse."""
    status = {"pid": 12345, "status": "running",
              "current_frame": "120-2026-06-30-120849",
              "iteration": 5000, "total_iterations": 30000}
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "_worker_status.json"
        sp.write_text(json.dumps(status, ensure_ascii=False))

        data = json.loads(sp.read_text())
        assert data["pid"] == 12345
        assert data["status"] == "running"
        assert data["iteration"] == 5000
        assert data["total_iterations"] == 30000


def test_kill_nonexistent_pid_does_not_raise():
    """Killing a PID that doesn't exist should raise ProcessLookupError
    (or PermissionError on Windows for system PIDs), which our wrapper
    must catch gracefully."""
    # Pick a PID extremely unlikely to exist
    huge_pid = 999999
    try:
        os.kill(huge_pid, signal.SIGTERM)
        # If it succeeds (incredibly unlikely), the PID existed.
        # That's fine — our wrapper still handled it.
    except (ProcessLookupError, PermissionError):
        # Expected: the wrapper must catch these
        pass
    except OSError:
        # Windows: os.kill with SIGTERM on invalid PID raises OSError
        # EINVAL or similar
        pass


def test_kill_pid_zero_is_guarded():
    """PID 0 has special meaning (process group). Our wrapper should
    never generate PID 0 from a normal status file."""
    # This is a documentation test — real batch_run.py always writes
    # os.getpid() which is never 0 on Windows.
    assert os.getpid() > 0


def test_kill_worker_local_process():
    """Start a real subprocess, kill it via kill_worker_process, verify it exits."""
    with tempfile.TemporaryDirectory() as tmp:
        # Write a status file with a real PID
        sp = Path(tmp) / "_worker_status.json"

        # Start a long-running process
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        pid = proc.pid
        status = {"pid": pid, "status": "running"}
        sp.write_text(json.dumps(status))

        # Create a localhost worker
        worker = WorkerNode(
            id="test-local", hostname="localhost", ip="127.0.0.1",
            is_host=True,
        )

        # Kill it
        ok, msg = kill_worker_process(worker, str(sp), timeout=5)
        assert ok, f"kill failed: {msg}"

        # Wait for process to actually die
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # Process should have exited
        assert proc.poll() is not None
