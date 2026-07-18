#!/usr/bin/env python3
"""
Train Daemon — continuous polling, frame dispatch, training monitor.

Usage:
  # 项目初始化（首次使用）
  uv run python -m tills.server.train_daemon init 06

  # 启动守护进程（--config 支持简写项目名或完整路径）
  uv run python -m tills.server.train_daemon --config 06
  uv run python -m tills.server.train_daemon --config 06 --port 8080

Start this and leave it running.  Open http://localhost:8080 to monitor.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
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
    ssh_run, ssh_run_async, kill_worker_process, cleanup_frame,
    read_worker_status, scp_send_multi, scp_recv_multi, ROOT,
    resolve_worker_python,
)
from tills._shared import load_preset, parse_frame_dirname
from tills.server._server import (
    SSEBroadcaster, SSEHandler, create_server, run_server, FileLogger,
)


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
        self.raw_images_dir: Path | None = None
        self.training_enabled: bool = False  # toggled by web UI start/stop button
        self.cali_running: bool = False       # true while generate_cali background thread runs
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
                "training_enabled": self.training_enabled,
                "cali_running": self.cali_running,
                "raw_images_dir": str(self.raw_images_dir) if self.raw_images_dir else "",
                "frames": frame_list,
                "workers": worker_list,
            }


# ── HTML page builder ────────────────────────────────────────────────────────────

_PAGE_CSS = """
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;
       background:#f5f0e8;color:#3e3a35;
       padding:20px}
  h1{color:#5b7c5a;margin-bottom:10px;font-size:22px}
  h2{color:#5b7c5a;margin:15px 0 10px;font-size:17px}
  .info{color:#7a7368;margin-bottom:20px;font-size:14px}
  table{width:100%;border-collapse:collapse;margin-bottom:20px;
        background:#fffdf7;border-radius:6px;overflow:hidden;
        box-shadow:0 1px 3px rgba(0,0,0,.06)}
  th{text-align:left;padding:8px 10px;background:#e8e0d3;color:#5b5a4e;
     font-size:13px;font-weight:600}
  td{padding:8px 10px;border-bottom:1px solid #e8e0d3;font-size:13px}
  tr:hover{background:#faf3e3}
  .st-new{color:#aaa295}
  .st-checking{color:#b8860b}
  .st-ready{color:#2e7d32}
  .st-copying{color:#1565c0}
  .st-training{color:#0277bd;font-weight:bold}
  .st-done{color:#2e7d32}
  .st-failed{color:#c62828}
  button{background:#6b8e6b;color:#fff;border:none;padding:4px 10px;
         cursor:pointer;border-radius:3px;font-size:12px;margin:1px}
  button.danger{background:#c0392b}
  button.warn{background:#d4850a}
  button:hover{opacity:0.85}
  button:disabled{opacity:0.4;cursor:default}
  .log-panel{background:#fdfaf2;border:1px solid #d9cfb8;border-radius:4px;
             margin-bottom:10px}
  .log-header{padding:6px 12px;background:#ede4d3;cursor:pointer;
              display:flex;justify-content:space-between;font-size:13px;
              color:#5b5a4e}
  .log-body{padding:8px 12px;max-height:300px;overflow-y:auto;
            font-size:12px;line-height:1.6;display:none;white-space:pre-wrap;
            font-family:Consolas,"Fira Code",monospace}
  .log-body.open{display:block}
  .log-body::-webkit-scrollbar{width:6px}
  .log-body::-webkit-scrollbar-thumb{background:#c9bfa8;border-radius:3px}
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
      let oldStatus = row.cells[2].textContent.trim();
      row.cells[2].innerHTML = '<span class="st-' + f.status + '">'
                               + f.status + '</span>';
      row.cells[3].textContent = f.worker_id || '—';
      const iter = f.total_iterations ? f.iteration + '/' + f.total_iterations : '—';
      row.cells[4].textContent = iter;
      // after clean, frame resets to new → action buttons need refresh
      if ((f.status === 'new' || f.status === 'checking' || f.status === 'ready')
          && oldStatus !== f.status) {
        location.reload();
      }
    }
    for (const w of data.workers) {
      let el = document.getElementById('worker-' + w.id);
      if (el) {
        el.textContent = (w.is_online ? '🟢' : '🔴')
                       + ' ' + w.id
                       + (w.current_frame ? ' (' + w.current_frame + ')' : '');
      }
    }
    if (data.hasOwnProperty('training_enabled')) {
      updateScanUI(data.training_enabled);
    }
    if (data.hasOwnProperty('cali_running')) {
      document.getElementById('cali-status').style.display = data.cali_running ? '' : 'none';
      updateCaliButton();
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
      .then(r => r.json()).then(d => {
        if (d.status !== 'ok') {
          alert(action + ' FAILED: ' + (d.message || JSON.stringify(d)));
        }
      });
  }
  function doScanToggle(cmd) {
    fetch('/action', {method:'POST',
     headers:{'Content-Type':'application/json'},
     body: JSON.stringify({action: cmd})
    }).then(r => r.json()).then(d => {
      if (d.status === 'ok') {
        updateScanUI(d.training_enabled);
      }
    });
  }
  function updateScanUI(scanning) {
    document.getElementById('btn-scan-start').style.display = scanning ? 'none' : '';
    document.getElementById('btn-scan-stop').style.display = scanning ? '' : 'none';
    let st = document.getElementById('scan-status');
    st.textContent = scanning ? '● 训练中...' : '● 训练已暂停';
    st.style.color = scanning ? '#2e7d32' : '#c0392b';
    updateCaliButton();
  }
  function selectCaliRow(tr, evt) {
    if (evt.target.tagName === 'BUTTON') return;
    let radio = tr.querySelector('input[name="cali-frame"]');
    if (radio) { radio.checked = true; updateCaliButton(); }
  }
  function updateCaliButton() {
    let scanningStopped = document.getElementById('btn-scan-start').style.display !== 'none';
    let selected = document.querySelector('input[name="cali-frame"]:checked');
    let caliRunning = document.getElementById('cali-status').style.display !== 'none';
    let btnGen = document.getElementById('btn-cali-gen');
    let btnDist = document.getElementById('btn-cali-dist');
    let startBtn = document.getElementById('btn-scan-start');
    if (caliRunning) {
      btnGen.disabled = true;
      btnDist.disabled = true;
      startBtn.disabled = true;
    } else {
      btnGen.disabled = !(scanningStopped && selected);
      btnDist.disabled = !(scanningStopped && selected);
      startBtn.disabled = false;
    }
  }
  function doGenerateCali() {
    let selected = document.querySelector('input[name="cali-frame"]:checked');
    if (!selected) { alert('请选择一个帧'); return; }
    let key = selected.value;
    let dirname = selected.dataset.dirname;
    document.getElementById('cali-status').style.display = '';
    updateCaliButton();
    fetch('/action', {method:'POST',
     headers:{'Content-Type':'application/json'},
     body: JSON.stringify({action: 'generate_cali', key: key, dirname: dirname})
    }).then(r => r.json()).then(d => {
      if (d.status !== 'ok') {
        document.getElementById('cali-status').style.display = 'none';
        updateCaliButton();
        alert('ERROR: ' + (d.message || JSON.stringify(d)));
      }
    });
  }
  function doDistributeCali() {
    let selected = document.querySelector('input[name="cali-frame"]:checked');
    if (!selected) { alert('请选择一个帧'); return; }
    let key = selected.value;
    let dirname = selected.dataset.dirname;
    document.getElementById('cali-status').style.display = '';
    updateCaliButton();
    fetch('/action', {method:'POST',
     headers:{'Content-Type':'application/json'},
     body: JSON.stringify({action: 'distribute_cali', key: key, dirname: dirname})
    }).then(r => r.json()).then(d => {
      if (d.status !== 'ok') {
        document.getElementById('cali-status').style.display = 'none';
        updateCaliButton();
        alert('ERROR: ' + (d.message || JSON.stringify(d)));
      }
    });
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
        <tr id="row-{f['key']}" onclick="selectCaliRow(this, event)">
          <td><input type="radio" name="cali-frame" value="{f['key']}"
                     data-dirname="{f['dirname']}"
                     onchange="updateCaliButton()"></td>
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
  <div id="scan-control" style="margin:10px 0;display:flex;align-items:center;gap:10px">
    <button id="btn-scan-start" onclick="doScanToggle('training_start')"
            style="background:#2e7d32;padding:8px 18px;font-size:14px;{('display:none' if d['training_enabled'] else '')}">▶ 开始训练</button>
    <button id="btn-scan-stop" onclick="doScanToggle('training_stop')"
            style="background:#c0392b;padding:8px 18px;font-size:14px;{('' if d['training_enabled'] else 'display:none')}">⏹ 停止训练</button>
    <span id="scan-status" style="font-size:14px;color:{'#2e7d32' if d['training_enabled'] else '#c0392b'}">{'● 训练中...' if d['training_enabled'] else '● 训练已暂停'}</span>
    <span style="flex:1"></span>
    <span id="cali-status" style="font-size:13px;color:#1565c0;{('' if d['cali_running'] else 'display:none')}">🔄 标定中...</span>
    <button id="btn-cali-gen" onclick="doGenerateCali()"
            style="background:#1565c0;padding:8px 18px;font-size:14px" disabled>📷 生成位姿</button>
    <button id="btn-cali-dist" onclick="doDistributeCali()"
            style="background:#6b8e6b;padding:8px 18px;font-size:14px" disabled>📡 分发位姿</button>
  </div>
  <div class="info">
    Workers: {workers_html}
    | 轮询间隔: {d['poll_interval']}s
    | 监控目录: {d['raw_images_dir']}
  </div>
  <table>
    <thead>
      <tr><th>位姿</th><th>帧号</th><th>状态</th><th>Worker</th><th>迭代</th><th>操作</th></tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
  <h2>Worker 日志</h2>
  {log_panels}
  {_JS_SSE}
</body>
</html>"""


# ── Action handler ───────────────────────────────────────────────────────────────

def handle_action(state: TrainState, body: dict,
                  cfg: dict | None = None,
                  broadcaster=None) -> dict:
    """Process a user action (stop / clean / generate_cali).

    Returns a result dict suitable for JSON response.
    """
    action = body.get("action", "")

    # ── global scan toggle (no frame key needed) ──
    if action == "training_start":
        state.training_enabled = True
        return {"status": "ok", "training_enabled": True,
                "message": "训练分发已开启"}
    if action == "training_stop":
        state.training_enabled = False
        return {"status": "ok", "training_enabled": False,
                "message": "训练分发已停止"}

    # ── generate calibration (spawns background thread) ──
    if action == "generate_cali":
        key = body.get("key", "")
        dirname = body.get("dirname", "")
        if not key or not dirname:
            return {"status": "error", "message": "缺少帧信息"}
        try:
            from tills._shared import parse_frame_dirname
            sub_dir, frame_id = parse_frame_dirname(dirname)
        except ValueError as e:
            return {"status": "error", "message": f"无法解析帧目录名: {e}"}
        state.cali_running = True
        t = threading.Thread(
            target=run_generate_cali,
            args=(state, key, sub_dir, dirname, cfg, broadcaster),
            daemon=True,
        )
        t.start()
        return {"status": "ok",
                "message": f"标定任务已启动: {key} → calibration/{sub_dir}"}

    # ── distribute calibration ──
    if action == "distribute_cali":
        key = body.get("key", "")
        dirname = body.get("dirname", "")
        if not key or not dirname:
            return {"status": "error", "message": "缺少帧信息"}
        try:
            from tills._shared import parse_frame_dirname
            sub_dir, frame_id = parse_frame_dirname(dirname)
        except ValueError as e:
            return {"status": "error", "message": f"无法解析帧目录名: {e}"}
        # Quick pre-check: host cali must exist
        host_cali = Path(cfg.get("litegs_path", "")) / "data" / "calibration" / sub_dir
        if not (host_cali / "sparse" / "cameras.txt").exists():
            return {"status": "error",
                    "message": f"主机标定数据不完整，请先生成位姿"}
        state.cali_running = True
        t = threading.Thread(
            target=run_distribute_cali,
            args=(state, key, sub_dir, dirname, cfg, broadcaster),
            daemon=True,
        )
        t.start()
        return {"status": "ok",
                "message": f"分发任务已启动: {key} → calibration/{sub_dir}"}

    # ── per-frame actions (require key) ──
    key = body.get("key", "")
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

        # 1. Kill the remote/local training process via PID
        status_path = str(
            Path(worker.litegs_path) / "results" / frame.sub_dir / "_worker_status.json"
        )
        ok, msg = kill_worker_process(worker, status_path)

        # 2. Terminate the local Popen wrapper (SSH process or cmd.exe shell).
        #    This also causes the stdout reader thread to hit EOF and exit.
        entry = state.running_processes.pop(key, None)
        if entry is not None:
            _worker, _proc = entry
            try:
                _proc.terminate()
            except Exception:
                pass

        state.update_frame(key, status="failed",
                           error_message=f"stopped by user: {msg}")
        if broadcaster:
            broadcaster.broadcast("status",
                                  json.dumps(state.to_dict(), ensure_ascii=False))
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
            raw_images_dir=state.raw_images_dir,
        )
        # Immediately reset state so the next scan re-detects this frame
        if result.get("status") == "ok":
            state.update_frame(key, status="new",
                               worker_id="",
                               iteration=0, total_iterations=0,
                               error_message="", retry_count=0)
        if broadcaster:
            broadcaster.broadcast("status",
                                  json.dumps(state.to_dict(), ensure_ascii=False))
        return result

    return {"status": "error", "message": f"unknown action: {action}"}


# ── Calibration helpers ─────────────────────────────────────────────────────────────

def _backup_cali_dir(cali_dir: Path, backup_root: Path, sub_dir: str) -> Path | None:
    """Move *cali_dir* to *backup_root*/<sub_dir>-N (incrementing suffix).

    Returns the backup path, or None if *cali_dir* does not exist.
    """
    if not cali_dir.exists():
        return None
    idx = 1
    while True:
        backup = backup_root / f"{sub_dir}-{idx}"
        if not backup.exists():
            break
        idx += 1
    backup_root.mkdir(parents=True, exist_ok=True)
    shutil.move(str(cali_dir), str(backup))
    return backup


# ── Calibration background tasks ────────────────────────────────────────────────────

def run_generate_cali(state: TrainState, key: str, sub_dir: str,
                      dirname: str, cfg: dict, broadcaster):
    """Background thread: backup old cali → copy images → run COLMAP."""
    litegs_path = Path(cfg.get("litegs_path", ""))
    cali_root = litegs_path / "data" / "calibration"
    old_cali_root = litegs_path / "data" / "old-cali"
    raw_dir = Path(cfg.get("raw_images_path", ""))
    frame_dir = raw_dir / dirname

    def _log(msg: str):
        print(f"  [cali:{key}] {msg}")
        if broadcaster:
            broadcaster.broadcast("log", f"daemon [cali:{key}] {msg}")

    _log(f"开始标定位姿: sub_dir={sub_dir}, dirname={dirname}")

    try:
        if not frame_dir.is_dir():
            _log(f"ERROR: 帧目录不存在: {frame_dir}")
            return

        cali_dir = cali_root / sub_dir

        # Backup existing calibration
        backup = _backup_cali_dir(cali_dir, old_cali_root, sub_dir)
        if backup:
            _log(f"已有标定数据已备份至: {backup}")

        # Copy frame images to calibration directory
        cali_dir.mkdir(parents=True, exist_ok=True)
        IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
        image_count = 0
        for img in sorted(frame_dir.iterdir()):
            if img.is_file() and img.suffix.lower() in IMG_EXTS:
                shutil.copy2(str(img), str(cali_dir / img.name))
                image_count += 1
        _log(f"已拷贝 {image_count} 张图片到 {cali_dir}")

        if image_count == 0:
            _log("ERROR: 帧目录中没有图片文件")
            return

        # Run prepare_calibration.py
        script = litegs_path / "utils" / "prepare_calibration.py"
        cmd = [
            "uv", "run", "python", str(script),
            "--sub_dir", sub_dir,
        ]
        _log(f"执行: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd, cwd=str(litegs_path),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        for line in proc.stdout:
            line = line.rstrip("\n\r")
            if line:
                _log(line)
        rc = proc.wait(timeout=7200)
        if rc != 0:
            _log(f"COLMAP FAILED (exit {rc})")
            return
        _log("标定位姿生成完成 ✓")

    except subprocess.TimeoutExpired:
        _log("TIMEOUT: COLMAP 超时 (2h)")
    except Exception as e:
        _log(f"ERROR: {e}")
    finally:
        state.cali_running = False
        if broadcaster:
            broadcaster.broadcast("status",
                                  json.dumps(state.to_dict(), ensure_ascii=False))


def run_distribute_cali(state: TrainState, key: str, sub_dir: str,
                        dirname: str, cfg: dict, broadcaster):
    """Background thread: verify host cali → backup + SCP to each remote worker."""
    litegs_path = Path(cfg.get("litegs_path", ""))
    cali_root = litegs_path / "data" / "calibration"
    host_cali = cali_root / sub_dir

    def _log(msg: str):
        print(f"  [dist:{key}] {msg}")
        if broadcaster:
            broadcaster.broadcast("log", f"daemon [dist:{key}] {msg}")

    _log(f"开始分发音位: sub_dir={sub_dir}")

    try:
        # 1. Verify host calibration is complete
        sparse_txt = host_cali / "sparse" / "cameras.txt"
        sparse_bin_dir = host_cali / "sparse_bin"
        if not sparse_txt.exists():
            _log("ERROR: 主机标定数据不完整 (sparse/cameras.txt 不存在)，请先生成位姿")
            return
        if not sparse_bin_dir.is_dir() or not any(sparse_bin_dir.iterdir()):
            _log("ERROR: 主机标定数据不完整 (sparse_bin/ 为空)，请先生成位姿")
            return
        _log("主机标定数据完整 ✓")

        # 2. Distribute to each remote worker
        remote_workers = [w for w in state.workers if not w.is_host and w.is_online]
        if not remote_workers:
            _log("没有在线副机需要分发")
            return

        from tills._distributed import ssh_run, scp_send_multi

        for w in remote_workers:
            _log(f"--- {w.id} ({w.ip}) ---")
            worker_litegs = Path(w.litegs_path)
            worker_cali = worker_litegs / "data" / "calibration" / sub_dir
            worker_old = worker_litegs / "data" / "old-cali"

            # 2a. Check + backup worker's existing calibration via SSH
            check_cmd = f'if exist "{worker_cali}" (echo EXISTS) else (echo NOT_FOUND)'
            result = ssh_run(w, check_cmd, timeout=30)
            if "EXISTS" in (result.stdout or ""):
                # Build backup move command
                move_cmd = _build_remote_backup_cmd(worker_cali, worker_old, sub_dir)
                _log(f"备份副机已有标定: {move_cmd}")
                move_result = ssh_run(w, move_cmd, timeout=60)
                if move_result.returncode != 0:
                    _log(f"WARNING: 备份失败 (exit {move_result.returncode})")
                    if move_result.stderr:
                        _log(f"  [stderr] {move_result.stderr.strip()}")
                else:
                    _log(f"已备份副机标定")

            # 2b. Ensure target parent exists on worker
            parent = str(worker_cali.parent).replace("\\", "/")
            mkdir_cmd = f'if not exist "{worker_cali.parent}" mkdir "{worker_cali.parent}"'
            ssh_run(w, mkdir_cmd, timeout=30)

            # 2c. SCP host calibration directory to worker
            host_cali_str = str(host_cali)
            _log(f"SCP {host_cali_str} → {w.id}:{parent}/")
            ok = scp_send_multi(w, [host_cali_str], parent)
            if ok:
                _log(f"{w.id} 分发完成 ✓")
            else:
                _log(f"ERROR: {w.id} SCP 失败")

        _log("分发音位完成")

    except Exception as e:
        _log(f"ERROR: {e}")
    finally:
        state.cali_running = False
        if broadcaster:
            broadcaster.broadcast("status",
                                  json.dumps(state.to_dict(), ensure_ascii=False))


def _build_remote_backup_cmd(cali_dir: Path, backup_root: Path, sub_dir: str) -> str:
    """Build a cmd.exe command to atomically move a directory with incrementing suffix."""
    return (
        f'powershell -Command "'
        f'$src=\'{cali_dir}\'; $dstRoot=\'{backup_root}\'; $name=\'{sub_dir}\'; '
        f'if (-not (Test-Path $src)) {{ exit 0 }}; '
        f'New-Item -ItemType Directory -Force -Path $dstRoot | Out-Null; '
        f'$n=1; while (Test-Path \\"$dstRoot\\$name-$n\\") {{ $n++ }}; '
        f'Move-Item -Path $src -Destination \\"$dstRoot\\$name-$n\\"'
        f'"'
    )


# ── Main loop ────────────────────────────────────────────────────────────────────

def main_loop(state: TrainState, cfg: dict,
              broadcaster: SSEBroadcaster, logger: FileLogger,
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

    _IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

    def _snapshot_dir(d: Path) -> tuple[int, set[str], dict[str, int]]:
        """Return (image_count, set_of_image_names, {name: size}) for images only."""
        files = [f for f in d.iterdir()
                 if f.is_file() and f.suffix.lower() in _IMG_EXTS]
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
            logger.write("daemon", line)
        else:
            logger.write(worker_id, line)

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

            # ── 1. Scan raw_images/ (always runs — populates frame list for UI) ──
            if raw_dir.is_dir():
                scanned = 0
                for fd in sorted(raw_dir.iterdir()):
                    if not fd.is_dir():
                        continue

                    # 仅纳入第一个"-"之前为纯数字的目录
                    prefix = fd.name.split("-")[0]
                    if not prefix.isdigit():
                        continue

                    scanned += 1

                    try:
                        sub_dir, frame_id = parse_frame_dirname(fd.name)
                    except ValueError:
                        continue

                    key = f"{sub_dir}-{frame_id}"

                    # Skip if already actively training
                    existing = state.get_frame(key)
                    if existing and existing.status == "training":
                        continue

                    # For "done" frames: verify PLY still exists.
                    # User may have cleaned it up via Web UI → re-check needed.
                    # "failed" frames are NOT auto-rechecked — training
                    # failed without producing a PLY; user must explicitly
                    # "清理" to reset.
                    if existing and existing.status == "done":
                        ply_path = proj_dir / f"{key}.ply"
                        if ply_path.exists():
                            continue  # PLY still there, truly done
                        # PLY gone (cleanup) → reset to checking
                        state.update_frame(key, status="checking",
                                           worker_id="",
                                           iteration=0, total_iterations=0)
                        _prev_snapshot.pop(key, None)
                        print(f"  [scan] RE-CHECK {key} — PLY deleted, "
                              f"will re-detect")
                        _emit_log("daemon",
                                  f"重新检测 {key} (PLY 已被删除)")

                    # Check if PLY already exists (from previous runs)
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
                    elif existing.status in ("new", "checking"):
                        if count_ok and stable:
                            state.update_frame(key, status="ready")
                            _emit_log("daemon", f"帧就绪: {key} ({fd.name})")
                            print(f"  [scan] READY {key} — {cur[0]} files stable")
                            # snapshot preserved for dispatch filtering
                        elif not count_ok:
                            print(f"  [scan] {key} — {cur[0]} files "
                                  f"(expect {expected}), copying in progress...")
                            _prev_snapshot.pop(key, None)  # reset on count change
                        else:
                            # count OK but snapshot changed → still copying files
                            print(f"  [scan] {key} — {cur[0]}/{expected} files, "
                                  f"not yet stable (waiting next cycle)")

                # Heartbeat: print + log scan summary every cycle when frames exist
                if scanned > 0:
                    ready = sum(1 for fs in state.frames.values()
                                if fs.status == "ready")
                    training = sum(1 for fs in state.frames.values()
                                   if fs.status == "training")
                    done = sum(1 for fs in state.frames.values()
                               if fs.status == "done")
                    failed = sum(1 for fs in state.frames.values()
                                 if fs.status == "failed")
                    msg = (f"scan #{_cycle}: {scanned} dirs | "
                           f"ready={ready} training={training} "
                           f"done={done} failed={failed}")
                    print(f"  [{msg}]")
                    _emit_log("daemon", msg)

                # Cleanup frames whose raw_images directory was deleted
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
                # Remove from state any non-training frame whose directory
                # no longer exists (e.g. hard-deleted).  Training frames
                # keep running — collection will still work.
                with state._lock:
                    for k in list(state.frames.keys()):
                        if k not in active_keys:
                            fs = state.frames.get(k)
                            if fs and fs.status != "training":
                                del state.frames[k]
                                state.running_processes.pop(k, None)
                                print(f"  [scan] REMOVED {k} — "
                                      f"raw_images directory gone")

            # ── 2. Dispatch ready frames (only when training enabled) ──
            ready_frames = [(k, fs) for k, fs in state.frames.items()
                            if fs.status == "ready"]
            if state.training_enabled and ready_frames:
                training_cfg = cfg.get("distributed", {}).get("training", {})
                max_per_worker = training_cfg.get("max_per_worker", 1)
                worker_loads = {w.id: 0 for w in online_workers}
                for fs in state.frames.values():
                    if fs.status == "training" and fs.worker_id:
                        worker_loads[fs.worker_id] = \
                            worker_loads.get(fs.worker_id, 0) + 1

                for key, fs in ready_frames:
                    # skip workers already at capacity
                    available = [w for w in online_workers
                                 if worker_loads.get(w.id, 0) < max_per_worker]
                    if not available:
                        break  # all workers busy, try next cycle
                    best_worker = min(available,
                                      key=lambda w: worker_loads.get(w.id, 0))
                    worker_loads[best_worker.id] += 1

                    state.update_frame(key, status="copying",
                                       worker_id=best_worker.id)

                    # Copy frame data to worker (image files only, from snapshot)
                    src = raw_dir / fs.dirname
                    worker_data = Path(best_worker.litegs_path) / "data" / fs.sub_dir
                    snap = _prev_snapshot.get(key, (0, set(), {}))
                    image_names = sorted(snap[1]) if snap[1] else None

                    if best_worker.is_host:
                        dst = worker_data / fs.dirname
                        try:
                            if not dst.exists():
                                dst.mkdir(parents=True, exist_ok=True)
                                if image_names:
                                    for name in image_names:
                                        shutil.copy2(str(src / name), str(dst / name))
                                else:
                                    shutil.copytree(src, dst, dirs_exist_ok=True)
                            _emit_log("daemon", f"分发 {key} → {best_worker.id} (local)")
                        except Exception as e:
                            state.update_frame(key, status="failed",
                                               error_message=f"copy failed: {e}")
                            continue
                    else:
                        try:
                            worker_data_str = str(worker_data / fs.dirname).replace("\\", "/")
                            if image_names:
                                # Ensure target subdirectory exists on the remote worker
                                ssh_run(best_worker,
                                        f'mkdir -p "{worker_data / fs.dirname}"',
                                        timeout=30)
                                # send individual image files (skip non-image clutter)
                                src_paths = [str(src / name) for name in image_names]
                                ok = scp_send_multi(best_worker, src_paths, worker_data_str)
                            else:
                                ok = scp_send_multi(best_worker, [str(src)], worker_data_str)
                            if ok:
                                _emit_log("daemon",
                                          f"分发 {key} → {best_worker.id} (SCP)")
                            else:
                                state.update_frame(key, status="failed",
                                                   error_message="SCP failed")
                                continue
                        except Exception as e:
                            state.update_frame(key, status="ready",
                                               worker_id="",
                                               error_message=f"SCP timeout/net: {e}")
                            _emit_log("daemon",
                                      f"分发 {key} → {best_worker.id} 失败 ({e}), 将重试")
                            continue

                    # Snapshot no longer needed after dispatch
                    _prev_snapshot.pop(key, None)

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
                        f' --force'
                    )
                    if extra_str:
                        cmd += f" {extra_str}"

                    try:
                        proc = ssh_run_async(best_worker, cmd)
                        state.running_processes[key] = (best_worker, proc)
                        _emit_log("daemon",
                                  f"启动训练 {key} → {best_worker.id}")

                        # Spawn a daemon thread to stream stdout WITHOUT
                        # blocking the main loop.  Each process gets its
                        # own reader so all workers' output arrives in
                        # real time, regardless of which finishes first.
                        _wid = best_worker.id
                        def _reader(proc_obj, wid):
                            try:
                                for line in proc_obj.stdout:
                                    line = line.rstrip("\n\r")
                                    if line:
                                        _emit_log(wid, line)
                            except Exception:
                                pass
                        t = threading.Thread(target=_reader,
                                             args=(proc, _wid),
                                             daemon=True)
                        t.start()
                    except Exception as e:
                        state.update_frame(key, status="failed",
                                           error_message=f"ssh_run_async: {e}")
                        _emit_log("daemon", f"启动失败 {key}: {e}")

            # ── 3. Monitor running processes ──
            #    Stdout is read by per-process daemon threads — this loop
            #    only checks exit status and reads progress files.
            done_keys = []
            for key, (worker, proc) in list(state.running_processes.items()):
                rc = proc.poll()
                if rc is None:
                    # Still running — read status file for progress
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
                else:
                    # Process exited
                    done_keys.append(key)

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
                        collected = False

                        if worker.is_host:
                            if remote_ply.exists():
                                shutil.copy2(str(remote_ply), str(local_ply))
                                size_mb = local_ply.stat().st_size / 1024 ** 2
                                _emit_log("daemon",
                                          f"回收 {key} ({size_mb:.1f} MB)")
                                collected = True
                            else:
                                _emit_log("daemon",
                                          f"回收失败 {key}: PLY 不存在 "
                                          f"({remote_ply})")
                        else:
                            remote_str = str(remote_ply).replace("\\", "/")
                            ok = scp_recv_multi(worker, [remote_str], proj_dir)
                            if ok and local_ply.exists():
                                size_mb = local_ply.stat().st_size / 1024 ** 2
                                _emit_log("daemon",
                                          f"回收 {key} ({size_mb:.1f} MB)")
                                collected = True
                            else:
                                _emit_log("daemon",
                                          f"回收失败 {key}: SCP ok={ok} "
                                          f"local_exists={local_ply.exists()}")

                        if collected:
                            # Collect cameras.json (always overwrite)
                            local_cam = proj_dir / "cameras.json"
                            remote_cam = worker_results / "cameras.json"
                            if worker.is_host:
                                if remote_cam.exists():
                                    shutil.copy2(str(remote_cam), str(local_cam))
                                    _emit_log("daemon",
                                              f"cameras.json → {local_cam}")
                            else:
                                ok_cam = scp_recv_multi(
                                    worker,
                                    [str(remote_cam).replace("\\", "/")],
                                    proj_dir,
                                )
                                if ok_cam and local_cam.exists():
                                    _emit_log("daemon",
                                              f"cameras.json → {local_cam}")
                            state.update_frame(key, status="done")
                        else:
                            # PLY not collected — retry if possible
                            if fs.retry_count < 1:
                                state.update_frame(
                                    key, status="ready",
                                    retry_count=fs.retry_count + 1,
                                    worker_id="",
                                    error_message="PLY collection failed")
                                _emit_log("daemon",
                                          f"回收失败 {key}, 重试中...")
                            else:
                                state.update_frame(
                                    key, status="failed",
                                    error_message="PLY collection failed after retries")
                                _emit_log("daemon",
                                          f"回收最终失败 {key}")
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

def _make_routes(state: TrainState, cfg: dict, broadcaster):
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
        result = handle_action(state, body, cfg, broadcaster)
        return json.dumps(result, ensure_ascii=False), \
               "application/json; charset=utf-8"

    def _api_status(handler):
        return json.dumps(state.to_dict(), ensure_ascii=False), \
               "application/json; charset=utf-8"

    return {"/": _root, "/action": _action, "/api/status": _api_status}


# ── main ─────────────────────────────────────────────────────────────────────────

def main():
    # ── init subcommand (before argparse) ──
    if len(sys.argv) >= 2 and sys.argv[1] == "init":
        if len(sys.argv) < 3:
            print("ERROR: init 需要项目名参数")
            print("Usage: uv run python -m tills.server.train_daemon init <project>")
            print("Example: uv run python -m tills.server.train_daemon init 06")
            sys.exit(1)
        from tills.server._server import init_project
        init_project(sys.argv[2])
        return

    parser = argparse.ArgumentParser(description="v8 Train Daemon")
    parser.add_argument("--config", required=True,
                        help="Project name (e.g. 06) or path to pipeline.json")
    parser.add_argument("--port", type=int, default=8080,
                        help="HTTP server port (default: 8080)")
    parser.add_argument("--force", action="store_true",
                        help="Re-train even if PLY exists")
    parser.add_argument("--frames", nargs="*", default=None,
                        help="Only monitor these frames")
    args_p = parser.parse_args()

    # Load config
    from tills.server._server import resolve_config_path
    config_path = resolve_config_path(args_p.config)
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
    state.raw_images_dir = Path(cfg.get("raw_images_path",
                                        proj_dir / "raw_images"))

    # Init broadcaster
    broadcaster = SSEBroadcaster()

    # Build handler class dynamically
    TrainHandler = type("TrainHandler", (SSEHandler,), {
        "routes": _make_routes(state, cfg, broadcaster),
        "sse_paths": {"/events"},
    })

    # Set up logger
    logger = FileLogger(proj_dir, prefix="daemon")
    logger.write("daemon", f"daemon started — project={cfg['project']} "
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
        logger.write("daemon","daemon stopped")
        logger.close()
        print("  Train daemon stopped.")


if __name__ == "__main__":
    main()
