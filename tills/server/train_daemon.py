#!/usr/bin/env python3
"""
Train Daemon — continuous polling, frame dispatch, training monitor.

Usage:
  python -m tills.server.train_daemon --config CameraData/05/pipeline.json
  python -m tills.server.train_daemon --config CameraData/05/pipeline.json --port 8080

Start this and leave it running.  Open http://localhost:8080 to monitor.
"""

import argparse
import json
import os
import shutil
import signal
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

# ── Add project root for sibling imports ──
_this_dir = Path(__file__).resolve().parent
_project_root = _this_dir.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from tills._distributed import (
    WorkerNode, load_workers, auto_detect_host, validate_workers,
    ssh_run_async, kill_worker_process, cleanup_frame,
    read_worker_status, scp_send_multi, ROOT,
)
from tills._shared import load_preset, parse_frame_dirname
from tills.server._server import (
    SSEBroadcaster, SSEHandler, create_server, run_server,
)


# ── Logging ──────────────────────────────────────────────────────────────────────

class DaemonLogger:
    """Writes daemon events and per-worker training output to disk.

    Directory layout::

        CameraData/<proj>/logs/daemon-20260709-143000/
        ├── daemon.log       # scan, dispatch, errors, lifecycle
        ├── host.log          # host worker training stdout
        └── worker1.log       # remote worker training stdout (one per worker)

    All writes are flushed immediately so logs survive a crash.
    """

    def __init__(self, proj_dir: Path):
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._log_dir = proj_dir / "logs" / f"daemon-{ts}"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, object] = {}  # name → file handle
        print(f"  logs → {self._log_dir}")

    def _write(self, name: str, line: str):
        """Append a line to a named log file."""
        if name not in self._handles:
            path = self._log_dir / f"{name}.log"
            self._handles[name] = open(path, "a", encoding="utf-8", buffering=1)
        f = self._handles[name]
        ts = time.strftime("%H:%M:%S")
        f.write(f"[{ts}] {line}\n")
        f.flush()

    def daemon(self, msg: str):
        """Log a daemon-level event."""
        self._write("daemon", msg)

    def worker(self, worker_id: str, line: str):
        """Log a line of training output from a worker."""
        self._write(worker_id, line)

    def close(self):
        for h in self._handles.values():
            try:
                h.close()
            except Exception:
                pass


# ── State ────────────────────────────────────────────────────────────────────────

@dataclass
class FrameState:
    """Runtime state for a single frame being tracked."""
    frame_id: str
    sub_dir: str
    dirname: str = ""          # raw_images/ subdirectory name
    status: str = "new"        # new → checking → ready → copying → training → done | failed
    worker_id: str = ""
    iteration: int = 0
    total_iterations: int = 0
    error_message: str = ""
    retry_count: int = 0


class TrainState:
    """In-memory state for all tracked frames and workers. Thread-safe."""

    def __init__(self, project: str, poll_interval: int = 5):
        self.project = project
        self.poll_interval = poll_interval
        self.frames: dict[str, FrameState] = {}     # "SUBDIR-FRAMEID" → FrameState
        self.workers: list[WorkerNode] = []
        self.running_processes: dict[str, tuple] = {}  # "KEY" → (WorkerNode, Popen)
        self._lock = threading.Lock()

    def add_frame(self, frame_id: str, sub_dir: str, dirname: str = ""):
        """Add a frame if not already tracked (idempotent)."""
        key = f"{sub_dir}-{frame_id}"
        with self._lock:
            if key not in self.frames:
                self.frames[key] = FrameState(
                    frame_id=frame_id, sub_dir=sub_dir, dirname=dirname,
                )

    def update_frame(self, key: str, **kwargs):
        """Update fields on a tracked frame. No-op if frame not found."""
        with self._lock:
            if key in self.frames:
                for k, v in kwargs.items():
                    if hasattr(self.frames[key], k):
                        setattr(self.frames[key], k, v)

    def get_frame(self, key: str) -> FrameState | None:
        """Get a frame by key, or None."""
        with self._lock:
            return self.frames.get(key)

    def to_dict(self) -> dict:
        """Serialize current state for JSON/SSE."""
        with self._lock:
            frame_list = []
            for key, fs in sorted(self.frames.items()):
                frame_list.append({
                    "key": key,
                    "frame_id": fs.frame_id,
                    "sub_dir": fs.sub_dir,
                    "dirname": fs.dirname,
                    "status": fs.status,
                    "worker_id": fs.worker_id,
                    "iteration": fs.iteration,
                    "total_iterations": fs.total_iterations,
                    "error_message": fs.error_message,
                    "retry_count": fs.retry_count,
                })
            worker_list = []
            for w in self.workers:
                worker_list.append({
                    "id": w.id,
                    "is_host": w.is_host,
                    "is_online": w.is_online,
                    "status": w.status,
                    "current_frame": w.current_frame,
                })
            return {
                "project": self.project,
                "poll_interval": self.poll_interval,
                "frames": frame_list,
                "workers": worker_list,
            }


