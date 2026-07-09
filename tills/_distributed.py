#!/usr/bin/env python3
"""
Distributed training utilities for v7 pipeline.

SSH-based remote execution, SCP file transfer, worker status monitoring,
and the terminal progress dashboard.

All remote commands go through ``ssh`` / ``scp`` (Win11 built-in OpenSSH).
The host worker (``is_host: true``) bypasses SSH — commands run locally.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── constants ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent

# ANSI escape codes for terminal control
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CLEAR_SCREEN = "\033[2J"
_CURSOR_HOME = "\033[H"
_CLEAR_LINE = "\033[2K"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_WHITE = "\033[37m"


# ── WorkerNode ─────────────────────────────────────────────────────────────────

@dataclass
class WorkerNode:
    """Configuration and runtime state for a single worker machine."""

    id: str
    hostname: str
    ip: str
    is_host: bool = False
    ssh_user: Optional[str] = None
    ssh_port: int = 22
    ssh_key_path: Optional[str] = None
    ssh_password: Optional[str] = None  # TBD: requires sshpass or paramiko
    litegs_path: str = ""
    supersplat_path: str = ""

    # runtime state (populated during execution)
    status: str = "idle"           # idle | copying | running | done | failed | offline
    current_frame: str = ""
    current_stage: str = ""
    iteration: int = 0
    total_iterations: int = 0
    total_frames: int = 0
    completed_frames: int = 0
    failed_frames: int = 0
    elapsed_seconds: float = 0.0
    last_error: str = ""

    @property
    def ssh_target(self) -> str:
        """Return 'user@ip' for SSH commands."""
        if self.ssh_user:
            return f"{self.ssh_user}@{self.ip}"
        return self.ip

    @property
    def is_online(self) -> bool:
        """Check if the worker is reachable via ping."""
        if self.is_host:
            return True
        try:
            param = "-n 1 -w 1000" if sys.platform == "win32" else "-c 1 -W 1"
            result = subprocess.run(
                f"ping {param} {self.ip}",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return result.returncode == 0
        except Exception:
            return False


# ── config loading ─────────────────────────────────────────────────────────────

def load_workers(workers_config_path: Path) -> list[WorkerNode]:
    """Load worker definitions from a workers.json file.

    Args:
        workers_config_path: Absolute or relative path to workers.json.

    Returns:
        List of WorkerNode instances. The host worker is always first.
    """
    path = Path(workers_config_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise FileNotFoundError(f"workers.json not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    workers = []
    for w in data.get("workers", []):
        node = WorkerNode(
            id=w["id"],
            hostname=w.get("hostname", w["id"]),
            ip=w.get("ip", "127.0.0.1"),
            is_host=w.get("is_host", False),
            ssh_user=w.get("ssh_user"),
            ssh_port=w.get("ssh_port", 22),
            ssh_key_path=w.get("ssh_key_path"),
            ssh_password=w.get("ssh_password"),
            litegs_path=w.get("litegs_path", ""),
            supersplat_path=w.get("supersplat_path", ""),
        )
        workers.append(node)

    # ensure host is first in list
    hosts = [w for w in workers if w.is_host]
    remotes = [w for w in workers if not w.is_host]
    return hosts + remotes


def auto_detect_host(workers: list[WorkerNode]) -> None:
    """Set ``is_host = True`` on whichever worker matches the local machine.

    Matches by hostname first, then by IP as fallback.  If no worker
    matches, raises RuntimeError.

    Call this **once** after ``load_workers()`` — it mutates the list
    in-place so every downstream caller sees the correct host.
    """
    local_hostname = socket.gethostname().lower()
    local_ip = _get_local_ip()

    # try hostname match first
    for w in workers:
        if w.hostname.lower() == local_hostname:
            w.is_host = True
            return

    # fallback: IP match
    for w in workers:
        if w.ip == local_ip or w.ip == "127.0.0.1":
            w.is_host = True
            return

    raise RuntimeError(
        f"本机 hostname ({local_hostname}) / IP ({local_ip}) 未匹配到 "
        f"workers.json 中的任何 Worker。请在 workers.json 中确认本机的 "
        f"hostname 或 IP 已正确填写。"
    )


def _get_local_ip() -> str:
    """Return the primary LAN IP of this machine, or '127.0.0.1'."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── frame readiness detection ────────────────────────────────────────────────────

