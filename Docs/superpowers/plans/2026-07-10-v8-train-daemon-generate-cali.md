# v8 Train Daemon — 生成位姿（COLMAP 标定）按钮

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** train daemon (8080) 页面添加帧单选 + "生成位姿"按钮，将选中帧的图片拷贝到 LiteGSWin 标定目录并运行 COLMAP 重建。

**Architecture:** 仅改 `tills/server/train_daemon.py`。POST `/action` 新增 `generate_cali` 命令，后台线程执行备份→拷贝→`prepare_calibration.py`，SSE 实时推送日志。

**Tech Stack:** Python 3, threading, subprocess, SSE, vanilla JS

---

## 需求确认

| 决策 | 选择 |
|------|------|
| COLMAP 执行方式 | 后台子进程 + SSE 日志推送 |
| 已有标定数据 | 直接剪切到 `old-cali/<sub_dir>-N`，不弹窗确认 |
| 帧选中方式 | Radio 按钮（每行一个，全局单选） |
| litegs_path | 从 `pipeline.json` 的 `litegs_path` 读取 |

---

### Task 1: HTML — 帧表格添加 Radio 列 + "生成位姿"按钮

**Files:**
- Modify: `tills/server/train_daemon.py:255-265`（`build_page()` 中 rows_html 生成）
- Modify: `tills/server/train_daemon.py:310-325`（HTML header 区域）

- [ ] **Step 1.1: 帧表格添加 radio 列**

修改表头和每行 HTML，在"操作"列前加入"位姿"列：

```python
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
      <td><input type="radio" name="cali-frame" value="{f['key']}"
                 data-dirname="{f['dirname']}"
                 onchange="updateCaliButton()"></td>
      <td>{f['frame_id']}</td>
      <td><span class="st-{f['status']}">{f['status']}</span></td>
      <td>{f['worker_id'] or '—'}</td>
      <td>{iter_str}</td>
      <td>{actions}</td>
    </tr>"""
```

表头修改:

```html
<thead>
  <tr><th>位姿</th><th>帧号</th><th>状态</th><th>Worker</th><th>迭代</th><th>操作</th></tr>
</thead>
```

- [ ] **Step 1.2: 在扫描控制区域添加"生成位姿"按钮**

在 `scan-control` div 中添加按钮（与开始/停止扫描按钮同行）:

```python
  <div id="scan-control" ...>
    <button id="btn-scan-start" ...>▶ 开始扫描</button>
    <button id="btn-scan-stop" ...>⏹ 停止扫描</button>
    <span id="scan-status" ...>● 扫描已暂停</span>
    <span style="flex:1"></span>
    <span id="cali-status" style="font-size:13px;color:#1565c0;display:none">🔄 标定中...</span>
    <button id="btn-cali" onclick="doGenerateCali()"
            style="background:#1565c0;padding:8px 18px;font-size:14px" disabled>📷 生成位姿</button>
  </div>
```

**按钮互斥规则：**

| 状态 | 开始扫描 | 停止扫描 | 生成位姿 |
|------|:---:|:---:|:---:|
| 扫描已停止，无帧选中 | enabled | — | disabled |
| 扫描已停止，有帧选中 | enabled | — | **enabled** |
| 扫描中 | — | enabled | disabled |
| **标定执行中** | **disabled** | — | disabled（显示"🔄 标定中..."） |

> "开始扫描"按钮在 cali 期间 disabled（避免扫描分发干扰 COLMAP 重建）。

### JS 更新

`updateCaliButton()` 改为处理三种状态：

```javascript
  function updateCaliButton() {{
    let scanningStopped = document.getElementById('btn-scan-start').style.display !== 'none';
    let selected = document.querySelector('input[name="cali-frame"]:checked');
    let caliRunning = document.getElementById('cali-status').style.display !== 'none';
    let btn = document.getElementById('btn-cali');
    let startBtn = document.getElementById('btn-scan-start');

    if (caliRunning) {{
      btn.disabled = true;
      startBtn.disabled = true;   // 标定期间禁止启动扫描
    }} else {{
      btn.disabled = !(scanningStopped && selected);
      startBtn.disabled = false;
    }}
  }}
```

`doGenerateCali()` 成功后设置 cali 状态：

```javascript
    if (d.status === 'ok') {{
      document.getElementById('cali-status').style.display = '';
      updateCaliButton();
      alert(d.message || '标定任务已启动');
    }}
```

后台线程完成后通过 SSE 推送恢复状态（`cali_running = False`），前端更新 UI。

### 后端状态

`TrainState` 新增 `cali_running: bool = False`。`to_dict()` 输出该字段。后台线程启动时设为 `True`，结束时设为 `False` 并 `_emit_status()`。

---

### Task 2: JS — `updateCaliButton()` + `doGenerateCali()`

