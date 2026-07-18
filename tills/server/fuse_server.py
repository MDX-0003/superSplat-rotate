#!/usr/bin/env python3
"""
Fuse Server — browser-based PLY selection → fuse+clip → render.

Three-column layout:
  - Fuse PLYs (multi-select) from ``CameraData/<proj>/`` (excl. combine)
  - Render PLYs (single-select) from ``CameraData/<proj>-clip/``
  - JSONs (single-select) from ``cfg["jsons_path"]``

Usage:
  # 项目初始化（首次使用）
  uv run python -m tills.server.fuse_server init 06

  # 启动服务（--config 支持简写项目名或完整路径）
  uv run python -m tills.server.fuse_server --config 06
  uv run python -m tills.server.fuse_server --config 06 --port 8081
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
PRESETS_FILE = _project_root / "tills_ply" / "presets.json"
PRESET_TEMPLATE_FILE = _project_root / "CameraData" / "_template" / "presets.json"
SUPERSPLAT_URL = "http://127.0.0.1:3000/"

# ── 关于 fuse_config.json / clip_config.json ─────────────────────────────────────
# 这两个文件位于 tills_ply/，仅用于**命令行直接执行** fuse_ply.py / clip_ply.py
# 时的参数默认值。fuse_server 和 ply_pipeline.py 都**不使用它们**：fuse_server 将
# presets.json 中的参数完整展开为 CLI args 传入，ply_pipeline.py 同理。
# 保留它们是为了方便开发调试时手动跑单步命令，避免每次都输入全部参数。
# ──────────────────────────────────────────────────────────────────────────────────


# ── Preset file I/O ──────────────────────────────────────────────────────────────

def _load_all_presets() -> dict:
    """Return the full presets dict ``{name: {...}}`` from presets.json."""
    if not PRESETS_FILE.exists():
        return {}
    with open(PRESETS_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("presets", {})


def _load_template_preset() -> dict:
    """Load the single template preset from CameraData/_template/presets.json."""
    if not PRESET_TEMPLATE_FILE.exists():
        return {}
    with open(PRESET_TEMPLATE_FILE, "r", encoding="utf-8") as f:
        templates = json.load(f).get("presets", {})
    return templates.get("template", {})


def _save_all_presets(presets: dict) -> None:
    """Atomically write the full presets dict back to presets.json."""
    data = {"_doc": "Named parameter presets for ply_pipeline.py.", "presets": presets}
    tmp = PRESETS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    tmp.replace(PRESETS_FILE)


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
        self.last_clipped_ply: str | None = None  # set after fuse+clip, consumed by page
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

        # ── render PLYs: <proj>-clip/*.ply (newest first) ──
        render_result = []
        if clip_dir.is_dir():
            plys = sorted(clip_dir.glob("*.ply"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for p in plys:
                mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
                size_mb = round(p.stat().st_size / 1024 ** 2, 1)
                render_result.append({
                    "name": p.name, "size_mb": size_mb,
                    "mtime": mtime, "path": str(p),
                })

        # ── JSON files: project dir cameras.json + cameras_align.json ──
        json_result = []
        for jname in ("cameras.json", "cameras_align.json"):
            jp = proj_dir / jname
            if jp.exists():
                json_result.append({"name": jname, "path": str(jp), "source": "proj"})

        # ── JSON files: jsons_path/*.json ──
        if self.jsons_dir and self.jsons_dir.is_dir():
            for p in sorted(self.jsons_dir.glob("*.json")):
                json_result.append({"name": p.name, "path": str(p), "source": "external"})

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
               display:flex;align-items:center;gap:8px;flex-wrap:wrap;
               position:sticky;top:0;z-index:1}
  .preview-bar .label{color:#7a7368;white-space:nowrap}
  .preview-bar .path{color:#3e3a35;font-family:Consolas,monospace;flex:1;
                     word-break:break-all}
  .preview-bar .clear{background:#c0392b;color:#fff;border:none;padding:2px 8px;
                      cursor:pointer;border-radius:3px;font-size:11px}
  .badge{display:inline-block;padding:1px 5px;border-radius:3px;
         font-size:11px;font-weight:600;margin-left:4px}
  .badge.main{background:#5b7c5a;color:#fff}
  .badge.pos{background:#d9cfb8;color:#5b5a4e}
  .render-table-wrap{max-height:320px;overflow-y:auto;border-radius:6px;
                      box-shadow:0 1px 3px rgba(0,0,0,.06)}
  .render-table-wrap table{box-shadow:none;border-radius:0;margin-bottom:0}
  .render-table-wrap::-webkit-scrollbar{width:6px}
  .render-table-wrap::-webkit-scrollbar-thumb{background:#c9bfa8;border-radius:3px}
  .json-section{margin-bottom:8px}
  .json-section h3{font-size:12px;color:#7a7368;margin:4px 0 3px;
                   padding-bottom:2px;border-bottom:1px solid #e8e0d3}
  .modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;
                  background:rgba(0,0,0,.45);z-index:1000;
                  justify-content:center;align-items:flex-start;padding-top:40px}
  .modal-overlay.open{display:flex}
  .modal-card{background:#f5f0e8;border-radius:8px;padding:24px 28px;
               max-width:980px;width:95%;max-height:85vh;overflow-y:auto;
               box-shadow:0 4px 20px rgba(0,0,0,.3)}
  .modal-card h2{font-size:20px;color:#5b7c5a;margin-bottom:12px}
  .modal-card .close{float:right;background:none;border:none;font-size:20px;
                      cursor:pointer;color:#7a7368;padding:0 4px}
  .modal-card .ms{background:#fffdf7;border:1px solid #d9cfb8;
                   border-radius:4px;padding:14px 18px;margin-bottom:14px}
  .modal-card .ms h3{color:#5b7c5a;font-size:16px;margin-bottom:8px;
                      padding-bottom:4px;border-bottom:1px solid #d9cfb8}
  .modal-card .fd{display:flex;align-items:center;gap:8px;margin-bottom:8px;
                   flex-wrap:wrap;line-height:1.6}
  .modal-card .fd label{font-size:13px;color:#5b5a4e;flex:0 0 170px;text-align:right}
  .modal-card .fd input[type="text"]{font-size:13px;padding:3px 5px}
  .modal-card .fd select{padding:3px 5px;border:1px solid #d9cfb8;
                           border-radius:3px;font-size:13px;background:#fffdf7}
  .tip{font-size:11px;color:#aaa295;flex:1;min-width:120px;line-height:1.4}
</style>
"""


def _preset_options_html(state: FuseState) -> str:
    """Build <option> tags for the preset selector dropdown."""
    presets = _load_all_presets()
    opts = ""
    for n in sorted(presets.keys()):
        sel = ' selected' if n == state.preset_name else ''
        opts += f'<option value="{n}"{sel}>{n}</option>'
    return opts