def check_frame_ready(frame_dir: Path,
                      expected_count: int | None = None,
                      stable_window: float = 5.0) -> bool:
    """Check if a frame directory is fully copied and stable.

    Takes two snapshots separated by ``stable_window`` seconds.
    Returns True only when all of:
      1. File count is stable across both snapshots
      2. File list (names) is unchanged
      3. Each file's size is unchanged
      4. File count matches ``expected_count`` (if provided)
         OR the numeric prefix of the directory name (e.g. ``"120-..."`` → 120)

    Args:
        frame_dir: Path to the frame subdirectory.
        expected_count: Explicit expected image count (from pipeline.json).
                        If None, parsed from directory name prefix.
        stable_window: Seconds to wait between samples.

    Returns:
        True if the directory appears fully copied and stable.
    """
    import time as _time

    def _snapshot(d: Path) -> tuple[int, set[str], dict[str, int]]:
        """Return (count, set_of_names, {name: size})."""
        files = list(d.iterdir())
        return (
            len(files),
            {f.name for f in files},
            {f.name: f.stat().st_size for f in files},
        )

    if not frame_dir.is_dir():
        return False

    # Determine expected count
    expected = None
    if expected_count is not None:
        expected = expected_count
    else:
        # Parse numeric prefix from dirname: "120-2026-..." → 120
        name = frame_dir.name
        try:
            prefix = name.split("-")[0]
            expected = int(prefix)
        except (ValueError, IndexError):
            expected = None  # can't determine → skip count check

    # First sample
    count1, names1, sizes1 = _snapshot(frame_dir)

    # Can't be ready if no files
    if count1 == 0:
        return False

    # Count mismatch on first sample → not ready
    if expected is not None and count1 != expected:
        return False

    # Wait for stability window
    if stable_window > 0:
        _time.sleep(stable_window)

    # Second sample
    count2, names2, sizes2 = _snapshot(frame_dir)

    # All three stability checks
    if count1 != count2:
        return False
    if names1 != names2:
        return False
    if sizes1 != sizes2:
        return False

    # Final count validation (second sample also must match)
    if expected is not None and count2 != expected:
        return False

    return True


# ── process control ──────────────────────────────────────────────────────────────

def kill_worker_process(worker: WorkerNode, status_path: str,
                        timeout: int = 15) -> tuple:
    """Kill a training process on a worker by reading its PID from status JSON.

    Args:
        worker: Target WorkerNode.
        status_path: Path to the worker's status JSON file
                     (e.g. ``results/0703/_worker_status.json``).
        timeout: SSH timeout in seconds.

    Returns:
        ``(success, message)`` — success=True if the process was killed
        or was already gone. success=False only on unexpected errors.
    """
    import signal as _signal

    # 1. Read PID from status file
    status_data = read_worker_status(worker, status_path, timeout=timeout)
    if status_data is None:
        return True, "no status file (process may have already finished)"

    pid = status_data.get("pid")
    if pid is None:
        return True, "no PID in status (nothing to kill)"

    # 2. Kill by PID
    if worker.is_host:
        try:
            os.kill(int(pid), _signal.SIGTERM)
            return True, f"killed local PID {pid}"
        except ProcessLookupError:
            return True, f"local PID {pid} already gone"
        except PermissionError as e:
            return False, f"permission denied killing PID {pid}: {e}"
        except OSError as e:
            # Windows: os.kill on invalid PID may raise OSError
            return True, f"local PID {pid} not found (OSError: {e})"
        except Exception as e:
            return False, f"unexpected error killing PID {pid}: {e}"
    else:
        # Remote: ``taskkill /f /pid`` via SSH.
        # ``2>nul & exit 0`` ensures we never get non-zero exit code
        # when the process is already gone.
        cmd = f'taskkill /f /pid {pid} 2>nul & exit 0'
        try:
            result = ssh_run(worker, cmd, timeout=timeout)
            return True, f"remote kill PID {pid}: {result.stdout.strip() or 'done'}"
        except Exception as e:
            return False, f"SSH kill failed: {e}"


# ── cleanup ──────────────────────────────────────────────────────────────────────