**Files:**
- Modify: `tills/server/train_daemon.py:177-240`（`_JS_SSE`）

- [ ] **Step 2.1: `updateCaliButton()` 函数**

按钮可点击条件：扫描已停止 AND 有 radio 被选中：

```javascript
  function updateCaliButton() {{
    let scanningStopped = document.getElementById('btn-scan-start').style.display !== 'none';
    let selected = document.querySelector('input[name="cali-frame"]:checked');
    let btn = document.getElementById('btn-cali');
    btn.disabled = !(scanningStopped && selected);
  }}
```

> 需要修改 `updateScanUI()` 末尾追加 `updateCaliButton();` 调用。

- [ ] **Step 2.2: 修改 `updateScanUI()` 联动 cali 按钮**

在 `updateScanUI()` 末尾添加:

```javascript
    updateCaliButton();
```

- [ ] **Step 2.3: `doGenerateCali()` 函数**

```javascript
  function doGenerateCali() {{
    let selected = document.querySelector('input[name="cali-frame"]:checked');
    if (!selected) {{ alert('请选择一个帧'); return; }}
    let key = selected.value;
    let dirname = selected.dataset.dirname;
    if (!confirm('将为帧 ' + key + ' 生成标定位姿，确认？')) return;
    fetch('/action', {{method:'POST',
     headers:{{'Content-Type':'application/json'}},
     body: JSON.stringify({{action: 'generate_cali', key: key, dirname: dirname}})
    }}).then(r => r.json()).then(d => {{
      if (d.status === 'ok') {{
        alert(d.message || '标定任务已启动');
      }} else {{
        alert('ERROR: ' + (d.message || JSON.stringify(d)));
      }}
    }});
  }}
```

- [ ] **Step 2.4: SSE status 事件中保持 radio 状态**

radio selection 在 SSE 更新表格时不应被清除。当前 SSE status 事件是增量更新单元格内容而非重建 DOM，radio 不受影响。**无需额外改动**。

---

### Task 3: `handle_action()` — 新增 `generate_cali` 分支

**Files:**
- Modify: `tills/server/train_daemon.py:312-330`（`handle_action()`）

- [ ] **Step 3.1: 在扫描开关分支后、per-frame 分支前插入**

```python
    # ── generate calibration (spawns background thread) ──
    if action == "generate_cali":
        key = body.get("key", "")
        dirname = body.get("dirname", "")
        if not key or not dirname:
            return {"status": "error", "message": "缺少帧信息"}
        # parse sub_dir from dirname (e.g. "120-2026-06-30-120849" → "0630")
        try:
            from tills._shared import parse_frame_dirname
            sub_dir, frame_id = parse_frame_dirname(dirname)
        except ValueError as e:
            return {"status": "error", "message": f"无法解析帧目录名: {e}"}
        # spawn background thread (non-blocking)
        t = threading.Thread(
            target=run_generate_cali,
            args=(state, key, sub_dir, dirname, cfg),
            daemon=True,
        )
        t.start()
        return {"status": "ok",
                "message": f"标定任务已启动: {key} → calibration/{sub_dir}"}
```

> `cfg` 需要从外层作用域传入 `handle_action()`。当前函数签名不包含 cfg。需要在 `_make_routes()` 闭包中捕获 cfg，或将其存入 `TrainState`。

- [ ] **Step 3.2: 将 `cfg` 传入 `handle_action` 或存入 state**

方案：在 `_make_routes(state, cfg)` 中添加 cfg 参数，闭包捕获后传给 `handle_action`:

```python
def _make_routes(state: TrainState, cfg: dict):
    def _action(handler, body):
        ...
        result = handle_action(state, body, cfg)  # 新增 cfg
```

`handle_action` 签名改为 `def handle_action(state: TrainState, body: dict, cfg: dict) -> dict:`。

向后兼容：`cfg` 参数带默认值 `None`，per-frame 的 stop/clean 不需要 cfg。

---

### Task 4: `run_generate_cali()` — 后台标定线程

**Files:**
- Modify: `tills/server/train_daemon.py`（新增函数，放在 `handle_action` 之后）

- [ ] **Step 4: 新增函数**