# ── HTML page builder ────────────────────────────────────────────────────────────

_PAGE_CSS = """
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:Consolas,monospace;background:#1a1a2e;color:#e0e0e0;
       padding:20px}
  h1{color:#7ec8e3;margin-bottom:10px}
  h2{color:#7ec8e3;margin:15px 0 10px}
  .info{color:#888;margin-bottom:20px;font-size:14px}
  table{width:100%;border-collapse:collapse;margin-bottom:20px}
  th{text-align:left;padding:8px 10px;background:#16213e;color:#7ec8e3;
     font-size:13px}
  td{padding:8px 10px;border-bottom:1px solid #16213e;font-size:13px}
  tr:hover{background:#16213e}
  .st-new{color:#888}
  .st-checking{color:#f0c040}
  .st-ready{color:#4caf50}
  .st-copying{color:#2196f3}
  .st-training{color:#03a9f4;font-weight:bold}
  .st-done{color:#4caf50}
  .st-failed{color:#f44336}
  button{background:#2196f3;color:#fff;border:none;padding:4px 10px;
         cursor:pointer;border-radius:3px;font-size:12px;margin:1px}
  button.danger{background:#f44336}
  button.warn{background:#ff9800}
  button:hover{opacity:0.8}
  button:disabled{opacity:0.4;cursor:default}
  .log-panel{background:#0d1117;border:1px solid #30363d;border-radius:4px;
             margin-bottom:10px}
  .log-header{padding:6px 12px;background:#161b22;cursor:pointer;
              display:flex;justify-content:space-between;font-size:13px}
  .log-body{padding:8px 12px;max-height:300px;overflow-y:auto;
            font-size:12px;line-height:1.5;display:none;white-space:pre-wrap;
            font-family:Consolas,monospace}
  .log-body.open{display:block}
  .log-body::-webkit-scrollbar{width:6px}
  .log-body::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
</style>
"""

_JS_SSE = """
<script>
  const evtSource = new EventSource('/events');
  evtSource.addEventListener('status', function(e) {
    const data = JSON.parse(e.data);
    for (const f of data.frames) {
      let row = document.getElementById('row-' + f.key);
      if (!row) { location.reload(); return; }
      row.cells[1].innerHTML = '<span class="st-' + f.status + '">'
                               + f.status + '</span>';
      row.cells[2].textContent = f.worker_id || '—';
      const iter = f.total_iterations ? f.iteration + '/' + f.total_iterations : '—';
      row.cells[3].textContent = iter;
    }
    for (const w of data.workers) {
      let el = document.getElementById('worker-' + w.id);
      if (el) {
        el.textContent = (w.is_online ? '🟢' : '🔴')
                       + ' ' + w.id
                       + (w.current_frame ? ' (' + w.current_frame + ')' : '');
      }
    }
  });
  evtSource.addEventListener('log', function(e) {
    const parts = e.data.split(' ', 2);
    const wid = parts[0];
    const msg = e.data.slice(wid.length + 1);
    for (let panel of document.querySelectorAll('.log-body')) {
      if (panel.dataset.worker === wid) {
        panel.textContent += msg + '\\n';
        panel.scrollTop = panel.scrollHeight;
      }
    }
  });
  function toggleLog(id) {
    document.getElementById(id).classList.toggle('open');
  }
  function doAction(key, action, level) {
    let body = JSON.stringify({key: key, action: action, level: level || 'soft'});
    fetch('/action', {method:'POST',
     headers:{'Content-Type':'application/json'}, body:body})
      .then(r => r.json()).then(d => alert(JSON.stringify(d)));
  }
</script>
"""