def cleanup_frame(worker: WorkerNode, proj_dir: Path,
                  sub_dir: str, frame_id: str,
                  level: str = "soft",
                  frame_dirname: str | None = None,
                  dry_run: bool = False) -> dict:
    """Delete training artifacts for a specific frame across all workers.

    Best-effort — missing files are silently skipped.

    Args:
        worker: WorkerNode whose artifacts to clean.
        proj_dir: Path to ``CameraData/<project>/`` on the host.
        sub_dir: MMDD sub-directory name (e.g. ``"0703"``).
        frame_id: HHMMSS frame identifier (e.g. ``"120849"``).
        level: ``"soft"`` (keep raw_images) or ``"hard"`` (delete raw_images too).
        frame_dirname: Full raw_images subdirectory name
                       (e.g. ``"120-2026-06-30-120849"``). Required for hard level.
        dry_run: If True, only compute deletion list without actually deleting.

    Returns:
        Dict with ``status``, ``level``, ``deleted`` (list of path strings),
        and ``skipped`` (list).
    """
    import shutil as _shutil

    deleted: list[str] = []
    skipped: list[str] = []

    def _rm(p: Path) -> str:
        """Try to delete a path. Returns 'deleted', 'skipped', or 'error:...'."""
        if dry_run:
            return "would_delete" if p.exists() else "skipped"
        try:
            if p.is_dir():
                _shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink(missing_ok=True)
            return "deleted"
        except Exception as e:
            return f"error: {e}"

    # 1. Supersplat PLY
    ply_path = proj_dir / f"{sub_dir}-{frame_id}.ply"
    result = _rm(ply_path)
    if "error" in result:
        skipped.append(f"ply({result})")
    else:
        deleted.append(str(ply_path))

    # 2. Worker results/<sub_dir>/
    worker_results = Path(worker.litegs_path) / "results" / sub_dir
    if worker.is_host:
        result = _rm(worker_results)
        if "error" in result:
            skipped.append(f"worker_results({result})")
        else:
            deleted.append(str(worker_results))
    else:
        if not dry_run:
            cmd = f'if exist "{worker_results}" rmdir /s /q "{worker_results}"'
            try:
                ssh_run(worker, cmd, timeout=30)
                deleted.append(f"[{worker.id}] {worker_results}")
            except Exception as e:
                skipped.append(f"[{worker.id}] worker_results({e})")
        else:
            deleted.append(f"[{worker.id}] {worker_results} (dry_run)")

    # 3. Worker data/<sub_dir>/<frame_dirname>/
    if frame_dirname:
        worker_data = Path(worker.litegs_path) / "data" / sub_dir / frame_dirname
        if worker.is_host:
            result = _rm(worker_data)
            if "error" in result:
                skipped.append(f"worker_data({result})")
            else:
                deleted.append(str(worker_data))
        else:
            if not dry_run:
                cmd = f'if exist "{worker_data}" rmdir /s /q "{worker_data}"'
                try:
                    ssh_run(worker, cmd, timeout=30)
                    deleted.append(f"[{worker.id}] {worker_data}")
                except Exception as e:
                    skipped.append(f"[{worker.id}] worker_data({e})")
            else:
                deleted.append(f"[{worker.id}] {worker_data} (dry_run)")

    # 4. Supersplat raw_images/<frame_dirname>/ (hard only)
    if level == "hard":
        if frame_dirname:
            raw_dir = proj_dir / "raw_images" / frame_dirname
            result = _rm(raw_dir)
            if "error" in result:
                skipped.append(f"raw_images({result})")
            else:
                deleted.append(str(raw_dir))
        else:
            skipped.append("raw_images(frame_dirname not provided)")

    return {"status": "ok", "level": level, "deleted": deleted, "skipped": skipped}


# ── SSH / remote execution ─────────────────────────────────────────────────────

def _build_ssh_cmd(worker: WorkerNode, remote_command: str) -> list[str]:
    """Build the ssh command list for a worker.

    On the host worker this is never called — use ``_build_local_cmd`` instead.
    """
    cmd = ["ssh"]
    if worker.ssh_key_path:
        cmd.extend(["-i", worker.ssh_key_path])
    if worker.ssh_port != 22:
        cmd.extend(["-p", str(worker.ssh_port)])
    # Disable strict host key checking for automation
    cmd.extend(["-o", "StrictHostKeyChecking=accept-new"])
    cmd.extend(["-o", "ConnectTimeout=10"])
    cmd.append(worker.ssh_target)
    cmd.append(remote_command)
    return cmd