```python
def run_generate_cali(state: TrainState, key: str, sub_dir: str,
                      dirname: str, cfg: dict):
    """Background thread: backup old cali → copy images → run COLMAP."""
    litegs_path = Path(cfg.get("litegs_path", ""))
    cali_root = litegs_path / "data" / "calibration"
    old_cali_root = litegs_path / "data" / "old-cali"
    raw_dir = Path(cfg.get("raw_images_path", ""))
    frame_dir = raw_dir / dirname

    def _log(msg: str):
        """Log to stdout (captured by daemon) + emit to SSE."""
        print(f"  [cali:{key}] {msg}")
        # SSE log: reuse the daemon log channel with a "cali" prefix
        # This will appear in the daemon log on the web page

    _log(f"开始: 为 {key} 生成标定位姿 (sub_dir={sub_dir})")

    # 1. Validate frame directory
    if not frame_dir.is_dir():
        _log(f"ERROR: 帧目录不存在: {frame_dir}")
        return

    # 2. Parse sub_dir from dirname
    cali_dir = cali_root / sub_dir

    # 3. Backup existing calibration
    if cali_dir.exists():
        # find next available backup index
        idx = 1
        while True:
            backup = old_cali_root / f"{sub_dir}-{idx}"
            if not backup.exists():
                break
            idx += 1
        old_cali_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cali_dir), str(backup))
        _log(f"已有标定数据已备份至: {backup}")

    # 4. Copy frame images to calibration directory
    cali_dir.mkdir(parents=True, exist_ok=True)
    image_count = 0
    for img in sorted(frame_dir.iterdir()):
        if img.is_file() and img.suffix.lower() in (
            ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"
        ):
            shutil.copy2(str(img), str(cali_dir / img.name))
            image_count += 1
    _log(f"已拷贝 {image_count} 张图片到 {cali_dir}")

    if image_count == 0:
        _log("ERROR: 帧目录中没有图片文件")
        return

    # 5. Run prepare_calibration.py
    script = litegs_path / "utils" / "prepare_calibration.py"
    cmd = [
        "uv", "run", "python", str(script),
        "--sub_dir", sub_dir,
    ]
    _log(f"执行: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=str(litegs_path),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=7200,  # 2h timeout for COLMAP
        )
        for line in result.stdout.split("\n"):
            if line.strip():
                _log(line)
        if result.returncode != 0:
            _log(f"COLMAP FAILED (exit {result.returncode})")
            if result.stderr:
                for line in result.stderr.split("\n"):
                    if line.strip():
                        _log(f"[stderr] {line}")
            return
        _log("标定位姿生成完成 ✓")
    except subprocess.TimeoutExpired:
        _log("TIMEOUT: COLMAP 超时 (2h)")
    except Exception as e:
        _log(f"ERROR: {e}")
```

> 需要在文件顶部确保 `import shutil` 和 `import subprocess` 已存在（train_daemon.py 已有）。

---

### Task 5: 更新 `_make_routes` 传入 cfg

**Files:**
- Modify: `tills/server/train_daemon.py:855-870`（`_make_routes()` 和调用处）

- [ ] **Step 5.1: `_make_routes` 签名加 cfg 参数**

```python
def _make_routes(state: TrainState, cfg: dict):
    def _action(handler, body):
        ...
        result = handle_action(state, body, cfg)
```

- [ ] **Step 5.2: 调用处传入 cfg**

找到 `main()` 中 `_make_routes(state)` 调用，改为 `_make_routes(state, cfg)`。

---

### Task 6: 验证

- [ ] **Step 6.1: Python 语法**

```powershell
cd e:\work\26.7_SKNJ\supersplat
uv run python -c "import py_compile; py_compile.compile('tills/server/train_daemon.py', doraise=True); print('OK')"
```

- [ ] **Step 6.2: 手动测试**

```powershell
uv run python -m tills.server.train_daemon --config 05
```

1. 打开 `http://localhost:8080` → 帧表有"位姿"列（radio按钮）
2. 扫描已暂停 + 未选中帧 → "生成位姿"按钮 disabled
3. 点选一个帧的 radio → 按钮变为 enabled
4. 点击"开始扫描" → 按钮变回 disabled（因为不再处于停止状态）
5. 切换回停止 → 仍选中帧 → 按钮恢复 enabled
6. 点击"生成位姿" → 弹窗确认 → 后台开始执行
7. 观察 daemon 日志面板是否有 cali 相关输出
8. 验证 `LiteGSWin/data/calibration/<sub_dir>/sparse/` 是否正确生成

- [ ] **Step 6.3: 提交**

```bash
git add tills/server/train_daemon.py
git commit -m "feat(v8): add 'generate cali' button to train daemon

- Radio column per frame row for single-select
- '生成位姿' button enabled only when scan stopped + frame selected
- Backend: backup old cali → copy images → run prepare_calibration.py
- Background thread with subprocess, COLMAP output streamed to SSE log

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

1. **Spec coverage:** 全部 grill 确认点覆盖 — 后台子进程、直接覆盖、radio 单选、litegs_path 复用
2. **Placeholder scan:** 无 TBD/TODO
3. **Type consistency:** `run_generate_cali` 的参数从 `body` JSON 透传，`dirname` 用于解析 `sub_dir`
4. **Edge cases:** 无图片、帧目录不存在、COLMAP 超时均已处理
5. **Button state machine:** disabled ← scan状态 OR 无选中 → enabled ← scan停止 AND 有选中