def build_page(state: TrainState) -> str:
    """Render the train daemon dashboard as an HTML string."""
    d = state.to_dict()

    rows_html = ""
    for f in d["frames"]:
        iter_str = f"{f['iteration']}/{f['total_iterations']}" if f["total_iterations"] else "—"
        actions = ""
        if f["status"] == "training":
            actions += (f'<button class="danger" '
                        f'onclick="doAction(\'{f["key"]}\',\'stop\')">停止</button> ')
        if f["status"] in ("training", "done", "failed"):
            actions += (f'<button class="warn" '
                        f'onclick="doAction(\'{f["key"]}\',\'clean\',\'soft\')">清理 soft</button> ')
            actions += (f'<button class="danger" '
                        f'onclick="doAction(\'{f["key"]}\',\'clean\',\'hard\')">清理 hard</button>')

        rows_html += f"""
        <tr id="row-{f['key']}">
          <td>{f['frame_id']}</td>
          <td><span class="st-{f['status']}">{f['status']}</span></td>
          <td>{f['worker_id'] or '—'}</td>
          <td>{iter_str}</td>
          <td>{actions}</td>
        </tr>"""

    workers_html = ""
    for w in d["workers"]:
        online_icon = "\U0001f7e2" if w["is_online"] else "\U0001f534"
        workers_html += (f'<span id="worker-{w["id"]}">{online_icon} {w["id"]}'
                         f'</span> ')

    log_panels = ""
    for w in d["workers"]:
        log_panels += f"""
        <div class="log-panel">
          <div class="log-header" onclick="toggleLog('log-{w['id']}')">
            <span>▸ {w['id']} 日志</span>
          </div>
          <div class="log-body" id="log-{w['id']}" data-worker="{w['id']}"></div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <title>v8 Train — {d['project']}</title>
  {_PAGE_CSS}
</head>
<body>
  <h1>\U0001f682 v8 Train Daemon — project: {d['project']}</h1>
  <div class="info">
    Workers: {workers_html}
    | 轮询间隔: {d['poll_interval']}s
  </div>
  <table>
    <thead>
      <tr><th>帧号</th><th>状态</th><th>Worker</th><th>迭代</th><th>操作</th></tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
  <h2>Worker 日志</h2>
  {log_panels}
  {_JS_SSE}
</body>
</html>"""


# ── Action handler ───────────────────────────────────────────────────────────────

def handle_action(state: TrainState, body: dict) -> dict:
    """Process a user action (stop / clean).

    Returns a result dict suitable for JSON response.
    """
    key = body.get("key", "")
    action = body.get("action", "")
    level = body.get("level", "soft")

    frame = state.get_frame(key)
    if frame is None:
        return {"status": "error", "message": f"frame not found: {key}"}

    if action == "stop":
        worker = None
        for w in state.workers:
            if w.id == frame.worker_id:
                worker = w
                break
        if worker is None:
            return {"status": "error", "message": f"worker not found: {frame.worker_id}"}

        status_path = str(
            Path(worker.litegs_path) / "results" / frame.sub_dir / "_worker_status.json"
        )
        ok, msg = kill_worker_process(worker, status_path)
        state.update_frame(key, status="failed",
                           error_message=f"stopped by user: {msg}")
        return {"status": "ok" if ok else "error", "message": msg}

    elif action == "clean":
        proj_dir = ROOT / f"CameraData/{state.project}"
        worker = None
        for w in state.workers:
            if w.id == frame.worker_id:
                worker = w
                break
        if worker is None and state.workers:
            worker = state.workers[0]
        if worker is None:
            return {"status": "error", "message": "no worker available"}

        result = cleanup_frame(
            worker, proj_dir, frame.sub_dir, frame.frame_id,
            level=level, frame_dirname=frame.dirname,
        )
        return result

    return {"status": "error", "message": f"unknown action: {action}"}


