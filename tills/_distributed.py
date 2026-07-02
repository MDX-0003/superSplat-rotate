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