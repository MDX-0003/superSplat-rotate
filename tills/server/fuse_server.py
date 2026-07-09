#!/usr/bin/env python3
"""
Fuse Server — browser-based PLY selection → fuse+clip → render.

Three-column layout:
  - Fuse PLYs (multi-select) from ``CameraData/<proj>/`` (excl. combine)
  - Render PLYs (single-select) from ``CameraData/<proj>-clip/``
  - JSONs (single-select) from ``cfg["jsons_path"]``

Usage:
  python -m tills.server.fuse_server --config CameraData/05/pipeline.json
  python -m tills.server.fuse_server --config CameraData/05/pipeline.json --port 8081
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

from tills._shared import ROOT, load_preset, check_dev_server, ensure_browser, \
    upload_ply, upload_json_file, render_video
from tills.server._server import (
    SSEBroadcaster, SSEHandler, create_server, run_server, FileLogger,
)

TILLS_PLY_DIR = _project_root / "tills_ply"
SUPERSPLAT_URL = "http://127.0.0.1:3000/"


# ── State ────────────────────────────────────────────────────────────────────────

class FuseState:
    """Thread-safe state for the fuse server — three file lists + npm status."""

    def __init__(self, project: str, preset_name: str, jsons_dir: str | None,
                 poll_interval: int = 5):
        self.project = project
        self.preset_name = preset_name
        self.jsons_dir = Path(jsons_dir) if jsons_dir else None
        self.poll_interval = poll_interval
        self.fuse_plys: list[dict] = []     # [{name, size_mb, mtime, path}]
        self.render_plys: list[dict] = []   # [{name, size_mb, mtime, path}]
        self.json_files: list[dict] = []    # [{name, path}]
        self.npm_ok: bool = False
        self.active_tasks: set[str] = set()  # {"fuse", "render"}
        self.task_log: list[str] = []
        self._lock = threading.Lock()

    def scan_all(self) -> bool:
        """Scan all three paths + npm. Returns True if anything changed."""
        proj_dir = ROOT / f"CameraData/{self.project}"
        clip_dir = proj_dir.parent / f"{proj_dir.name}-clip"

        # ── fuse PLYs: <proj>/*.ply, exclude "combine" ──
        #    Index (1-based) is the position in the FULL sorted glob
        #    (matching v6 behaviour).  fuse_ply.py uses these indices
        #    both for file lookup and combine filename generation.
        all_plys = sorted(proj_dir.glob("*.ply"))
        full_idx = {p.name: i + 1 for i, p in enumerate(all_plys)}
        fuse_result = []
        for p in all_plys:
            if "combine" in p.name.lower():
                continue
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
            size_mb = round(p.stat().st_size / 1024 ** 2, 1)
            fuse_result.append({
                "name": p.name, "size_mb": size_mb,
                "mtime": mtime, "path": str(p),
                "idx": full_idx[p.name],  # 1-based global index
            })

        # ── render PLYs: <proj>-clip/*.ply ──
        render_result = []
        if clip_dir.is_dir():
            for p in sorted(clip_dir.glob("*.ply")):
                mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
                size_mb = round(p.stat().st_size / 1024 ** 2, 1)
                render_result.append({
                    "name": p.name, "size_mb": size_mb,
                    "mtime": mtime, "path": str(p),
                })

        # ── JSON files: jsons_path/*.json ──
        json_result = []
        if self.jsons_dir and self.jsons_dir.is_dir():
            for p in sorted(self.jsons_dir.glob("*.json")):
                json_result.append({"name": p.name, "path": str(p)})

        # ── npm / SuperSplat dev server ──
        npm_ok = check_dev_server(SUPERSPLAT_URL)

        with self._lock:
            old_fuse = {f["name"] for f in self.fuse_plys}
            old_render = {f["name"] for f in self.render_plys}
            old_json = {f["name"] for f in self.json_files}
            old_npm = self.npm_ok

            self.fuse_plys = fuse_result
            self.render_plys = render_result
            self.json_files = json_result
            self.npm_ok = npm_ok

            return (
                {f["name"] for f in fuse_result} != old_fuse
                or {f["name"] for f in render_result} != old_render
                or {f["name"] for f in json_result} != old_json
                or npm_ok != old_npm
            )

    def add_log(self, line: str):
        with self._lock:
            self.task_log.append(line)
            if len(self.task_log) > 500:
                self.task_log = self.task_log[-500:]


# ── HTML page builder ────────────────────────────────────────────────────────────

_CSS = """
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;
       background:#f5f0e8;color:#3e3a35;padding:20px}
  h1{color:#5b7c5a;margin-bottom:10px;font-size:22px}
  .info{color:#7a7368;margin-bottom:10px;font-size:14px}
  .warn{color:#c0392b;font-weight:bold}
  .grid{display:flex;gap:16px;margin-bottom:10px}
  .col{flex:1;min-width:0}
  .col h2{color:#5b7c5a;font-size:15px;margin-bottom:6px;
          padding-bottom:4px;border-bottom:2px solid #d9cfb8}
  table{width:100%;border-collapse:collapse;margin-bottom:6px;
        background:#fffdf7;border-radius:6px;overflow:hidden;
        box-shadow:0 1px 3px rgba(0,0,0,.06)}
  th{text-align:left;padding:6px 8px;background:#e8e0d3;color:#5b5a4e;
     font-size:12px;font-weight:600}
  td{padding:6px 8px;border-bottom:1px solid #e8e0d3;font-size:12px}
  tr.row{cursor:pointer;user-select:none}
  tr.row:hover{background:#faf3e3}
  tr.row.selected{background:#e6f0e0}
  button{background:#6b8e6b;color:#fff;border:none;padding:6px 14px;
         cursor:pointer;border-radius:3px;font-size:13px;margin:4px}
  button:disabled{opacity:0.4;cursor:default}
  button.fuse{background:#5b7c5a}
  button.render-btn{background:#d4850a}
  .log-panel{background:#fdfaf2;border:1px solid #d9cfb8;border-radius:4px;
             margin-top:15px}
  .log-body{padding:10px 14px;max-height:300px;overflow-y:auto;font-size:12px;
            line-height:1.6;white-space:pre-wrap;
            font-family:Consolas,"Fira Code",monospace}
  .log-body::-webkit-scrollbar{width:6px}
  .log-body::-webkit-scrollbar-thumb{background:#c9bfa8;border-radius:3px}
  .preview-bar{background:#fffdf7;border:1px solid #d9cfb8;border-radius:4px;
               padding:8px 10px;margin-bottom:8px;font-size:13px;
               display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .preview-bar .label{color:#7a7368;white-space:nowrap}
  .preview-bar .path{color:#3e3a35;font-family:Consolas,monospace;flex:1;
                     word-break:break-all}
  .preview-bar .clear{background:#c0392b;color:#fff;border:none;padding:2px 8px;
                      cursor:pointer;border-radius:3px;font-size:11px}
  .badge{display:inline-block;padding:1px 5px;border-radius:3px;
         font-size:11px;font-weight:600;margin-left:4px}
  .badge.main{background:#5b7c5a;color:#fff}
  .badge.pos{background:#d9cfb8;color:#5b5a4e}
</style>
"""


def build_fuse_page(state: FuseState) -> str:
    with state._lock:
        fuse_plys = list(state.fuse_plys)
        render_plys = list(state.render_plys)
        json_files = list(state.json_files)
        active = set(state.active_tasks)
        npm_ok = state.npm_ok
        log_lines = list(state.task_log[-50:])

    # ── fuse column (ordered multi-select) ──
    fuse_rows = ""
    for i, p in enumerate(fuse_plys):
        gidx = p.get("idx", i + 1)  # 1-based global index
        fuse_rows += f"""
        <tr class="row" data-col="fuse" data-idx="{i}"
            data-gidx="{gidx}" data-name="{p['name']}"
            onclick="toggleFuseRow(this)" id="fuserow-{i}">
          <td style="width:20px"><span class="sel-mark" id="selmark-{i}"></span></td>
          <td style="width:30px;color:#7a7368;font-size:12px">[{gidx}]</td>
          <td>{p['name']}</td>
          <td>{p['size_mb']} MB</td>
          <td>{p['mtime']}</td>
        </tr>"""

    # ── render column (single-select) ──
    render_rows = ""
    for i, p in enumerate(render_plys):
        render_rows += f"""
        <tr class="row" data-col="render" data-idx="{i}"
            onclick="selectOne(this)">
          <td><input type="radio" name="render-ply" value="{i}"
                     onclick="event.stopPropagation()"></td>
          <td>{p['name']}</td>
          <td>{p['size_mb']} MB</td>
          <td>{p['mtime']}</td>
        </tr>"""

    # ── JSON column (single-select) ──
    json_rows = ""
    for i, j in enumerate(json_files):
        json_rows += f"""
        <tr class="row" data-col="json" data-idx="{i}"
            onclick="selectOne(this)">
          <td><input type="radio" name="render-json" value="{i}"
                     onclick="event.stopPropagation()"></td>
          <td colspan="3">{j['name']}</td>
        </tr>"""

    # ── button states ──
    fuse_disabled = 'disabled' if ('fuse' in active or not fuse_plys) else ''
    render_no_ply = not render_plys
    render_no_json = not json_files
    render_disabled = 'disabled' if (
        'render' in active or render_no_ply or render_no_json or not npm_ok
    ) else ''

    # Status bar
    parts = []
    if active:
        parts.append(', '.join(sorted(active)))
    if not npm_ok:
        parts.append('<span class="warn">SuperSplat 未启动</span>')
    status_str = ' | '.join(parts) if parts else '空闲'

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <title>v8 Fuse — {state.project}</title>
  {_CSS}
</head>
<body>
  <h1>🧩 v8 Fuse Server — project: {state.project}</h1>
  <div class="info">
    状态: {status_str}
    &nbsp;|&nbsp; Fuse PLY: {len(fuse_plys)} 个
    &nbsp;|&nbsp; Render PLY: {len(render_plys)} 个
    &nbsp;|&nbsp; JSON: {len(json_files)} 个
  </div>

  <div class="grid">
    <!-- Fuse column -->
    <div class="col">
      <h2>Fuse PLYs（顺序敏感）</h2>
      <div class="preview-bar" id="preview-bar" style="display:none">
        <span class="label">选中顺序:</span>
        <span id="preview-order"></span>
        <span style="flex:1"></span>
        <span class="label">→</span>
        <span class="path" id="preview-path"></span>
        <button class="clear" onclick="clearFuseSelection()">清空</button>
      </div>
      <table>
        <thead><tr><th></th><th>文件名</th><th>大小</th><th>时间</th></tr></thead>
        <tbody>{fuse_rows}</tbody>
      </table>
      <p style="color:#7a7368;font-size:11px;margin:4px 0">
        点击选择（首个 = main，全部点保留），再次点击取消，顺序决定 combine 名称。
      </p>
      <button class="fuse" {fuse_disabled} onclick="doFuse()">fuse + clip 选中</button>
    </div>

    <!-- Render column -->
    <div class="col">
      <h2>Render PLYs（单选）</h2>
      <table>
        <thead><tr><th></th><th>文件名</th><th>大小</th><th>时间</th></tr></thead>
        <tbody>{render_rows}</tbody>
      </table>
    </div>

    <!-- JSON column -->
    <div class="col">
      <h2>JSONs（单选）</h2>
      <table>
        <thead><tr><th></th><th>文件名</th></tr></thead>
        <tbody>{json_rows}</tbody>
      </table>
      <button class="render-btn" {render_disabled} onclick="doRender()">render 选中</button>
      {"<p style='color:#c0392b;font-size:11px;margin:4px 0'>SuperSplat 未启动 (npm run serve)</p>" if not npm_ok else ""}
      {"<p style='color:#c0392b;font-size:11px;margin:4px 0'>Render PLY 列表为空 (尚未 fuse+clip)</p>" if render_no_ply else ""}
      {"<p style='color:#c0392b;font-size:11px;margin:4px 0'>JSON 列表为空 (jsons_path 无文件)</p>" if render_no_json else ""}
      {"<p style='color:#c0392b;font-size:11px;margin:4px 0'>render 正在运行</p>" if 'render' in active else ""}
    </div>
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

    // ── ordered fuse selection ──
    let fuseOrder = [];  // array of {{idx, gidx, name}} in click order

    function updateFuseUI() {{
      // Clear all highlights / badges
      for (let r of document.querySelectorAll('tr[data-col="fuse"]')) {{
        r.classList.remove('selected');
      }}
      for (let m of document.querySelectorAll('.sel-mark')) {{
        m.innerHTML = '';
      }}
      // Apply highlights + badges in order
      fuseOrder.forEach((item, pos) => {{
        let tr = document.getElementById('fuserow-' + item.idx);
        if (tr) {{
          tr.classList.add('selected');
          let mark = document.getElementById('selmark-' + item.idx);
          if (pos === 0) {{
            mark.innerHTML = '<span class="badge main">主</span>';
          }} else {{
            mark.innerHTML = '<span class="badge pos">#' + (pos + 1) + '</span>';
          }}
        }}
      }});
      // Preview bar
      let bar = document.getElementById('preview-bar');
      let orderEl = document.getElementById('preview-order');
      let pathEl = document.getElementById('preview-path');
      if (fuseOrder.length === 0) {{
        bar.style.display = 'none';
      }} else {{
        bar.style.display = 'flex';
        let gidxs = fuseOrder.map(f => '[' + f.gidx + ']');
        orderEl.textContent = gidxs.join(' → ');
        // combine name uses global indices (matching fuse_ply.py output)
        let idxs = fuseOrder.map(f => f.gidx);
        pathEl.textContent = 'combine-' + idxs.join('-') + '.ply';
      }}
    }}

    function toggleFuseRow(tr) {{
      let idx = parseInt(tr.dataset.idx);
      let gidx = parseInt(tr.dataset.gidx);
      let name = tr.dataset.name;
      let pos = fuseOrder.findIndex(f => f.idx === idx);
      if (pos >= 0) {{
        fuseOrder.splice(pos, 1);
      }} else {{
        fuseOrder.push({{idx: idx, gidx: gidx, name: name}});
      }}
      updateFuseUI();
    }}

    function clearFuseSelection() {{
      fuseOrder = [];
      updateFuseUI();
    }}

    // ── render / json selection (unchanged) ──
    function selectOne(tr) {{
      let radio = tr.querySelector('input[type="radio"]');
      radio.checked = true;
      let col = tr.dataset.col;
      for (let r of document.querySelectorAll('tr[data-col="' + col + '"]')) {{
        r.classList.remove('selected');
      }}
      tr.classList.add('selected');
    }}

    function getRenderPlyIndex() {{
      let r = document.querySelector('input[name="render-ply"]:checked');
      return r ? parseInt(r.value) : null;
    }}
    function getJsonIndex() {{
      let r = document.querySelector('input[name="render-json"]:checked');
      return r ? parseInt(r.value) : null;
    }}

    // ── actions ──
    async function doFuse() {{
      if (!fuseOrder.length) {{ alert('请至少选择一个 PLY'); return; }}
      let ply_indices = fuseOrder.map(f => f.idx);
      let r = await fetch('/fuse', {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{ply_indices: ply_indices}})
      }});
      let d = await r.json();
      if (d.status === 'ok') location.reload();
      else alert(d.message || JSON.stringify(d));
    }}

    async function doRender() {{
      let ply_idx = getRenderPlyIndex();
      let json_idx = getJsonIndex();
      if (ply_idx === null) {{ alert('请选择一个 Render PLY'); return; }}
      if (json_idx === null) {{ alert('请选择一个 JSON 文件'); return; }}
      let r = await fetch('/render', {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{ply_index: ply_idx, json_index: json_idx}})
      }});
      let d = await r.json();
      if (d.status === 'ok') location.reload();
      else alert(d.message || JSON.stringify(d));
    }}
  </script>
</body>
</html>"""


# ── Actions ──────────────────────────────────────────────────────────────────────

def run_fuse_clip(state: FuseState, cfg: dict, preset: dict,
                  ply_indices: list[int], force: bool,
                  broadcaster: SSEBroadcaster, logger: FileLogger):
    """Execute fuse_ply.py → clip_ply.py in a background thread.

    Args:
        ply_indices: 0-based indices into ``state.fuse_plys``.
    """
    proj_dir = ROOT / f"CameraData/{cfg['project']}"
    proj_path = f"CameraData/{cfg['project']}"

    # Resolve indices to actual PLY paths (1-based for fuse_ply.py)
    with state._lock:
        fuse_list = list(state.fuse_plys)
    ply_paths = [Path(fuse_list[i]["path"]) for i in ply_indices
                 if 0 <= i < len(fuse_list)]
    if not ply_paths:
        _log_static("ERROR: no valid PLY indices", state, broadcaster, logger)
        with state._lock:
            state.active_tasks.discard("fuse")
        return

    # Build 1-based index list for fuse_ply.py
    # fuse_ply.py expects indices relative to ALL *.ply in the proj dir.
    # We need to map our filtered list back to the full list.
    all_plys = sorted(proj_dir.glob("*.ply"))
    name_to_idx = {p.name: i + 1 for i, p in enumerate(all_plys)}  # 1-based
    one_based = []
    for pp in ply_paths:
        idx = name_to_idx.get(pp.name)
        if idx:
            one_based.append(idx)

    fuse_script = TILLS_PLY_DIR / "fuse_ply.py"
    clip_script = TILLS_PLY_DIR / "clip_ply.py"
    max_index = preset.get("max_index", 89)
    f = preset.get("fuse", {})

    def _log(line: str):
        state.add_log(line)
        broadcaster.broadcast("log", line)
        logger.write("fuse", line)

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
            "--indices", " ".join(str(i) for i in one_based),
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
        with state._lock:
            state.active_tasks.discard("fuse")
        state.scan_all()
        broadcaster.broadcast("status", "done")


def _log_static(line: str, state: FuseState, broadcaster: SSEBroadcaster,
                logger: FileLogger):
    """One-shot log helper (no closure needed)."""
    state.add_log(line)
    broadcaster.broadcast("log", line)
    logger.write("fuse", line)


def run_render(state: FuseState, cfg: dict,
               ply_index: int, json_index: int,
               broadcaster: SSEBroadcaster, logger: FileLogger):
    """Execute Playwright render — directly using _shared.py functions.

    Args:
        ply_index: 0-based index into ``state.render_plys``.
        json_index: 0-based index into ``state.json_files``.
    """
    proj_name = cfg["project"]
    proj_dir = ROOT / f"CameraData/{proj_name}"
    fps = cfg.get("fps", 25)

    with state._lock:
        render_list = list(state.render_plys)
        json_list = list(state.json_files)

    if not (0 <= ply_index < len(render_list)):
        _log_render("ERROR: invalid PLY index", state, broadcaster, logger)
        with state._lock:
            state.active_tasks.discard("render")
        return
    if not (0 <= json_index < len(json_list)):
        _log_render("ERROR: invalid JSON index", state, broadcaster, logger)
        with state._lock:
            state.active_tasks.discard("render")
        return

    ply_path = Path(render_list[ply_index]["path"])
    json_path = Path(json_list[json_index]["path"])

    def _log(line: str):
        state.add_log(line)
        broadcaster.broadcast("log", line)
        logger.write("render", line)

    async def _do_render():
        """All Playwright ops in ONE event loop — page references survive."""
        pw, browser, page = await ensure_browser(SUPERSPLAT_URL)
        try:
            await upload_ply(page, ply_path)
            total_frames = await upload_json_file(page, json_path)
            if total_frames == 0:
                _log("ERROR: JSON 导入失败 (total_frames=0)")
                return

            renders_dir = proj_dir / "renders"
            renders_dir.mkdir(parents=True, exist_ok=True)
            expected_filename = f"{proj_name}.mp4"
            success = await render_video(page, total_frames, renders_dir,
                                         expected_filename, fps)
            if success:
                _log(f"render 完成 → {renders_dir / expected_filename}")
            else:
                _log("render 可能未完成，请检查 SuperSplat 页面")
        finally:
            try:
                await page.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await pw.stop()
            except Exception:
                pass

    try:
        _log(f"render PLY: {ply_path.name}")
        _log(f"render JSON: {json_path.name}")
        asyncio.run(_do_render())

    except Exception as e:
        _log(f"RENDER ERROR: {e}")
    finally:
        with state._lock:
            state.active_tasks.discard("render")
        broadcaster.broadcast("status", "done")


def _log_render(line: str, state: FuseState, broadcaster: SSEBroadcaster,
                logger: FileLogger):
    state.add_log(line)
    broadcaster.broadcast("log", line)
    logger.write("render", line)


# ── HTTP Handler ─────────────────────────────────────────────────────────────────

def _make_fuse_routes(state: FuseState, cfg: dict, preset: dict,
                      force: bool, broadcaster: SSEBroadcaster,
                      logger: FileLogger):
    def _root(handler):
        return build_fuse_page(state), "text/html; charset=utf-8"

    def _fuse(handler, body):
        with state._lock:
            if "fuse" in state.active_tasks:
                return json.dumps({"status": "error",
                                   "message": "fuse+clip 已在运行"}), \
                       "application/json; charset=utf-8"
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                pass
        ply_indices = body.get("ply_indices", [])
        if not ply_indices:
            return json.dumps({"status": "error", "message": "未选择 PLY"}), \
                   "application/json; charset=utf-8"
        with state._lock:
            state.active_tasks.add("fuse")
        t = threading.Thread(
            target=run_fuse_clip,
            args=(state, cfg, preset, ply_indices, force, broadcaster, logger),
            daemon=True,
        )
        t.start()
        return json.dumps({"status": "ok",
                           "message": f"fuse+clip started"}), \
               "application/json; charset=utf-8"

    def _render(handler, body):
        with state._lock:
            if "render" in state.active_tasks:
                return json.dumps({"status": "error",
                                   "message": "render 已在运行"}), \
                       "application/json; charset=utf-8"
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                pass
        ply_index = body.get("ply_index")
        json_index = body.get("json_index")
        if ply_index is None:
            return json.dumps({"status": "error", "message": "未选择 Render PLY"}), \
                   "application/json; charset=utf-8"
        if json_index is None:
            return json.dumps({"status": "error", "message": "未选择 JSON"}), \
                   "application/json; charset=utf-8"
        with state._lock:
            state.active_tasks.add("render")
        t = threading.Thread(
            target=run_render,
            args=(state, cfg, ply_index, json_index, broadcaster, logger),
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
            changed = state.scan_all()
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
    jsons_dir = cfg.get("jsons_path")

    proj_dir = ROOT / f"CameraData/{cfg['project']}"
    state = FuseState(project=cfg["project"],
                      preset_name=cfg["preset"],
                      jsons_dir=jsons_dir,
                      poll_interval=poll_interval)
    broadcaster = SSEBroadcaster()
    logger = FileLogger(proj_dir, prefix="fuse")

    # Scan initial state
    state.scan_all()

    # Build handler class dynamically
    FuseHandler = type("FuseHandler", (SSEHandler,), {
        "routes": _make_fuse_routes(state, cfg, preset,
                                    args_p.force, broadcaster, logger),
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

    with state._lock:
        fuse_n = len(state.fuse_plys)
        render_n = len(state.render_plys)
        json_n = len(state.json_files)
        npm_str = "OK" if state.npm_ok else "DOWN"
    print(f"v8 Fuse Server — project: {cfg['project']}")
    print(f"  Fuse PLYs: {fuse_n}  |  Render PLYs: {render_n}"
          f"  |  JSONs: {json_n}  |  npm: {npm_str}")

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
