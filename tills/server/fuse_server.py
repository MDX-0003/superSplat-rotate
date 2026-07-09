#!/usr/bin/env python3
"""
Fuse Server — browser-based PLY selection → fuse+clip → render.

Usage:
  python -m tills.server.fuse_server --config CameraData/05/pipeline.json
  python -m tills.server.fuse_server --config CameraData/05/pipeline.json --port 8081

Open http://localhost:8081 to browse PLYs and trigger fuse/render.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

# ── path setup ──
_this_dir = Path(__file__).resolve().parent
_project_root = _this_dir.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from tills._shared import ROOT, load_preset
from tills.server._server import (
    SSEBroadcaster, SSEHandler, create_server, run_server,
)

TILLS_PLY_DIR = _project_root / "tills_ply"


# ── State ────────────────────────────────────────────────────────────────────────

class FuseState:
    """Minimal thread-safe state for the fuse server."""

    def __init__(self, project: str, preset_name: str, poll_interval: int = 5):
        self.project = project
        self.preset_name = preset_name
        self.poll_interval = poll_interval
        self.ply_files: list[dict] = []      # [{name, size_mb, mtime, path}]
        self.current_task: str | None = None  # "fuse" | "render" | None
        self.task_log: list[str] = []
        self._lock = threading.Lock()

    def scan_plys(self) -> bool:
        """Scan project dir for PLYs. Returns True if list changed."""
        proj_dir = ROOT / f"CameraData/{self.project}"
        plys = sorted(proj_dir.glob("*.ply"))
        result = []
        for p in plys:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
            size_mb = round(p.stat().st_size / 1024 ** 2, 1)
            result.append({
                "name": p.name,
                "size_mb": size_mb,
                "mtime": mtime,
                "path": str(p),
            })
        with self._lock:
            old_names = {f["name"] for f in self.ply_files}
            self.ply_files = result
        return set(f["name"] for f in result) != old_names

    def add_log(self, line: str):
        with self._lock:
            self.task_log.append(line)
            if len(self.task_log) > 500:
                self.task_log = self.task_log[-500:]

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "project": self.project,
                "current_task": self.current_task,
                "ply_count": len(self.ply_files),
                "ply_files": list(self.ply_files),
            }


# ── HTML page builder ────────────────────────────────────────────────────────────

_CSS = """
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:Consolas,monospace;background:#1a1a2e;color:#e0e0e0;padding:20px}
  h1{color:#7ec8e3;margin-bottom:10px}
  h2{color:#7ec8e3;margin:15px 0 10px}
  .info{color:#888;margin-bottom:20px;font-size:14px}
  table{width:100%;border-collapse:collapse;margin-bottom:10px}
  th{text-align:left;padding:8px 10px;background:#16213e;color:#7ec8e3;font-size:13px}
  td{padding:8px 10px;border-bottom:1px solid #16213e;font-size:13px}
  tr:hover{background:#16213e}
  button{background:#2196f3;color:#fff;border:none;padding:6px 14px;cursor:pointer;
         border-radius:3px;font-size:13px;margin:4px}
  button:disabled{opacity:0.4;cursor:default}
  button.fuse{background:#4caf50}
  button.render-btn{background:#ff9800}
  .log-panel{background:#0d1117;border:1px solid #30363d;border-radius:4px;
             margin-top:15px}
  .log-body{padding:10px 14px;max-height:400px;overflow-y:auto;font-size:12px;
            line-height:1.5;white-space:pre-wrap;font-family:Consolas,monospace}
  .log-body::-webkit-scrollbar{width:6px}
  .log-body::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
</style>
"""


def build_fuse_page(state: FuseState) -> str:
    with state._lock:
        plys = list(state.ply_files)
        current = state.current_task
        log_lines = list(state.task_log[-50:])

    rows = ""
    for i, p in enumerate(plys):
        rows += f"""
        <tr>
          <td><input type="checkbox" name="ply" value="{i}"></td>
          <td>{p['name']}</td>
          <td>{p['size_mb']} MB</td>
          <td>{p['mtime']}</td>
        </tr>"""

    disabled = 'disabled' if current else ''
    task_status = f"任务进行中: {current}" if current else "空闲"

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="30">
  <title>v8 Fuse — {state.project}</title>
  {_CSS}
</head>
<body>
  <h1>🧩 v8 Fuse Server — project: {state.project}</h1>
  <div class="info">
    可用 PLY: {len(plys)} 个 | 状态: {task_status}
  </div>
  <table>
    <thead>
      <tr><th>选择</th><th>文件名</th><th>大小</th><th>时间</th></tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#888;font-size:13px;">默认全不勾选，手动选择最新 2-3 个。</p>
  <div>
    <button class="fuse" {disabled} onclick="doFuse()">fuse + clip 选中</button>
    <button class="render-btn" {disabled} onclick="doRender()">render 选中</button>
  </div>
  <div class="log-panel">
    <div class="log-body" id="task-log">{chr(10).join(log_lines)}</div>
  </div>
  <script>
    const evtSource = new EventSource('/events');
    evtSource.addEventListener('log', function(e) {{
      let el = document.getElementById('task-log');
      el.textContent += e.data + '\\n';
      el.scrollTop = el.scrollHeight;
    }});
    evtSource.addEventListener('status', function(e) {{
      location.reload();
    }});
    function getChecked() {{
      let boxes = document.querySelectorAll('input[name="ply"]:checked');
      return Array.from(boxes).map(cb => parseInt(cb.value) + 1);
    }}
    async function doFuse() {{
      let indices = getChecked();
      if (!indices.length) {{ alert('请至少勾选一个 PLY'); return; }}
      let r = await fetch('/fuse', {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{indices: indices}})
      }});
      let d = await r.json();
      if (d.status === 'ok') location.reload();
      else alert(JSON.stringify(d));
    }}
    async function doRender() {{
      let indices = getChecked();
      if (!indices.length) {{ alert('请勾选一个 PLY 用于渲染'); return; }}
      let r = await fetch('/render', {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{indices: indices}})
      }});
      let d = await r.json();
      if (d.status === 'ok') location.reload();
      else alert(JSON.stringify(d));
    }}
  </script>
</body>
</html>"""


# ── Actions ──────────────────────────────────────────────────────────────────────

def run_fuse_clip(state: FuseState, cfg: dict, preset: dict,
                  indices: list[int], force: bool,
                  broadcaster: SSEBroadcaster):
    """Execute fuse_ply.py → clip_ply.py in a background thread."""
    proj_dir = ROOT / f"CameraData/{cfg['project']}"
    proj_path = f"CameraData/{cfg['project']}"
    fuse_script = TILLS_PLY_DIR / "fuse_ply.py"
    clip_script = TILLS_PLY_DIR / "clip_ply.py"

    max_index = preset.get("max_index", 89)
    f = preset.get("fuse", {})

    def _log(line: str):
        state.add_log(line)
        broadcaster.broadcast("log", line)

    try:
        # Step 1: Fuse
        before_combine = set(p.name for p in proj_dir.glob("*combine*.ply"))
        fuse_args = [
            sys.executable, str(fuse_script),
            "--path", proj_path,
            "--max-index", str(max_index),
            "--radius-scale", str(f.get("radius_scale", 1.0)),
            "--height-up", str(f.get("height_up", 2)),
            "--height-down", str(f.get("height_down", 0.5)),
            "--indices", " ".join(str(i) for i in indices),
        ]
        if f.get("bias"):
            fuse_args.append("--bias")
            fuse_args.extend(["--bias-margin", str(f.get("bias_margin", 0.05))])

        _log(f"fuse: {' '.join(str(a) for a in fuse_args)}")
        result = subprocess.run(
            fuse_args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=3600,
        )
        for line in result.stdout.split("\n"):
            if line.strip():
                _log(line)
        if result.returncode != 0:
            _log(f"FUSE FAILED (exit {result.returncode})")
            if result.stderr:
                for line in result.stderr.split("\n"):
                    if line.strip():
                        _log(f"[stderr] {line}")
            return

        # Find the newly created combine PLY
        combine_plys_after = list(proj_dir.glob("*combine*.ply"))
        new_combine = None
        for cp in combine_plys_after:
            if cp.name not in before_combine:
                new_combine = cp
                break
        if not new_combine and combine_plys_after:
            new_combine = max(combine_plys_after, key=lambda p: p.stat().st_mtime)
            _log(f"未检测到新合成 PLY，使用最新: {new_combine.name}")

        _log(f"fuse 完成 → {new_combine.name if new_combine else 'unknown'}")

        # Step 2: Clip (auto follows fuse)
        if new_combine:
            clip_args = [
                sys.executable, str(clip_script),
                "--path", proj_path,
                "--files", new_combine.name,
            ]
            _log(f"clip: {' '.join(str(a) for a in clip_args)}")
            result = subprocess.run(
                clip_args, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=3600,
            )
            for line in result.stdout.split("\n"):
                if line.strip():
                    _log(line)
            if result.returncode == 0:
                _log("clip 完成")
            else:
                _log(f"CLIP FAILED (exit {result.returncode})")

    except subprocess.TimeoutExpired:
        _log("TIMEOUT: fuse+clip 超时 (1h)")
    except Exception as e:
        _log(f"ERROR: {e}")
    finally:
        state.current_task = None
        state.scan_plys()
        broadcaster.broadcast("status", "done")


def run_render(state: FuseState, cfg: dict, preset: dict,
               indices: list[int], broadcaster: SSEBroadcaster):
    """Execute Playwright render in a background thread."""
    proj_dir = ROOT / f"CameraData/{cfg['project']}"

    def _log(line: str):
        state.add_log(line)
        broadcaster.broadcast("log", line)

    try:
        plys = sorted(proj_dir.glob("*.ply"))
        if not indices or indices[0] < 1 or indices[0] > len(plys):
            _log("ERROR: invalid PLY index")
            return
        ply_path = plys[indices[0] - 1]

        _log(f"render: {ply_path.name}")

        # Reuse v6's render logic via import
        # Build a minimal args namespace for async_main_v6
        class _Args:
            config = cfg.get("_config_path", "")
            force = False
            steps = "render"

        # The v6 render path uses the ply selected through its own UI flow.
        # For v8, we set the ply path directly and trigger render.
        from run_pipeline_v6 import async_main_v6
        asyncio.run(async_main_v6(_Args(), cfg))

        _log("render 完成")

    except Exception as e:
        _log(f"RENDER ERROR: {e}")
    finally:
        state.current_task = None
        broadcaster.broadcast("status", "done")


# ── HTTP Handler ─────────────────────────────────────────────────────────────────

def _make_fuse_routes(state: FuseState, cfg: dict, preset: dict,
                      force: bool, broadcaster: SSEBroadcaster):
    def _root(handler):
        return build_fuse_page(state), "text/html; charset=utf-8"

    def _fuse(handler, body):
        if state.current_task:
            return json.dumps({"status": "error",
                               "message": f"任务进行中: {state.current_task}"}), \
                   "application/json; charset=utf-8"
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                pass
        indices = body.get("indices", [])
        if not indices:
            return json.dumps({"status": "error", "message": "未选择 PLY"}), \
                   "application/json; charset=utf-8"
        state.current_task = "fuse+clip"
        t = threading.Thread(
            target=run_fuse_clip,
            args=(state, cfg, preset, indices, force, broadcaster),
            daemon=True,
        )
        t.start()
        return json.dumps({"status": "ok",
                           "message": f"fuse+clip started for indices {indices}"}), \
               "application/json; charset=utf-8"

    def _render(handler, body):
        if state.current_task:
            return json.dumps({"status": "error",
                               "message": f"任务进行中: {state.current_task}"}), \
                   "application/json; charset=utf-8"
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                pass
        indices = body.get("indices", [])
        if not indices:
            return json.dumps({"status": "error", "message": "未选择 PLY"}), \
                   "application/json; charset=utf-8"
        state.current_task = "render"
        t = threading.Thread(
            target=run_render,
            args=(state, cfg, preset, indices, broadcaster),
            daemon=True,
        )
        t.start()
        return json.dumps({"status": "ok", "message": "render started"}), \
               "application/json; charset=utf-8"

    return {"/": _root, "/fuse": _fuse, "/render": _render}


# ── Polling loop ─────────────────────────────────────────────────────────────────

def poll_loop(state: FuseState, broadcaster: SSEBroadcaster,
              stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            changed = state.scan_plys()
            if changed:
                broadcaster.broadcast("status", "plys_updated")
        except Exception as e:
            state.add_log(f"poll error: {e}")
        stop_event.wait(state.poll_interval)


# ── main ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="v8 Fuse Server")
    parser.add_argument("--config", required=True,
                        help="Path to pipeline.json")
    parser.add_argument("--port", type=int, default=8081,
                        help="HTTP server port (default: 8081)")
    parser.add_argument("--force", action="store_true",
                        help="Force clean before fuse")
    args_p = parser.parse_args()

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
    if "preset" not in cfg:
        print("ERROR: Missing 'preset' in config"); sys.exit(1)

    preset = load_preset(cfg["preset"])
    poll_interval = cfg.get("poll_interval", 5)

    state = FuseState(project=cfg["project"],
                      preset_name=cfg["preset"],
                      poll_interval=poll_interval)
    broadcaster = SSEBroadcaster()

    # Scan initial PLY list
    state.scan_plys()

    # Build handler class dynamically
    FuseHandler = type("FuseHandler", (SSEHandler,), {
        "routes": _make_fuse_routes(state, cfg, preset,
                                    args_p.force, broadcaster),
        "sse_paths": {"/events"},
    })

    # Start poll loop
    stop_event = threading.Event()
    poll_thread = threading.Thread(
        target=poll_loop,
        args=(state, broadcaster, stop_event),
        daemon=True,
    )
    poll_thread.start()

    print(f"v8 Fuse Server — project: {cfg['project']}")
    with state._lock:
        ply_count = len(state.ply_files)
    print(f"  {ply_count} PLY(s) found")

    server = create_server("0.0.0.0", args_p.port, FuseHandler, broadcaster)
    try:
        run_server(server)
    except KeyboardInterrupt:
        print("\n  用户中断，正在停止...")
    finally:
        stop_event.set()
        poll_thread.join(timeout=5)
        print("  Fuse server stopped.")


if __name__ == "__main__":
    main()