def build_fuse_page(state: FuseState) -> str:
    with state._lock:
        fuse_plys = list(state.fuse_plys)
        render_plys = list(state.render_plys)
        json_files = list(state.json_files)
        active = set(state.active_tasks)
        npm_ok = state.npm_ok
        log_lines = list(state.task_log[-50:])
        auto_select_name = state.last_clipped_ply
        state.last_clipped_ply = None  # one-shot consume

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
    auto_select_idx = -1
    for i, p in enumerate(render_plys):
        if auto_select_name and p["name"] == auto_select_name:
            auto_select_idx = i
        checked_attr = 'checked' if i == auto_select_idx else ''
        render_rows += f"""
        <tr class="row" data-col="render" data-idx="{i}"
            onclick="selectOne(this)">
          <td><input type="radio" name="render-ply" value="{i}"
                     {checked_attr}
                     onclick="event.stopPropagation()"></td>
          <td>{p['name']}</td>
          <td>{p['size_mb']} MB</td>
          <td>{p['mtime']}</td>
        </tr>"""

    # ── JSON column: build rows with source flag ──
    proj_json_rows = ""
    ext_json_rows = ""
    for i, j in enumerate(json_files):
        row_html = f"""
        <tr class="row" data-col="json" data-idx="{i}"
            onclick="selectOne(this)">
          <td><input type="radio" name="render-json" value="{i}"
                     data-path="{j['path']}"
                     onclick="event.stopPropagation()"></td>
          <td colspan="3">{j['name']}</td>
        </tr>"""
        if j.get("source") == "proj":
            proj_json_rows += row_html
        else:
            ext_json_rows += row_html

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
    &nbsp;|&nbsp; <a href="javascript:openPresets()"
         style="color:#5b7c5a;font-weight:600;text-decoration:none">[点击编辑Presets]</a>
  </div>

  <div class="grid">
    <!-- Fuse column -->
    <div class="col">
      <h2>Fuse PLYs（顺序敏感）</h2>
      <div class="render-table-wrap">
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
      </div>
      <p style="color:#7a7368;font-size:11px;margin:4px 0">
        点击选择（首个 = main，全部点保留），再次点击取消，顺序决定 combine 名称。
      </p>
      <label style="font-size:12px;color:#5b5a4e;margin-top:4px;display:block">
        Preset:
        <select id="fuse-preset" style="margin:4px 0;padding:2px 6px;
               border:1px solid #d9cfb8;border-radius:3px;font-size:12px;
               background:#fffdf7">
          {_preset_options_html(state)}
        </select>
      </label>
      <button class="fuse" {fuse_disabled} onclick="doFuse()">interpolate + fuse + clip 选中</button>
    </div>

    <!-- Render column -->
    <div class="col">
      <h2>Render PLYs（单选 · 最新在前）</h2>
      <div class="render-table-wrap">
      <table>
        <thead><tr><th></th><th>文件名</th><th>大小</th><th>时间</th></tr></thead>
        <tbody>{render_rows}</tbody>
      </table>
      </div>
    </div>

    <!-- JSON column -->
    <div class="col">
      <h2>JSONs（单选 · 全局单选）</h2>
      {"<div class='json-section'><h3>📁 项目 JSON</h3><table><thead><tr><th></th><th>文件名</th></tr></thead><tbody>" + proj_json_rows + "</tbody></table></div>" if proj_json_rows else ""}
      {"<div class='json-section'><h3>📂 外部 JSON (jsons_path)</h3><table><thead><tr><th></th><th>文件名</th></tr></thead><tbody>" + ext_json_rows + "</tbody></table></div>" if ext_json_rows else "<p style='color:#aaa295;font-size:11px;margin:4px 0'>jsons_path 无文件</p>"}
      {"<p style='color:#c0392b;font-size:11px;margin:4px 0'>JSON 列表为空（无项目 JSON 且 jsons_path 无文件）</p>" if render_no_json else ""}
      <button class="render-btn" {render_disabled} onclick="doRender()">render 选中</button>
      {"<p style='color:#c0392b;font-size:11px;margin:4px 0'>SuperSplat 未启动 (npm run serve)</p>" if not npm_ok else ""}
      {"<p style='color:#c0392b;font-size:11px;margin:4px 0'>Render PLY 列表为空 (尚未 fuse+clip)</p>" if render_no_ply else ""}
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

    // ── render / json selection ──
    function selectOne(tr) {{
      let radio = tr.querySelector('input[type="radio"]');
      radio.checked = true;
      let col = tr.dataset.col;
      for (let r of document.querySelectorAll('tr[data-col="' + col + '"]')) {{
        r.classList.remove('selected');
      }}
      tr.classList.add('selected');
      // persist JSON selection via localStorage
      if (col === 'json' && radio.dataset.path) {{
        localStorage.setItem('fuse-selected-json-path', radio.dataset.path);
      }}
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
      let preset = document.getElementById('fuse-preset').value;
      let r = await fetch('/fuse', {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{ply_indices: ply_indices, preset_name: preset}})
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

    // ── preset modal ──
    async function openPresets() {{
      let r = await fetch('/presets/data');
      let all = await r.json();
      let sel = document.getElementById('pm-select');
      sel.innerHTML = '<option value="">— 选择 Preset —</option>';
      for (let n of Object.keys(all.presets).sort()) {{
        sel.innerHTML += '<option value="' + n + '">' + n + '</option>';
      }}
      document.getElementById('preset-modal').classList.add('open');
    }}
    function closePresets() {{
      document.getElementById('preset-modal').classList.remove('open');
    }}
    async function refreshDropdowns() {{
      let r = await fetch('/presets/data');
      let all = await r.json();
      let names = Object.keys(all.presets).sort();
      let html = '';
      for (let n of names) {{ html += '<option value="' + n + '">' + n + '</option>'; }}
      let mainSel = document.getElementById('fuse-preset');
      if (mainSel) {{ let cur = mainSel.value; mainSel.innerHTML = html; mainSel.value = names.includes(cur) ? cur : names[0] || ''; }}
      let pmSel = document.getElementById('pm-select');
      if (pmSel) {{ pmSel.innerHTML = '<option value="">— 选择 Preset —</option>' + html; }}
    }}
    let pmName = '';
    async function pmLoad() {{
      pmName = document.getElementById('pm-select').value;
      let editor = document.getElementById('pm-editor');
      if (!pmName) {{ editor.style.display = 'none'; document.getElementById('pm-actions').style.display = 'none'; return; }}
      let r = await fetch('/presets/data');
      let all = await r.json();
      let p = all.presets[pmName];
      if (!p) return;
      editor.style.display = 'block';
      document.getElementById('pm-actions').style.display = 'flex';
      pmSet('pm-f-max_index', p.max_index);
      pmSet('pm-f-radius_scale', p.fuse?.radius_scale);
      pmSet('pm-f-height_up', p.fuse?.height_up);
      pmSet('pm-f-height_down', p.fuse?.height_down);
      pmSet('pm-f-bias', p.fuse?.bias, true);
      pmSet('pm-f-bias_margin', p.fuse?.bias_margin);
      pmSet('pm-f-bias_radius_percentile', p.fuse?.bias_radius_percentile);
      pmSet('pm-c-clip_percent', p.clip?.clip_percent);
      pmSet('pm-c-denoise', p.clip?.denoise, true);
      pmSet('pm-c-denoise_method', p.clip?.denoise_method, false, true);
      pmSet('pm-c-denoise_grid_cell', p.clip?.denoise_grid_cell);
      pmSet('pm-c-denoise_min_points', p.clip?.denoise_min_points);
      pmSet('pm-c-denoise_voxel_size', p.clip?.denoise_voxel_size);
      pmSet('pm-c-height_up', p.clip?.height_up);
      pmSet('pm-c-height_down', p.clip?.height_down);
      pmSet('pm-c-radius_scale', p.clip?.radius_scale);
      pmSet('pm-c-ring_delete', p.clip?.ring_delete, true);
      pmSet('pm-c-ring_outer_delta', p.clip?.ring_outer_delta);
      pmSet('pm-c-ring_inner_delta', p.clip?.ring_inner_delta);
      pmSet('pm-c-ring_height_up', p.clip?.ring_height_up);
      pmSet('pm-c-ring_height_down', p.clip?.ring_height_down);
      pmSet('pm-i-total', p.interpolate?.total);
      pmSet('pm-i-anchor_camera', p.interpolate?.anchor_camera, false, true);
      pmSet('pm-i-radius_scale', p.interpolate?.radius_scale);
      document.getElementById('pm-f-bias_margin').disabled = !p.fuse?.bias;
      document.getElementById('pm-f-bias_radius_percentile').disabled = !p.fuse?.bias;
      pmToggleDenoise(); pmToggleRing();
    }}
    function pmSet(id, val, isCb, isTxt) {{
      let el = document.getElementById(id);
      if (!el) return;
      if (isCb) {{ el.checked = !!val; }}
      else if (isTxt) {{ if (val != null) el.value = val; }}
      else {{ if (val != null) el.value = val; }}
    }}
    function pF(id) {{ let v=parseFloat(document.getElementById(id).value); return isNaN(v)?null:v; }}
    function pI(id) {{ let v=parseInt(document.getElementById(id).value); return isNaN(v)?null:v; }}
    async function pmSave() {{
      if (!pmName) return;
      let params = {{max_index: pI('pm-f-max_index'), fuse:{{}}, clip:{{}}, interpolate:{{}}}};
      params.fuse.radius_scale=pF('pm-f-radius_scale');
      params.fuse.height_up=pF('pm-f-height_up');
      params.fuse.height_down=pF('pm-f-height_down');
      params.fuse.bias=document.getElementById('pm-f-bias').checked;
      params.fuse.bias_margin=pF('pm-f-bias_margin');
      params.fuse.bias_radius_percentile=pI('pm-f-bias_radius_percentile');
      params.clip.clip_percent=pF('pm-c-clip_percent');
      params.clip.denoise=document.getElementById('pm-c-denoise').checked;
      params.clip.denoise_method=document.getElementById('pm-c-denoise_method').value;
      params.clip.denoise_grid_cell=pF('pm-c-denoise_grid_cell');
      params.clip.denoise_min_points=pI('pm-c-denoise_min_points');
      params.clip.denoise_voxel_size=pF('pm-c-denoise_voxel_size');
      params.clip.height_up=pF('pm-c-height_up');
      params.clip.height_down=pF('pm-c-height_down');
      params.clip.radius_scale=pF('pm-c-radius_scale');
      params.clip.ring_delete=document.getElementById('pm-c-ring_delete').checked;
      params.clip.ring_outer_delta=pF('pm-c-ring_outer_delta');
      params.clip.ring_inner_delta=pF('pm-c-ring_inner_delta');
      params.clip.ring_height_up=pF('pm-c-ring_height_up');
      params.clip.ring_height_down=pF('pm-c-ring_height_down');
      params.interpolate.total=pI('pm-i-total');
      params.interpolate.anchor_camera=document.getElementById('pm-i-anchor_camera').value;
      params.interpolate.radius_scale=pF('pm-i-radius_scale');
      let r=await fetch('/presets/save',{{method:'POST',
       headers:{{'Content-Type':'application/json'}},
       body:JSON.stringify({{name:pmName,params:params}})}});
      let d=await r.json();
      if(d.status==='ok'){{alert('已保存');}}
      else alert('ERROR: '+d.message);
    }}
    async function pmDelete() {{
      if(!pmName||!confirm('确认删除 preset: '+pmName+'？'))return;
      let r=await fetch('/presets/delete',{{method:'POST',
       headers:{{'Content-Type':'application/json'}},
       body:JSON.stringify({{name:pmName}})}});
      let d=await r.json();
      if(d.status==='ok'){{refreshDropdowns();}}
      else alert('ERROR: '+d.message);
    }}
    async function pmCreate() {{
      let name=prompt('新 Preset 名称:');
      if(!name||!name.trim())return;
      let r=await fetch('/presets/create',{{method:'POST',
       headers:{{'Content-Type':'application/json'}},
       body:JSON.stringify({{name:name.trim()}})}});
      let d=await r.json();
      if(d.status==='ok'){{refreshDropdowns();}}
      else alert('ERROR: '+d.message);
    }}
    function pmToggleDenoise() {{
      let b = document.getElementById('pm-c-denoise').checked;
      let method = document.getElementById('pm-c-denoise_method').value;
      let isRegion = (method === 'region-grow');
      document.getElementById('pm-c-denoise_method').disabled = !b;
      // region-grow only: show when denoise=on AND method=region-grow
      let showRegion = b && isRegion;
      ['pm-c-denoise_grid_cell','pm-c-height_up','pm-c-height_down'].forEach(id => {{
        let el = document.getElementById(id); if (!el) return;
        el.disabled = !showRegion; el.parentElement.style.display = showRegion ? '' : 'none';
      }});
      // components only: show when denoise=on AND method=components
      let showComp = b && !isRegion;
      let vxEl = document.getElementById('pm-c-denoise_voxel_size');
      if (vxEl) {{ vxEl.disabled = !showComp; vxEl.parentElement.style.display = showComp ? '' : 'none'; }}
      // shared: visible when denoise=on
      let mpEl = document.getElementById('pm-c-denoise_min_points');
      if (mpEl) {{ mpEl.disabled = !b; mpEl.parentElement.style.display = b ? '' : 'none'; }}
      // radius_scale: always visible; enabled when region-grow OR ring_delete is active
      let ringOn = document.getElementById('pm-c-ring_delete').checked;
      let rsEl = document.getElementById('pm-c-radius_scale');
      if (rsEl) rsEl.disabled = !(showRegion || ringOn);
    }}
    function pmToggleRing() {{
      let b = document.getElementById('pm-c-ring_delete').checked;
      ['pm-c-ring_outer_delta','pm-c-ring_inner_delta','pm-c-ring_height_up','pm-c-ring_height_down'].forEach(id => {{
        let el = document.getElementById(id); if (!el) return;
        el.disabled = !b; el.parentElement.style.display = b ? '' : 'none';
      }});
      // radius_scale shared between ring_delete and denoise region-grow
      let denoiseOn = document.getElementById('pm-c-denoise').checked;
      let isRegion = document.getElementById('pm-c-denoise_method').value === 'region-grow';
      let rsEl = document.getElementById('pm-c-radius_scale');
      if (rsEl) rsEl.disabled = !(b || (denoiseOn && isRegion));
    }}
    // ── restore persisted selections on page load ──
    (function() {{
      // auto-select render PLY row highlight (radio checked in HTML by server)
      let autoRadio = document.querySelector('input[name="render-ply"]:checked');
      if (autoRadio) {{
        let tr = autoRadio.closest('tr');
        if (tr) tr.classList.add('selected');
      }}
      // restore persisted JSON selection (iterate to avoid CSS-backslash bug)
      let savedPath = localStorage.getItem('fuse-selected-json-path');
      if (savedPath) {{
        let allRadios = document.querySelectorAll('input[name="render-json"]');
        for (let r of allRadios) {{
          if (r.dataset.path === savedPath) {{
            r.checked = true;
            let tr = r.closest('tr');
            if (tr) tr.classList.add('selected');
            break;
          }}
        }}
      }}
    }})();
  </script>

  <!-- Preset Editor Modal -->
  <div class="modal-overlay" id="preset-modal" onclick="if(event.target===this)closePresets()">
    <div class="modal-card">
      <button class="close" onclick="closePresets()">&times;</button>
      <h2>Preset 编辑器</h2>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap">
        <select id="pm-select" onchange="pmLoad()"
                style="padding:5px 8px;border:1px solid #d9cfb8;border-radius:3px;
                       font-size:15px;background:#fffdf7">
          <option value="">— 选择 Preset —</option>
        </select>
        <button onclick="pmCreate()" style="font-size:14px;padding:5px 12px">+ 新建</button>
        <span id="pm-actions" style="display:none;gap:6px">
          <button onclick="pmSave()" style="font-size:14px;padding:5px 12px">保存</button>
          <button onclick="pmDelete()" style="font-size:14px;padding:5px 12px;background:#c0392b;color:#fff;border:none;border-radius:3px;cursor:pointer">删除</button>
        </span>
      </div>
      <div id="pm-editor" style="display:none">
        <div class="ms"><h3>fuse 参数</h3>
          <div class="fd"><label>max_index</label><input type="text" id="pm-f-max_index" step="1" size="4"><span class="tip">拟合圆相机范围 id=0..max_index（含max_index）</span></div>
          <div class="fd"><label>radius_scale</label><input type="text" id="pm-f-radius_scale" step="0.01" size="5"><span class="tip"><1 收紧圆柱,只保留靠近圆心点</span></div>
          <div class="fd"><label>height_up (m)</label><input type="text" id="pm-f-height_up" step="0.1" size="4"><span class="tip">圆柱法向量上方保留高度,人物约2m</span></div>
          <div class="fd"><label>height_down (m)</label><input type="text" id="pm-f-height_down" step="0.1" size="4"><span class="tip">地面侧搜索范围。典型值 0.3~0.5</span></div>
          <div class="fd"><label>bias</label><input type="checkbox" id="pm-f-bias"
            onchange="let b=this.checked;document.getElementById('pm-f-bias_margin').disabled=!b;document.getElementById('pm-f-bias_radius_percentile').disabled=!b"><span class="tip">启用人物重叠分离,非main PLY施加XY平移</span></div>
          <div class="fd"><label>bias_margin (m)</label><input type="text" id="pm-f-bias_margin" step="0.01" size="5"><span class="tip">人物核心间额外安全距离。越大越暴力</span></div>
          <div class="fd"><label>bias_radius_percentile</label><input type="text" id="pm-f-bias_radius_percentile" step="1" size="4"><span class="tip">核心半径百分位数(0~100)。越小越紧</span></div>
        </div>
        <div class="ms"><h3>clip 参数</h3>
          <div class="fd"><label>clip_percent</label><input type="text" id="pm-c-clip_percent" step="0.01" size="5"><span class="tip">最外层球壳丢弃比例</span></div>
          <div class="fd"><label>denoise</label><input type="checkbox" id="pm-c-denoise"
            onchange="pmToggleDenoise()" title="启用去噪，移除孤立漂浮高斯点。两种算法可选：region-grow（2D网格密度峰+区域生长法，需要cameras.json拟合圆）或 components（3D体素连通分量法，纯几何方法，无需cameras.json）"><span class="tip">启用去噪,移除孤立漂浮高斯</span></div>
          <div class="fd"><label>denoise_method</label>
            <select id="pm-c-denoise_method" onchange="pmToggleDenoise()" title="去噪算法选择" style="padding:2px 4px;border:1px solid #d9cfb8;border-radius:3px;font-size:12px;background:#fffdf7">
              <option value="region-grow" title="2D网格密度峰值+8邻域区域生长法。将圆柱内点投影到2D平面，从密度最高格子出发生长，只保留与主体连通的区域。需要cameras.json拟合圆。适合去除散布在圆柱内但不连续的漂浮点">region-grow</option>
              <option value="components" title="3D体素化+26邻域BFS连通分量法。将点云体素化后找出所有连通分量，点数少于阈值的分量被当作漂浮物丢弃。不需要cameras.json。适合去除稀疏漂浮点，对密集孤立簇效果较差">components</option>
            </select><span class="tip">region-grow=网格区域生长 / components=连通分量</span></div>
          <div class="fd"><label>denoise_grid_cell (m)</label><input type="text" id="pm-c-denoise_grid_cell" step="0.01" size="5"><span class="tip">[region-grow] 2D网格边长, 默认0.15</span></div>
          <div class="fd"><label>denoise_min_points</label><input type="text" id="pm-c-denoise_min_points" step="1" size="4"><span class="tip">[region-grow] grid cell最低点数,默认30</span></div>
          <div class="fd"><label>denoise_voxel_size (m)</label><input type="text" id="pm-c-denoise_voxel_size" step="0.01" size="5"><span class="tip">[components] 3D体素边长,默认0.30</span></div>
          <div class="fd"><label>height_up (m)</label><input type="text" id="pm-c-height_up" step="0.1" size="4"><span class="tip">[region-grow] 圆柱上方保留高度</span></div>
          <div class="fd"><label>height_down (m)</label><input type="text" id="pm-c-height_down" step="0.1" size="4"><span class="tip">[region-grow] 圆柱下方保留高度</span></div>
          <div class="fd"><label>radius_scale</label><input type="text" id="pm-c-radius_scale" step="0.01" size="5"><span class="tip">[region-grow+ring] 拟合圆半径缩放</span></div>
          <div class="fd"><label>ring_delete</label><input type="checkbox" id="pm-c-ring_delete"
            onchange="pmToggleRing()" title="启用环形区域点删除。在两个同心圆之间（内圆收缩inner_delta，外圆扩张outer_delta）删除高斯点，用于清除圆环状伪影"><span class="tip">启用环形区域点删除</span></div>
          <div class="fd"><label>ring_outer_delta (m)</label><input type="text" id="pm-c-ring_outer_delta" step="0.01" size="5"><span class="tip">外环扩张量,默认0.5</span></div>
          <div class="fd"><label>ring_inner_delta (m)</label><input type="text" id="pm-c-ring_inner_delta" step="0.01" size="5"><span class="tip">内环收缩量,默认0.3</span></div>
          <div class="fd"><label>ring_height_up (m)</label><input type="text" id="pm-c-ring_height_up" step="0.1" size="4"><span class="tip">环形删除上高度</span></div>
          <div class="fd"><label>ring_height_down (m)</label><input type="text" id="pm-c-ring_height_down" step="0.1" size="4"><span class="tip">环形删除下高度</span></div>
        </div>
        <div class="ms"><h3>interpolate 参数</h3>
          <div class="fd"><label>total</label><input type="text" id="pm-i-total" step="1" size="4"><span class="tip">插值总帧数</span></div>
          <div class="fd"><label>anchor_camera</label><input type="text" id="pm-i-anchor_camera" placeholder="006" size="4"><span class="tip">锚点相机编号</span></div>
          <div class="fd"><label>radius_scale</label><input type="text" id="pm-i-radius_scale" step="0.01" size="5"><span class="tip">插值圆半径缩放系数</span></div>
        </div>
      </div>
    </div>
  </div>
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

    interp_script = TILLS_PLY_DIR / "interpolate_cameras_circle.py"
    fuse_script = TILLS_PLY_DIR / "fuse_ply.py"
    clip_script = TILLS_PLY_DIR / "clip_ply.py"
    max_index = preset.get("max_index", 89)
    f = preset.get("fuse", {})

    def _log(line: str):
        state.add_log(line)
        broadcaster.broadcast("log", line)
        logger.write("fuse", line)

    new_combine = None  # set after successful fuse, used in finally for auto-select
    try:
        # Step 0: Interpolate (generate cameras_align.json)
        ip = preset.get("interpolate", {})
        interp_args = [
            sys.executable, str(interp_script),
            "--path", proj_path,
            "--max-index", str(max_index),
            "--total", str(ip.get("total", 300)),
            "--anchor-camera", str(ip.get("anchor_camera", "006")),
            "--radius-scale", str(ip.get("radius_scale", 1.0)),
        ]
        _log(f"interpolate: {' '.join(str(a) for a in interp_args)}")
        result = subprocess.run(
            interp_args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=3600,
        )
        for line in result.stdout.split("\n"):
            if line.strip():
                _log(line)
        if result.returncode != 0:
            _log(f"INTERPOLATE FAILED (exit {result.returncode})")
            if result.stderr:
                for line in result.stderr.split("\n"):
                    if line.strip():
                        _log(f"[stderr] {line}")
            return
        _log("interpolate 完成")

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
            c = preset.get("clip", {})
            clip_args = [
                sys.executable, str(clip_script),
                "--path", proj_path,
                "--files", new_combine.name,
                "--clip-percent", str(c.get("clip_percent", 10.0)),
            ]
            # -- denoise --------------------------------------------------
            if c.get("denoise"):
                clip_args.append("--denoise")
                clip_args.extend(["--denoise-method", str(c.get("denoise_method", "region-grow"))])
                clip_args.extend(["--denoise-min-points", str(c.get("denoise_min_points", 30))])
                clip_args.extend(["--denoise-grid-cell", str(c.get("denoise_grid_cell", 0.15))])
                clip_args.extend(["--denoise-voxel-size", str(c.get("denoise_voxel_size", 0.30))])
                clip_args.extend(["--max-index", str(max_index)])
                clip_args.extend(["--radius-scale", str(c.get("radius_scale", 1.0))])
                if c.get("height_up") is not None:
                    clip_args.extend(["--height-up", str(c["height_up"])])
                if c.get("height_down") is not None:
                    clip_args.extend(["--height-down", str(c["height_down"])])
            else:
                clip_args.append("--no-denoise")
            # -- ring delete ----------------------------------------------
            if c.get("ring_delete"):
                clip_args.append("--ring-delete")
                clip_args.extend(["--max-index", str(max_index)])
                clip_args.extend(["--radius-scale", str(c.get("radius_scale", 1.0))])
                clip_args.extend(["--ring-outer-delta", str(c.get("ring_outer_delta", 0.5))])
                clip_args.extend(["--ring-inner-delta", str(c.get("ring_inner_delta", 0.3))])
                if c.get("ring_height_up") is not None:
                    clip_args.extend(["--ring-height-up", str(c["ring_height_up"])])
                if c.get("ring_height_down") is not None:
                    clip_args.extend(["--ring-height-down", str(c["ring_height_down"])])
            else:
                clip_args.append("--no-ring-delete")
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
            if new_combine is not None:
                state.last_clipped_ply = new_combine.name
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

            renders_dir = (
                Path(cfg["video_output_path"]) if "video_output_path" in cfg
                else proj_dir / "renders"
            )
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

def _build_presets_page() -> str:
    """GET /presets — full preset editor page."""
    presets = _load_all_presets()
    names = sorted(presets.keys())

    options = ""
    for n in names:
        options += f'<option value="{n}">{n}</option>'

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <title>v8 Presets — Editor</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;
          background:#f5f0e8;color:#3e3a35;padding:20px}}
    h1{{color:#5b7c5a;margin-bottom:6px;font-size:22px}}
    .nav{{margin-bottom:15px}}
    .nav a{{color:#5b7c5a;text-decoration:none;font-size:14px}}
    .toolbar{{display:flex;gap:10px;align-items:center;margin-bottom:15px;flex-wrap:wrap}}
    select,input[type="text"],input[type="text"]{{padding:4px 8px;
           border:1px solid #d9cfb8;border-radius:3px;font-size:13px;
           background:#fffdf7}}
    button{{background:#6b8e6b;color:#fff;border:none;padding:5px 12px;
            cursor:pointer;border-radius:3px;font-size:13px}}
    button.danger{{background:#c0392b}}
    button:disabled{{opacity:0.4;cursor:default}}
    .section{{background:#fffdf7;border:1px solid #d9cfb8;border-radius:6px;
              padding:12px 16px;margin-bottom:12px}}
    .section h2{{color:#5b7c5a;font-size:15px;margin-bottom:10px;
                 padding-bottom:4px;border-bottom:2px solid #d9cfb8}}
    .field{{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}}
    .field label{{font-size:13px;color:#5b5a4e;min-width:170px}}
    .field input[type="text"]{{width:90px}}
    .field input[type="text"]{{width:140px}}
    .field input[type="checkbox"]{{width:auto;margin-right:4px}}
    .tip{{font-size:11px;color:#aaa295;flex:1;min-width:120px;line-height:1.4;margin-left:2px}}
    #editor{{display:none}}
    #no-preset{{color:#7a7368;font-size:14px;padding:20px 0}}
    body{{max-width:1050px}}
  </style>
</head>
<body>
  <h1>🧩 v8 Preset Editor</h1>
  <div class="nav"><a href="/">← 返回主页面</a></div>
  <div class="toolbar">
    <select id="preset-select" onchange="loadPreset()">
      <option value="">— 选择 Preset —</option>
      {options}
    </select>
    <button onclick="doCreate()">+ 新建 Preset</button>
  </div>

  <div id="no-preset">请选择一个 Preset 以编辑参数。</div>

  <div id="editor">
    <div class="section">
      <h2>fuse 参数</h2>
      <div class="field"><label>max_index</label>
        <input type="text" id="f-max_index" step="1" size="4"><span class="tip">拟合圆相机范围 id=0..max_index（含max_index）</span></div>
      <div class="field"><label>radius_scale</label>
        <input type="text" id="f-radius_scale" step="0.01" size="5"><span class="tip"><1 收紧圆柱,只保留靠近圆心点</span></div>
      <div class="field"><label>height_up (m)</label>
        <input type="text" id="f-height_up" step="0.1" size="4"><span class="tip">圆柱法向量上方保留高度,人物约2m</span></div>
      <div class="field"><label>height_down (m)</label>
        <input type="text" id="f-height_down" step="0.1" size="4"><span class="tip">地面侧搜索范围。典型值 0.3~0.5</span></div>
      <div class="field"><label>bias</label>
        <input type="checkbox" id="f-bias" onchange="toggleBias()"><span class="tip">启用人物重叠分离,非main PLY施加XY平移</span></div>
      <div class="field"><label>bias_margin (m)</label>
        <input type="text" id="f-bias_margin" step="0.01" size="5"><span class="tip">人物核心间额外安全距离。越大越暴力</span></div>
      <div class="field"><label>bias_radius_percentile</label>
        <input type="text" id="f-bias_radius_percentile" step="1" size="4"><span class="tip">核心半径百分位数(0~100)。越小越紧</span></div>
    </div>
    <div class="section">
      <h2>clip 参数</h2>
      <div class="field"><label>clip_percent</label>
        <input type="text" id="c-clip_percent" step="0.01" size="5"><span class="tip">最外层球壳丢弃比例</span></div>
      <div class="field"><label>denoise</label>
        <input type="checkbox" id="c-denoise" onchange="toggleDenoise()" title="启用去噪，移除孤立漂浮高斯点。两种算法可选：region-grow（2D网格密度峰+区域生长法，需要cameras.json拟合圆）或 components（3D体素连通分量法，纯几何方法，无需cameras.json）"><span class="tip">启用去噪,移除孤立漂浮高斯</span></div>
      <div class="field"><label>denoise_method</label>
        <select id="c-denoise_method" onchange="toggleDenoise()" title="去噪算法选择" style="padding:2px 4px;border:1px solid #d9cfb8;border-radius:3px;font-size:13px;background:#fffdf7">
          <option value="region-grow" title="2D网格密度峰值+8邻域区域生长法。将圆柱内点投影到2D平面，从密度最高格子出发生长，只保留与主体连通的区域。需要cameras.json拟合圆。适合去除散布在圆柱内但不连续的漂浮点">region-grow</option>
          <option value="components" title="3D体素化+26邻域BFS连通分量法。将点云体素化后找出所有连通分量，点数少于阈值的分量被当作漂浮物丢弃。不需要cameras.json。适合去除稀疏漂浮点，对密集孤立簇效果较差">components</option>
        </select><span class="tip">region-grow=网格区域生长 / components=连通分量</span></div>
      <div class="field"><label>denoise_grid_cell (m)</label>
        <input type="text" id="c-denoise_grid_cell" step="0.01" size="5"><span class="tip">[region-grow] 2D网格边长,默认0.15</span></div>
      <div class="field"><label>denoise_min_points</label>
        <input type="text" id="c-denoise_min_points" step="1" size="4"><span class="tip">[region-grow] grid cell最低点数,默认30</span></div>
      <div class="field"><label>denoise_voxel_size (m)</label>
        <input type="text" id="c-denoise_voxel_size" step="0.01" size="5"><span class="tip">[components] 3D体素边长,默认0.30</span></div>
      <div class="field"><label>height_up (m)</label>
        <input type="text" id="c-height_up" step="0.1" size="4"><span class="tip">[region-grow] 圆柱上方保留高度</span></div>
      <div class="field"><label>height_down (m)</label>
        <input type="text" id="c-height_down" step="0.1" size="4"><span class="tip">[region-grow] 圆柱下方保留高度</span></div>
      <div class="field"><label>radius_scale</label>
        <input type="text" id="c-radius_scale" step="0.01" size="5"><span class="tip">[region-grow+ring] 拟合圆半径缩放</span></div>
      <div class="field"><label>ring_delete</label>
        <input type="checkbox" id="c-ring_delete" onchange="toggleRing()"><span class="tip">启用环形区域点删除</span></div>
      <div class="field"><label>ring_outer_delta (m)</label>
        <input type="text" id="c-ring_outer_delta" step="0.01" size="5"><span class="tip">外环扩张量,默认0.5</span></div>
      <div class="field"><label>ring_inner_delta (m)</label>
        <input type="text" id="c-ring_inner_delta" step="0.01" size="5"><span class="tip">内环收缩量,默认0.3</span></div>
      <div class="field"><label>ring_height_up (m)</label>
        <input type="text" id="c-ring_height_up" step="0.1" size="4"><span class="tip">环形删除上高度</span></div>
      <div class="field"><label>ring_height_down (m)</label>
        <input type="text" id="c-ring_height_down" step="0.1" size="4"><span class="tip">环形删除下高度</span></div>
    </div>
    <div class="section">
      <h2>interpolate 参数</h2>
      <div class="field"><label>total</label>
        <input type="text" id="i-total" step="1" size="4"><span class="tip">插值总帧数</span></div>
      <div class="field"><label>anchor_camera</label>
        <input type="text" id="i-anchor_camera" placeholder="006" size="4"><span class="tip">锚点相机编号</span></div>
      <div class="field"><label>radius_scale</label>
        <input type="text" id="i-radius_scale" step="0.01" size="5"><span class="tip">插值圆半径缩放系数</span></div>
    </div>
    <div style="display:flex;gap:10px;margin-top:10px">
      <button onclick="doSave()">保存</button>
      <button class="danger" onclick="doDelete()">删除此 Preset</button>
    </div>
  </div>

  <script>
    let currentName = '';
    let presetData = null;

    async function loadPreset() {{
      currentName = document.getElementById('preset-select').value;
      if (!currentName) {{
        document.getElementById('editor').style.display = 'none';
        document.getElementById('no-preset').style.display = 'block';
        return;
      }}
      document.getElementById('no-preset').style.display = 'none';
      let r = await fetch('/presets/data');
      let all = await r.json();
      presetData = all.presets[currentName];
      if (!presetData) return;
      document.getElementById('editor').style.display = 'block';
      populateForm(presetData);
    }}

    function populateForm(p) {{
      setVal('f-max_index', p.max_index);
      setVal('f-radius_scale', p.fuse?.radius_scale);
      setVal('f-height_up', p.fuse?.height_up);
      setVal('f-height_down', p.fuse?.height_down);
      setVal('f-bias', p.fuse?.bias, true);
      setVal('f-bias_margin', p.fuse?.bias_margin);
      setVal('f-bias_radius_percentile', p.fuse?.bias_radius_percentile);
      setVal('c-clip_percent', p.clip?.clip_percent);
      setVal('c-denoise', p.clip?.denoise, true);
      setVal('c-denoise_method', p.clip?.denoise_method, false, true);
      setVal('c-denoise_grid_cell', p.clip?.denoise_grid_cell);
      setVal('c-denoise_min_points', p.clip?.denoise_min_points);
      setVal('c-denoise_voxel_size', p.clip?.denoise_voxel_size);
      setVal('c-height_up', p.clip?.height_up);
      setVal('c-height_down', p.clip?.height_down);
      setVal('c-radius_scale', p.clip?.radius_scale);
      setVal('c-ring_delete', p.clip?.ring_delete, true);
      setVal('c-ring_outer_delta', p.clip?.ring_outer_delta);
      setVal('c-ring_inner_delta', p.clip?.ring_inner_delta);
      setVal('c-ring_height_up', p.clip?.ring_height_up);
      setVal('c-ring_height_down', p.clip?.ring_height_down);
      setVal('i-total', p.interpolate?.total);
      setVal('i-anchor_camera', p.interpolate?.anchor_camera, false, true);
      setVal('i-radius_scale', p.interpolate?.radius_scale);
      toggleBias(); toggleDenoise(); toggleRing();
    }}

    function setVal(id, val, isCheckbox, isText) {{
      let el = document.getElementById(id);
      if (!el) return;
      if (isCheckbox) {{ el.checked = !!val; }}
      else if (isText) {{ if (val !== undefined && val !== null) el.value = val; }}
      else {{ if (val !== undefined && val !== null) el.value = val; }}
    }}

    function toggleBias() {{
      let b = document.getElementById('f-bias').checked;
      document.getElementById('f-bias_margin').disabled = !b;
      document.getElementById('f-bias_radius_percentile').disabled = !b;
    }}
    function toggleDenoise() {{
      let b = document.getElementById('c-denoise').checked;
      let method = document.getElementById('c-denoise_method').value;
      let isRegion = (method === 'region-grow');
      document.getElementById('c-denoise_method').disabled = !b;
      // region-grow only: show when denoise=on AND method=region-grow
      let showRegion = b && isRegion;
      ['c-denoise_grid_cell','c-height_up','c-height_down'].forEach(id => {{
        let el = document.getElementById(id); if (!el) return;
        el.disabled = !showRegion; el.parentElement.style.display = showRegion ? '' : 'none';
      }});
      // components only: show when denoise=on AND method=components
      let showComp = b && !isRegion;
      let vxEl = document.getElementById('c-denoise_voxel_size');
      if (vxEl) {{ vxEl.disabled = !showComp; vxEl.parentElement.style.display = showComp ? '' : 'none'; }}
      // shared: visible when denoise=on
      let mpEl = document.getElementById('c-denoise_min_points');
      if (mpEl) {{ mpEl.disabled = !b; mpEl.parentElement.style.display = b ? '' : 'none'; }}
      // radius_scale: always visible; enabled when region-grow OR ring_delete is active
      let ringOn = document.getElementById('c-ring_delete').checked;
      let rsEl = document.getElementById('c-radius_scale');
      if (rsEl) rsEl.disabled = !(showRegion || ringOn);
    }}
    function toggleRing() {{
      let b = document.getElementById('c-ring_delete').checked;
      ['c-ring_outer_delta','c-ring_inner_delta','c-ring_height_up','c-ring_height_down'].forEach(id => {{
        let el = document.getElementById(id); if (!el) return;
        el.disabled = !b; el.parentElement.style.display = b ? '' : 'none';
      }});
      // radius_scale shared between ring_delete and denoise region-grow
      let denoiseOn = document.getElementById('c-denoise').checked;
      let isRegion = document.getElementById('c-denoise_method').value === 'region-grow';
      let rsEl = document.getElementById('c-radius_scale');
      if (rsEl) rsEl.disabled = !(b || (denoiseOn && isRegion));
    }}

    function floatVal(id) {{
      let v = parseFloat(document.getElementById(id).value);
      return isNaN(v) ? null : v;
    }}
    function intVal(id) {{
      let v = parseInt(document.getElementById(id).value);
      return isNaN(v) ? null : v;
    }}

    function collectParams() {{
      let params = {{max_index: intVal('f-max_index'),
                     fuse: {{}}, clip: {{}}, interpolate: {{}}}};
      params.fuse.radius_scale = floatVal('f-radius_scale');
      params.fuse.height_up = floatVal('f-height_up');
      params.fuse.height_down = floatVal('f-height_down');
      params.fuse.bias = document.getElementById('f-bias').checked;
      params.fuse.bias_margin = floatVal('f-bias_margin');
      params.fuse.bias_radius_percentile = intVal('f-bias_radius_percentile');
      params.clip.clip_percent = floatVal('c-clip_percent');
      params.clip.denoise = document.getElementById('c-denoise').checked;
      params.clip.denoise_method = document.getElementById('c-denoise_method').value;
      params.clip.denoise_grid_cell = floatVal('c-denoise_grid_cell');
      params.clip.denoise_min_points = intVal('c-denoise_min_points');
      params.clip.denoise_voxel_size = floatVal('c-denoise_voxel_size');
      params.clip.height_up = floatVal('c-height_up');
      params.clip.height_down = floatVal('c-height_down');
      params.clip.radius_scale = floatVal('c-radius_scale');
      params.clip.ring_delete = document.getElementById('c-ring_delete').checked;
      params.clip.ring_outer_delta = floatVal('c-ring_outer_delta');
      params.clip.ring_inner_delta = floatVal('c-ring_inner_delta');
      params.clip.ring_height_up = floatVal('c-ring_height_up');
      params.clip.ring_height_down = floatVal('c-ring_height_down');
      params.interpolate.total = intVal('i-total');
      params.interpolate.anchor_camera = document.getElementById('i-anchor_camera').value;
      params.interpolate.radius_scale = floatVal('i-radius_scale');
      return params;
    }}

    async function doSave() {{
      if (!currentName) return;
      let params = collectParams();
      let r = await fetch('/presets/save', {{
        method:'POST', headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{name: currentName, params: params}})
      }});
      let d = await r.json();
      alert(d.status === 'ok' ? '已保存' : ('ERROR: ' + d.message));
    }}

    async function doDelete() {{
      if (!currentName) return;
      if (!confirm('确认删除 preset: ' + currentName + '？')) return;
      let r = await fetch('/presets/delete', {{
        method:'POST', headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{name: currentName}})
      }});
      let d = await r.json();
      if (d.status === 'ok') location.reload();
      else alert('ERROR: ' + d.message);
    }}

    async function doCreate() {{
      let name = prompt('新 Preset 名称:');
      if (!name || !name.trim()) return;
      let r = await fetch('/presets/create', {{
        method:'POST', headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{name: name.trim()}})
      }});
      let d = await r.json();
      if (d.status === 'ok') location.reload();
      else alert('ERROR: ' + d.message);
    }}
  </script>
</body>
</html>"""


def _make_fuse_routes(state: FuseState, cfg: dict,
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
        preset_name = body.get("preset_name", state.preset_name)
        preset = _load_all_presets().get(preset_name)
        if not preset:
            return json.dumps({"status": "error",
                               "message": f"preset not found: {preset_name}"}), \
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

    # ── preset CRUD ──

    def _presets_page(handler):
        return _build_presets_page(), "text/html; charset=utf-8"

    def _presets_data(handler):
        presets = _load_all_presets()
        return json.dumps({"presets": presets}, ensure_ascii=False), \
               "application/json; charset=utf-8"

    def _presets_save(handler, body):
        if isinstance(body, str):
            body = json.loads(body)
        name = body.get("name", "")
        params = body.get("params", {})
        presets = _load_all_presets()
        if name not in presets:
            return json.dumps({"status": "error",
                               "message": f"preset not found: {name}"}), \
                   "application/json; charset=utf-8"
        # Merge only known sections
        p = presets[name]
        if "max_index" in params:
            p["max_index"] = params["max_index"]
        for section in ("fuse", "clip", "interpolate"):
            if section in params and isinstance(params[section], dict):
                p.setdefault(section, {})
                p[section].update(params[section])
        _save_all_presets(presets)
        logger.write("presets", f"saved preset '{name}'")
        return json.dumps({"status": "ok", "message": f"preset '{name}' saved"}), \
               "application/json; charset=utf-8"

    def _presets_delete(handler, body):
        if isinstance(body, str):
            body = json.loads(body)
        name = body.get("name", "")
        presets = _load_all_presets()
        if name not in presets:
            return json.dumps({"status": "error",
                               "message": f"preset not found: {name}"}), \
                   "application/json; charset=utf-8"
        del presets[name]
        _save_all_presets(presets)
        logger.write("presets", f"deleted preset '{name}'")
        return json.dumps({"status": "ok", "message": f"preset '{name}' deleted"}), \
               "application/json; charset=utf-8"

    def _presets_create(handler, body):
        if isinstance(body, str):
            body = json.loads(body)
        name = body.get("name", "").strip()
        if not name:
            return json.dumps({"status": "error", "message": "name is required"}), \
                   "application/json; charset=utf-8"
        presets = _load_all_presets()
        if name in presets:
            return json.dumps({"status": "error",
                               "message": f"preset '{name}' already exists"}), \
                   "application/json; charset=utf-8"
        import copy
        template = _load_template_preset()
        if not template:
            return json.dumps({"status": "error",
                               "message": "template not found in _template/presets.json"}), \
                   "application/json; charset=utf-8"
        presets[name] = copy.deepcopy(template)
        _save_all_presets(presets)
        logger.write("presets", f"created preset '{name}' from template")
        return json.dumps({"status": "ok", "message": f"preset '{name}' created"}), \
               "application/json; charset=utf-8"

    return {"/": _root, "/fuse": _fuse, "/render": _render,
            "/presets": _presets_page, "/presets/data": _presets_data,
            "/presets/save": _presets_save, "/presets/delete": _presets_delete,
            "/presets/create": _presets_create}


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
    # ── init subcommand (before argparse) ──
    if len(sys.argv) >= 2 and sys.argv[1] == "init":
        if len(sys.argv) < 3:
            print("ERROR: init 需要项目名参数")
            print("Usage: uv run python -m tills.server.fuse_server init <project>")
            print("Example: uv run python -m tills.server.fuse_server init 06")
            sys.exit(1)
        from tills.server._server import init_project
        init_project(sys.argv[2])
        return

    parser = argparse.ArgumentParser(description="v8 Fuse Server")
    parser.add_argument("--config", required=True,
                        help="Project name (e.g. 06) or path to pipeline.json")
    parser.add_argument("--port", type=int, default=8081,
                        help="HTTP server port (default: 8081)")
    parser.add_argument("--force", action="store_true",
                        help="Force clean before fuse")
    args_p = parser.parse_args()

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
    if "preset" not in cfg:
        print("ERROR: Missing 'preset' in config"); sys.exit(1)

    load_preset(cfg["preset"])  # validate preset exists at startup
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
        "routes": _make_fuse_routes(state, cfg,
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