def ssh_run(worker: WorkerNode, command: str, timeout: int = 3600,
            capture_output: bool = True) -> subprocess.CompletedProcess:
    """Execute a command on a worker and wait for completion.

    Args:
        worker: Target WorkerNode.
        command: Shell command to run on the remote machine (cmd.exe syntax).
        timeout: Seconds to wait before killing.
        capture_output: If True, capture stdout/stderr.

    Returns:
        ``subprocess.CompletedProcess`` with stdout/stderr as strings.
    """
    kwargs = dict(timeout=timeout)
    if capture_output:
        kwargs.update(capture_output=True, text=True, encoding="utf-8",
                      errors="replace")

    if worker.is_host:
        # shell=True avoids Windows list2cmdline mangling of quoted commands
        return subprocess.run(command, shell=True, **kwargs)
    else:
        return subprocess.run(_build_ssh_cmd(worker, command), **kwargs)


def ssh_run_async(worker: WorkerNode, command: str) -> subprocess.Popen:
    """Start a long-running command on a worker, return immediately.

    The returned ``Popen`` object's ``poll()`` returns ``None`` while
    the remote process is still running.

    Args:
        worker: Target WorkerNode.
        command: Shell command to run on the remote machine.

    Returns:
        ``subprocess.Popen`` — use ``.poll()`` to check, ``.wait()`` to block.
    """
    if worker.is_host:
        # shell=True passes the raw string straight to cmd.exe, avoiding
        # Windows list2cmdline mangling of an already-quoted command.
        return subprocess.Popen(
            command, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
    else:
        return subprocess.Popen(
            _build_ssh_cmd(worker, command),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )


def ssh_test(worker: WorkerNode, timeout: int = 15) -> tuple[bool, str]:
    """Quick connectivity test — run 'echo OK' on the worker.

    Returns:
        (success, message) tuple.
    """
    if worker.is_host:
        return True, "host (local)"

    try:
        result = ssh_run(worker, "echo OK", timeout=timeout)
        if result.returncode == 0 and "OK" in result.stdout:
            return True, "SSH OK"
        else:
            return False, f"SSH returned code {result.returncode}: {result.stderr or result.stdout}"
    except subprocess.TimeoutExpired:
        return False, "SSH timeout (network or auth issue)"
    except FileNotFoundError:
        return False, "ssh command not found on PATH"
    except Exception as e:
        return False, str(e)


# ── file transfer ──────────────────────────────────────────────────────────────

def _to_remote_path(worker: WorkerNode, local_path: Path, remote_base: str) -> str:
    """Convert a local path to a remote path string.

    On the host, this is just the local path as string. On remote workers,
    replace the host's base path with the worker's equivalent base.

    For v7 training, the remote_base is the worker's ``litegs_path``, and
    the local path is relative to the host's ``litegs_path``.
    """
    return str(Path(remote_base) / local_path.name)


def scp_send(worker: WorkerNode, local_path: Path, remote_path: str) -> bool:
    """Copy a file or directory from host to worker.

    On the host worker this is a plain ``shutil.copy`` (file) or
    ``shutil.copytree`` (directory).

    Args:
        worker: Target WorkerNode.
        local_path: Local file or directory to send.
        remote_path: Destination path on the remote machine (forward slashes).

    Returns:
        True on success, False on failure.
    """
    local = Path(local_path)
    if not local.exists():
        print(f"  [scp_send] source not found: {local}")
        return False

    if worker.is_host:
        dst = Path(remote_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if local.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(local, dst)
            else:
                shutil.copy2(local, dst)
            return True
        except OSError as e:
            print(f"  [scp_send] local copy failed: {e}")
            return False

    # remote worker: use SCP
    remote_target = f"{worker.ssh_target}:{remote_path}"
    scp_args = ["scp"]
    if worker.ssh_key_path:
        scp_args.extend(["-i", worker.ssh_key_path])
    if worker.ssh_port != 22:
        scp_args.extend(["-P", str(worker.ssh_port)])
    scp_args.extend(["-o", "StrictHostKeyChecking=accept-new"])
    scp_args.extend(["-o", "ConnectTimeout=10"])

    if local.is_dir():
        scp_args.extend(["-r"])

    scp_args.append(str(local))
    scp_args.append(remote_target)

    try:
        result = subprocess.run(scp_args, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300)
        if result.returncode != 0:
            print(f"  [scp_send] failed: {result.stderr}")
            return False
        return True
    except Exception as e:
        print(f"  [scp_send] error: {e}")
        return False


def scp_recv(worker: WorkerNode, remote_path: str, local_path: Path) -> bool:
    """Copy a file or directory from worker to host.

    On the host worker this is a plain ``shutil.copy`` / ``shutil.copytree``.

    Args:
        worker: Source WorkerNode.
        remote_path: Source path on the remote machine.
        local_path: Local destination path.

    Returns:
        True on success, False on failure.
    """
    local = Path(local_path)
    local.parent.mkdir(parents=True, exist_ok=True)

    if worker.is_host:
        src = Path(remote_path)
        if not src.exists():
            print(f"  [scp_recv] source not found: {src}")
            return False
        try:
            if src.is_dir():
                if local.exists():
                    shutil.rmtree(local)
                shutil.copytree(src, local)
            else:
                shutil.copy2(src, local)
            return True
        except OSError as e:
            print(f"  [scp_recv] local copy failed: {e}")
            return False

    # remote worker: use SCP
    remote_source = f"{worker.ssh_target}:{remote_path}"
    scp_args = ["scp"]
    if worker.ssh_key_path:
        scp_args.extend(["-i", worker.ssh_key_path])
    if worker.ssh_port != 22:
        scp_args.extend(["-P", str(worker.ssh_port)])
    scp_args.extend(["-o", "StrictHostKeyChecking=accept-new"])
    scp_args.extend(["-o", "ConnectTimeout=10"])
    scp_args.extend(["-r"])  # always recursive for flexibility
    scp_args.append(remote_source)
    scp_args.append(str(local))

    try:
        result = subprocess.run(scp_args, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300)
        if result.returncode != 0:
            print(f"  [scp_recv] failed: {result.stderr}")
            return False
        return True
    except Exception as e:
        print(f"  [scp_recv] error: {e}")
        return False


# ── batch SCP (multi-file in single handshake) ────────────────────────────────────

def scp_send_multi(worker: WorkerNode, local_paths: list[str],
                   remote_dst: str) -> bool:
    """Send multiple files/dirs to a worker in a single SCP call.

    One SSH handshake instead of N, avoiding per-file connection overhead.
    ``remote_dst`` must already exist on the worker.
    """
    import subprocess as _sp

    remote_target = f"{worker.ssh_target}:{remote_dst}"
    scp_args = ["scp", "-r"]
    if worker.ssh_key_path:
        scp_args.extend(["-i", worker.ssh_key_path])
    if worker.ssh_port != 22:
        scp_args.extend(["-P", str(worker.ssh_port)])
    scp_args.extend(["-o", "StrictHostKeyChecking=accept-new"])
    scp_args.extend(["-o", "ConnectTimeout=10"])
    scp_args.extend(local_paths)
    scp_args.append(remote_target)

    try:
        result = _sp.run(scp_args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=600)
        return result.returncode == 0
    except Exception:
        return False


def scp_recv_multi(worker: WorkerNode, remote_paths: list[str],
                   local_dst: Path) -> bool:
    """Pull multiple files from a worker in a single SCP call.

    The reverse of ``scp_send_multi`` — one handshake for N files.
    """
    import subprocess as _sp

    remote_src = f"{worker.ssh_target}:"
    scp_args = ["scp", "-r"]
    if worker.ssh_key_path:
        scp_args.extend(["-i", worker.ssh_key_path])
    if worker.ssh_port != 22:
        scp_args.extend(["-P", str(worker.ssh_port)])
    scp_args.extend(["-o", "StrictHostKeyChecking=accept-new"])
    scp_args.extend(["-o", "ConnectTimeout=10"])
    scp_args.extend([f"{remote_src}{rp}" for rp in remote_paths])
    scp_args.append(str(local_dst))

    try:
        result = _sp.run(scp_args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=600)
        return result.returncode == 0
    except Exception:
        return False


# ── worker status file ─────────────────────────────────────────────────────────

def read_worker_status(worker: WorkerNode, status_path: str,
                       timeout: int = 15) -> dict | None:
    """Read a JSON status file from a worker.

    Uses SSH ``type`` (cmd.exe) to dump the file content, then parses it.

    Args:
        worker: Target WorkerNode.
        status_path: Absolute or relative path to the status JSON file
                     on the remote machine.
        timeout: SSH timeout in seconds.

    Returns:
        Parsed dict on success, None if the file doesn't exist or is unreadable.
    """
    # Use cmd.exe 'type' to dump file content; 'type' returns errorlevel 1 if
    # the file doesn't exist, so we wrap it.
    cmd = f'if exist "{status_path}" type "{status_path}"'
    try:
        result = ssh_run(worker, cmd, timeout=timeout)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout.strip())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return None


def write_worker_status(status_path: str, data: dict) -> None:
    """Write a worker status JSON file atomically (write-tmp + rename).

    Called locally by batch_run.py on each worker. Not an SSH operation.
    """
    p = Path(status_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(p)  # atomic on Windows (within same volume)


# ── progress display ───────────────────────────────────────────────────────────

class ProgressDisplay:
    """Terminal dashboard that shows all workers' training progress.

    Refreshes in-place every ``render()`` call. Uses ANSI escape codes
    (works on Windows Terminal, PowerShell 5.1+, cmd.exe with VT support).
    """

    def __init__(self, workers: list[WorkerNode], project_name: str = "",
                 sub_dir: str = "", new_frame_count: int = 0,
                 existing_ply_count: int = 0):
        self._workers = workers
        self._project = project_name
        self._sub_dir = sub_dir
        self._new_frames = new_frame_count
        self._existing_ply = existing_ply_count
        self._start_time = time.time()
        self._lines_printed = 0

    def update(self, worker_statuses: list[dict | None]) -> None:
        """Update worker states from status dicts (one per worker, same order)."""
        for w, s in zip(self._workers, worker_statuses):
            if s is None:
                # No status file yet — worker might still be copying or just started
                if w.status not in ("done", "failed", "offline", "running"):
                    w.status = "preparing"
                continue
            w.status = s.get("status", w.status)
            w.current_frame = s.get("current_frame", w.current_frame)
            w.current_stage = s.get("current_stage", w.current_stage)
            w.iteration = s.get("iteration", w.iteration)
            w.total_iterations = s.get("total_iterations", w.total_iterations)
            w.total_frames = s.get("total_frames", w.total_frames)
            w.completed_frames = s.get("completed_frames", w.completed_frames)
            w.failed_frames = s.get("failed_frames", w.failed_frames)
            w.elapsed_seconds = s.get("elapsed_seconds", w.elapsed_seconds)

    def render(self) -> None:
        """(Re)draw the dashboard in the terminal."""
        # Move cursor to home, then overwrite
        lines: list[str] = []
        elapsed = time.time() - self._start_time
        elapsed_str = _format_seconds(elapsed)

        # ── header ──
        lines.append("")
        lines.append(f"{_BOLD}{'═' * 70}{_RESET}")
        lines.append(
            f"  {_BOLD}v7 分布式训练{_RESET} — "
            f"project: {_CYAN}{self._project}{_RESET}  "
            f"sub_dir: {_CYAN}{self._sub_dir}{_RESET}  "
            f"新增: {_YELLOW}{self._new_frames}{_RESET} 帧  "
            f"总耗时: {elapsed_str}"
        )
        lines.append(f"{'═' * 70}")

        # ── column headers ──
        lines.append(
            f"  {_BOLD}{'Worker':<10} {'分配':<6} {'状态':<12} "
            f"{'当前帧':<28} {'耗时':<8} {'阶段'}{_RESET}"
        )
        lines.append(f"  {'─' * 68}")

        # ── worker rows ──
        done_count = 0
        failed_count = 0
        offline_count = 0
        running_count = 0

        for w in self._workers:
            status_color = _WHITE
            status_str = w.status

            if w.status == "done":
                status_color = _GREEN
                done_count += 1
            elif w.status == "running" or w.status == "preparing":
                status_color = _CYAN
                if w.status == "running":
                    running_count += 1
            elif w.status == "failed":
                status_color = _RED
                failed_count += 1
            elif w.status == "offline":
                status_color = _RED
                offline_count += 1
            elif w.status == "idle":
                status_color = _DIM
                status_str = "闲置"

            # frame count for this worker
            frame_count = w.total_frames
            frame_disp = f"{frame_count}帧" if frame_count else "—"

            # current frame display (truncate if long)
            cur_frame = w.current_frame or "—"
            if len(cur_frame) > 26:
                cur_frame = cur_frame[:23] + "..."

            # stage
            stage = w.current_stage or ("—" if w.status in ("idle", "done") else "...")
            if w.status == "running" and w.iteration and w.total_iterations:
                stage = f"iter {w.iteration}/{w.total_iterations}"

            # elapsed
            elapsed_w = _format_seconds(w.elapsed_seconds) if w.elapsed_seconds else "—"

            lines.append(
                f"  {status_color}{w.id:<10} {frame_disp:<6} {status_str:<12} "
                f"{cur_frame:<28} {elapsed_w:<8} {stage}{_RESET}"
            )

        # ── summary ──
        lines.append(f"  {'─' * 68}")
        total_frames_done = sum(w.completed_frames for w in self._workers)
        total_frames_failed = sum(w.failed_frames for w in self._workers)
        summary_parts = []
        if running_count:
            summary_parts.append(f"{running_count} 训练中")
        if done_count:
            summary_parts.append(f"{done_count} 完成")
        if failed_count:
            summary_parts.append(f"{failed_count} 失败")
        if offline_count:
            summary_parts.append(f"{offline_count} 离线")
        summary_str = ", ".join(summary_parts) if summary_parts else "等待中..."

        ply_info = ""
        if self._existing_ply:
            total_ply = self._existing_ply + total_frames_done
            ply_info = f" | 已有 PLY: {self._existing_ply} → fuse 可选: 1-{total_ply}"

        lines.append(
            f"  整体: {total_frames_done}/{self._new_frames} 完成, "
            f"{summary_str}{ply_info}"
        )
        lines.append(f"{'═' * 70}")

        # ── output ──
        text = "\n".join(lines)

        # Clear previous output if needed (first render uses clear-screen,
        # subsequent renders overwrite previous lines)
        if hasattr(self, '_first_render') and self._first_render:
            self._first_render = False

        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def close(self) -> None:
        """Finalize the display (no-op for now)."""
        pass


def _format_seconds(seconds: float) -> str:
    """Format seconds as mm:ss or hh:mm:ss."""
    s = int(seconds)
    if s < 3600:
        return f"{s // 60:02d}:{s % 60:02d}"
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ── quick validation helper ────────────────────────────────────────────────────

def validate_workers(workers: list[WorkerNode]) -> dict[str, tuple[bool, str]]:
    """Test SSH connectivity to all non-host workers.

    Returns:
        Dict mapping worker id → (ok, message).
    """
    results = {}
    for w in workers:
        if w.is_host:
            results[w.id] = (True, "host (local)")
        else:
            ok, msg = ssh_test(w)
            results[w.id] = (ok, msg)
    return results


# ── standalone test (run directly: python tills/_distributed.py <workers.json>) ──

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {Path(__file__).name} <workers.json>")
        print(f"  Validates SSH connectivity to all workers defined in the JSON file.")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    print(f"Loading workers from: {config_path}")
    workers = load_workers(config_path)
    print(f"Found {len(workers)} worker(s):")
    for w in workers:
        host_tag = " [HOST]" if w.is_host else ""
        print(f"  {w.id}: {w.ip} ({w.ssh_target}){host_tag}")
        print(f"    litegs_path: {w.litegs_path}")

    print(f"\nTesting connectivity...")
    results = validate_workers(workers)
    all_ok = True
    for wid, (ok, msg) in results.items():
        status = f"{_GREEN}OK{_RESET}" if ok else f"{_RED}FAIL{_RESET}"
        print(f"  {wid}: {status} — {msg}")
        if not ok:
            all_ok = False

    if all_ok:
        print(f"\n{_GREEN}All workers reachable.{_RESET}")
    else:
        print(f"\n{_RED}Some workers unreachable — check IP, SSH server, and credentials.{_RESET}")
        sys.exit(1)