# ── Main loop ────────────────────────────────────────────────────────────────────

def main_loop(state: TrainState, cfg: dict,
              broadcaster: SSEBroadcaster, logger: DaemonLogger,
              force: bool,
              frames_filter: list[str] | None,
              stop_event: threading.Event):
    """The infinite polling loop. Runs in a background thread."""

    proj_dir = (ROOT / f"CameraData/{cfg['project']}").resolve()
    raw_dir = Path(cfg.get("raw_images_path", proj_dir / "raw_images"))
    img_num = cfg.get("img_num")

    # Ring buffer for worker logs: worker_id → list of lines
    log_buffers: dict[str, list[str]] = defaultdict(list)
    MAX_LOG_LINES = 500

    # Cross-cycle snapshot cache for readiness detection.
    # Keyed by frame key; stores (file_count, set_of_names, {name:size}) from
    # the PREVIOUS cycle. When the current cycle's snapshot matches, the frame
    # is stable — no extra sleep needed.
    _prev_snapshot: dict[str, tuple] = {}

    def _snapshot_dir(d: Path) -> tuple[int, set[str], dict[str, int]]:
        """Return (count, set_of_names, {name: size})."""
        files = list(d.iterdir())
        return (
            len(files),
            {f.name for f in files},
            {f.name: f.stat().st_size for f in files},
        )

    def _emit_log(worker_id: str, line: str):
        log_buffers[worker_id].append(line)
        if len(log_buffers[worker_id]) > MAX_LOG_LINES:
            log_buffers[worker_id] = log_buffers[worker_id][-MAX_LOG_LINES:]
        broadcaster.broadcast("log", f"{worker_id} {line}")
        # Also write to disk — daemon vs worker separated
        if worker_id == "daemon":
            logger.daemon(line)
        else:
            logger.worker(worker_id, line)

    def _emit_status():
        broadcaster.broadcast("status",
                              json.dumps(state.to_dict(), ensure_ascii=False))

    print(f"  Train daemon main loop started. "
          f"raw_images={raw_dir}, poll={state.poll_interval}s")
    _cycle = 0

    while not stop_event.is_set():
        _cycle += 1
        try:
            # Refresh worker online status
            online_workers = [w for w in state.workers if w.is_online]
            if not online_workers:
                _emit_status()
                stop_event.wait(state.poll_interval)
                continue

            # ── 1. Scan raw_images/ ──
            if raw_dir.is_dir():
                scanned = 0
                for fd in sorted(raw_dir.iterdir()):
                    if not fd.is_dir():
                        continue
                    scanned += 1

                    try:
                        sub_dir, frame_id = parse_frame_dirname(fd.name)
                    except ValueError:
                        continue

                    key = f"{sub_dir}-{frame_id}"

                    # Skip if already tracking and training/done
                    existing = state.get_frame(key)
                    if existing and existing.status in ("training", "done"):
                        continue

                    # Check if PLY already exists
                    ply_path = proj_dir / f"{key}.ply"
                    if ply_path.exists() and not force:
                        state.add_frame(frame_id, sub_dir, fd.name)
                        state.update_frame(key, status="done")
                        continue

                    # ── Cross-cycle stability check ──
                    # Take a snapshot now, compare with previous cycle's.
                    # The poll interval (5s) provides the stability window.
                    cur = _snapshot_dir(fd)
                    prev = _prev_snapshot.get(key)

                    # Count validation
                    expected = None
                    if img_num is not None:
                        expected = img_num
                    else:
                        try:
                            expected = int(fd.name.split("-")[0])
                        except (ValueError, IndexError):
                            pass

                    count_ok = (expected is None or cur[0] == expected)
                    stable = (prev is not None
                              and cur[0] == prev[0]
                              and cur[1] == prev[1]
                              and cur[2] == prev[2])

                    # Store current snapshot for next cycle
                    _prev_snapshot[key] = cur

                    if existing is None:
                        # First discovery
                        state.add_frame(frame_id, sub_dir, fd.name)
                        state.update_frame(key, status="checking")
                        print(f"  [scan] NEW {key} ({fd.name}) "
                              f"— {cur[0]} files, expect {expected or '?'}")
                    elif existing.status == "checking":
                        if count_ok and stable:
                            state.update_frame(key, status="ready")
                            _emit_log("daemon", f"帧就绪: {key} ({fd.name})")
                            print(f"  [scan] READY {key} — {cur[0]} files stable")
                            _prev_snapshot.pop(key, None)  # cleanup
                        elif not count_ok:
                            print(f"  [scan] {key} — {cur[0]} files "
                                  f"(expect {expected}), copying in progress...")
                            _prev_snapshot.pop(key, None)  # reset on count change
                        else:
                            # count OK but snapshot changed → still copying files
                            print(f"  [scan] {key} — {cur[0]}/{expected} files, "
                                  f"not yet stable (waiting next cycle)")

                # Heartbeat: print scan summary every 6 cycles (~30s)
                if _cycle % 6 == 0 and scanned > 0:
                    ready = sum(1 for fs in state.frames.values()
                                if fs.status == "ready")
                    training = sum(1 for fs in state.frames.values()
                                   if fs.status == "training")
                    done = sum(1 for fs in state.frames.values()
                               if fs.status == "done")
                    print(f"  [scan #{_cycle}] {scanned} dirs | "
                          f"ready={ready} training={training} done={done}")

                # Cleanup snapshots for frames no longer in raw_images
                active_keys = set()
                for fd in raw_dir.iterdir():
                    if fd.is_dir():
                        try:
                            sd, fid = parse_frame_dirname(fd.name)
                            active_keys.add(f"{sd}-{fid}")
                        except ValueError:
                            pass
                for k in list(_prev_snapshot):
                    if k not in active_keys:
                        del _prev_snapshot[k]

            # ── 2. Dispatch ready frames (round-robin to least-loaded worker) ──
            ready_frames = [(k, fs) for k, fs in state.frames.items()
                            if fs.status == "ready"]
            if ready_frames:
                worker_loads = {w.id: 0 for w in online_workers}
                for fs in state.frames.values():
                    if fs.status == "training" and fs.worker_id:
                        worker_loads[fs.worker_id] = \
                            worker_loads.get(fs.worker_id, 0) + 1

                for key, fs in ready_frames:
                    best_worker = min(online_workers,
                                      key=lambda w: worker_loads.get(w.id, 0))
                    worker_loads[best_worker.id] += 1

                    state.update_frame(key, status="copying",
                                       worker_id=best_worker.id)

                    # Copy frame data to worker
                    src = raw_dir / fs.dirname
                    worker_data = Path(best_worker.litegs_path) / "data" / fs.sub_dir

                    if best_worker.is_host:
                        dst = worker_data / fs.dirname
                        try:
                            if not dst.exists():
                                shutil.copytree(src, dst, dirs_exist_ok=True)
                            _emit_log("daemon", f"分发 {key} → {best_worker.id} (local)")
                        except Exception as e:
                            state.update_frame(key, status="failed",
                                               error_message=f"copy failed: {e}")
                            continue
                    else:
                        worker_data_str = str(worker_data).replace("\\", "/")
                        ok = scp_send_multi(best_worker, [str(src)], worker_data_str)
                        if ok:
                            _emit_log("daemon",
                                      f"分发 {key} → {best_worker.id} (SCP)")
                        else:
                            state.update_frame(key, status="failed",
                                               error_message="SCP failed")
                            continue

                    # Start training
                    state.update_frame(key, status="training")
                    print(f"  [dispatch] {key} → {best_worker.id}")
                    litegs_path = Path(best_worker.litegs_path)
                    py = str(litegs_path / ".venv" / "Scripts" / "python.exe")
                    status_rel = f"results/{fs.sub_dir}/_worker_status.json"
                    training_cfg = cfg.get("distributed", {}).get("training", {})
                    extra_parts = []
                    if training_cfg.get("iterations"):
                        extra_parts.extend(
                            ["--iterations", str(training_cfg["iterations"])])
                    extra_str = " ".join(extra_parts)

                    cmd = (
                        f'cd /d "{best_worker.litegs_path}" && '
                        f'"{py}" batch_run.py '
                        f'--sub_dir {fs.sub_dir} '
                        f'--frames {fs.dirname} '
                        f'--worker-status {status_rel}'
                    )
                    # On retry, force batch_run.py to ignore stale results
                    if fs.retry_count > 0:
                        cmd += " --force"
                    if extra_str:
                        cmd += f" {extra_str}"

                    try:
                        proc = ssh_run_async(best_worker, cmd)
                        state.running_processes[key] = (best_worker, proc)
                        _emit_log("daemon",
                                  f"启动训练 {key} → {best_worker.id}")
                    except Exception as e:
                        state.update_frame(key, status="failed",
                                           error_message=f"ssh_run_async: {e}")
                        _emit_log("daemon", f"启动失败 {key}: {e}")

            # ── 3. Monitor running processes ──
            done_keys = []
            for key, (worker, proc) in list(state.running_processes.items()):
                rc = proc.poll()
                if rc is None:
                    # Still running — read status file
                    fs = state.get_frame(key)
                    if fs:
                        status_path = str(
                            Path(worker.litegs_path) / "results" /
                            fs.sub_dir / "_worker_status.json"
                        )
                        status_data = read_worker_status(worker, status_path)
                        if status_data:
                            state.update_frame(
                                key,
                                iteration=status_data.get("iteration", 0),
                                total_iterations=status_data.get(
                                    "total_iterations", 0),
                            )
                    # Stream stdout lines
                    try:
                        for line in proc.stdout:
                            line = line.rstrip("\n\r")
                            if line:
                                _emit_log(worker.id, line)
                    except Exception:
                        pass
                else:
                    # Process exited — drain remaining stdout first
                    done_keys.append(key)
                    try:
                        remaining = proc.stdout.read()
                        if remaining:
                            for line in remaining.splitlines():
                                line = line.strip()
                                if line:
                                    _emit_log(worker.id, line)
                    except Exception:
                        pass

                    fs = state.get_frame(key)
                    if fs is None:
                        continue

                    if rc == 0:
                        # Collect PLY
                        ply_name = f"{key}.ply"
                        worker_results = (Path(worker.litegs_path) /
                                          "results" / fs.sub_dir)
                        remote_ply = worker_results / ply_name
                        local_ply = proj_dir / ply_name

                        if worker.is_host:
                            if remote_ply.exists():
                                shutil.copy2(str(remote_ply), str(local_ply))
                                size_mb = local_ply.stat().st_size / 1024 ** 2
                                _emit_log("daemon",
                                          f"回收 {key} ({size_mb:.1f} MB)")
                        else:
                            from tills._distributed import scp_recv_multi
                            remote_str = str(remote_ply).replace("\\", "/")
                            ok = scp_recv_multi(worker, [remote_str], proj_dir)
                            if ok and local_ply.exists():
                                size_mb = local_ply.stat().st_size / 1024 ** 2
                                _emit_log("daemon",
                                          f"回收 {key} ({size_mb:.1f} MB)")
                            else:
                                _emit_log("daemon", f"回收失败 {key}")

                        state.update_frame(key, status="done")
                    else:
                        # Retry once
                        if fs.retry_count < 1:
                            state.update_frame(
                                key, status="ready",
                                retry_count=fs.retry_count + 1,
                                worker_id="",
                                error_message=f"exit {rc}, retrying")
                            _emit_log("daemon",
                                      f"训练失败 {key} (exit {rc}), 重试中...")
                        else:
                            state.update_frame(
                                key, status="failed",
                                error_message=f"exit {rc} after retries")
                            _emit_log("daemon",
                                      f"训练最终失败 {key} (exit {rc})")

            for key in done_keys:
                state.running_processes.pop(key, None)

            # ── 4. Push status update ──
            _emit_status()

        except Exception as e:
            _emit_log("daemon", f"ERROR in main loop: {e}")
            import traceback as _tb
            _tb.print_exc()

        stop_event.wait(state.poll_interval)

    # Cleanup on stop
    for key, (worker, proc) in state.running_processes.items():
        try:
            proc.terminate()
        except Exception:
            pass
    print("  Train daemon main loop stopped.")


# ── HTTP Handler ─────────────────────────────────────────────────────────────────

def _make_routes(state: TrainState):
    """Create route handlers bound to the given TrainState instance."""

    def _root(handler):
        return build_page(state), "text/html; charset=utf-8"

    def _action(handler, body):
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                return json.dumps({"status": "error",
                                   "message": "invalid JSON"}), \
                       "application/json; charset=utf-8"
        result = handle_action(state, body)
        return json.dumps(result, ensure_ascii=False), \
               "application/json; charset=utf-8"

    def _api_status(handler):
        return json.dumps(state.to_dict(), ensure_ascii=False), \
               "application/json; charset=utf-8"

    return {"/": _root, "/action": _action, "/api/status": _api_status}


# ── main ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="v8 Train Daemon")
    parser.add_argument("--config", required=True,
                        help="Path to pipeline.json")
    parser.add_argument("--port", type=int, default=8080,
                        help="HTTP server port (default: 8080)")
    parser.add_argument("--force", action="store_true",
                        help="Re-train even if PLY exists")
    parser.add_argument("--frames", nargs="*", default=None,
                        help="Only monitor these frames")
    args_p = parser.parse_args()

    # Load config
    config_path = Path(args_p.config)
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    if not config_path.exists():
        print(f"ERROR: config file not found: {config_path}")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_config_path"] = str(config_path)

    if "project" not in cfg:
        print("ERROR: Missing 'project' in config"); sys.exit(1)

    # Load workers
    proj_dir = (ROOT / f"CameraData/{cfg['project']}").resolve()
    dist_cfg = cfg.get("distributed", {})
    workers_config = dist_cfg.get("workers_config", "workers.json")
    workers_path = proj_dir / workers_config
    if not workers_path.exists():
        print(f"ERROR: workers config not found: {workers_path}")
        sys.exit(1)

    workers = load_workers(workers_path)
    auto_detect_host(workers)

    print("v8 Train Daemon")
    print(f"  Project: {cfg['project']}")
    print(f"  Workers: {len(workers)}")
    for w in workers:
        tag = " [HOST]" if w.is_host else ""
        online = "🟢" if w.is_online else "🔴"
        print(f"    {online} {w.id}: {w.ip}{tag}")

    # Validate connectivity
    print(f"\n  验证 Worker 连通性 ...")
    results = validate_workers(workers)
    all_ok = True
    for wid, (ok, msg) in results.items():
        status = "OK" if ok else "FAIL"
        print(f"    {wid}: {status} — {msg}")
        if not ok:
            all_ok = False

    if not all_ok:
        print("\nWARNING: 部分 Worker 不可达。Daemon 将继续启动，"
              "离线 Worker 会被跳过。")

    # Init state
    poll_interval = cfg.get("poll_interval", 5)
    state = TrainState(project=cfg["project"], poll_interval=poll_interval)
    state.workers = workers

    # Init broadcaster
    broadcaster = SSEBroadcaster()

    # Build handler class dynamically
    TrainHandler = type("TrainHandler", (SSEHandler,), {
        "routes": _make_routes(state),
        "sse_paths": {"/events"},
    })

    # Set up logger
    logger = DaemonLogger(proj_dir)
    logger.daemon(f"daemon started — project={cfg['project']} "
                  f"raw_images={cfg.get('raw_images_path', proj_dir / 'raw_images')}")

    # Start main loop in background thread
    stop_event = threading.Event()
    loop_thread = threading.Thread(
        target=main_loop,
        args=(state, cfg, broadcaster, logger,
              args_p.force, args_p.frames, stop_event),
        daemon=True,
    )
    loop_thread.start()

    # Start HTTP server (blocking)
    server = create_server("0.0.0.0", args_p.port, TrainHandler, broadcaster)
    try:
        run_server(server)
    except KeyboardInterrupt:
        print("\n  用户中断，正在停止...")
    finally:
        stop_event.set()
        loop_thread.join(timeout=10)
        logger.daemon("daemon stopped")
        logger.close()
        print("  Train daemon stopped.")


if __name__ == "__main__":
    main()
