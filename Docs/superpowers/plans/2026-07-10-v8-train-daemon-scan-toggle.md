# v8 Train Daemon — 开始/停止扫描按钮

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** train daemon (8080) 页面添加 "开始扫描" / "停止扫描" 按钮，控制 Phase 1（扫描 raw_images）和 Phase 2（分发+启动训练）的启停。Phase 3（监控已有进程+回收 PLY）始终运行。

**Architecture:** 仅改 `tills/server/train_daemon.py`。POST `/action` 新增 `scan_start` / `scan_stop` 命令，SSE 推送状态变化。

**Tech Stack:** Python 3, vanilla JS, SSE

---

## 需求确认

| 决策 | 选择 |
|------|------|
| 默认状态 | 停止扫描（`scanning_enabled = False`） |
| Phase 3 在停止时 | 继续运行（不中断已启动的训练） |
| 按钮位置 | train daemon (8080) |
| 实现方式 | POST `/action` + SSE 推送 |

---

### Task 1: TrainState 添加 `scanning_enabled` 字段

**Files:**
- Modify: `tills/server/train_daemon.py:62-72`（`TrainState.__init__`）
- Modify: `tills/server/train_daemon.py:96-127`（`to_dict()`）

- [ ] **Step 1.1: 添加字段**

```python
class TrainState:
    def __init__(self, project: str, poll_interval: int = 5):
        ...
        self.scanning_enabled: bool = False  # toggled by web UI start/stop button
        self._lock = threading.Lock()
```

- [ ] **Step 1.2: `to_dict()` 输出该字段**

在 `to_dict()` 的 `return` dict 中添加:

```python
return {
    "project": self.project,
    "poll_interval": self.poll_interval,
    "scanning_enabled": self.scanning_enabled,   # NEW
    "frames": frame_list,
    "workers": worker_list,
}
```

---

### Task 2: `main_loop()` 按条件跳过 Phase 1 + 2

**Files:**
- Modify: `tills/server/train_daemon.py:429-668`（main_loop 中 Phase 1 和 Phase 2）

- [ ] **Step 2.1: Phase 1 加 `scanning_enabled` 守卫**

在 Phase 1 开头（line ~439 `# ── 1. Scan raw_images/ ──` 之后，`if raw_dir.is_dir():` 之前）加守卫:

```python
            # ── 1. Scan raw_images/ ──
            if state.scanning_enabled:
                if raw_dir.is_dir():
                    ...  # 现有扫描逻辑保持不变
```

> 注意：需要将 Phase 1 的整个代码块缩进一级（放在 `if state.scanning_enabled:` 下）。

- [ ] **Step 2.2: Phase 2 加 `scanning_enabled` 守卫**

同理，Phase 2（line ~572 `# ── 2. Dispatch ready frames ──`）也需要包裹在 `if state.scanning_enabled:` 内。Phase 3 不加守卫。

实际结构变为:

```python
while not stop_event.is_set():
    _cycle += 1
    try:
        if not online_workers:
            _emit_status()
            stop_event.wait(state.poll_interval)
            continue

        if state.scanning_enabled:
            # ── 1. Scan raw_images/ ──
            ...  # 原有 Phase 1 代码

            # ── 2. Dispatch ready frames ──
            ...  # 原有 Phase 2 代码

        # ── 3. Monitor running processes ──  (always runs)
        ...  # 原有 Phase 3 代码
    except Exception:
        ...
```

- [ ] **Step 2.3: 停止时仍然打印心跳日志**

在 `if not state.scanning_enabled:` 时，打印一条简化日志避免输出完全空白:

```python
        if not state.scanning_enabled:
            if _cycle % 12 == 0:  # ~once per minute at 5s poll
                print(f"  [scan #{_cycle}] 扫描已暂停（点击"开始扫描"按钮恢复）")
```

---

### Task 3: `handle_action()` 新增 `scan_start` / `scan_stop`

**Files:**
- Modify: `tills/server/train_daemon.py:308-372`（`handle_action()` 函数）

- [ ] **Step 3.1: 在 `handle_action()` 开头添加扫描开关处理**

在现有 `key = body.get("key", "")` 之前插入:

```python
def handle_action(state: TrainState, body: dict) -> dict:
    """Process a user action."""
    action = body.get("action", "")

    # ── global scan toggle (no frame key needed) ──
    if action == "scan_start":
        state.scanning_enabled = True
        return {"status": "ok", "scanning_enabled": True,
                "message": "扫描已开启"}
    if action == "scan_stop":
        state.scanning_enabled = False
        return {"status": "ok", "scanning_enabled": False,
                "message": "扫描已停止"}

    # ── per-frame actions (require key) ──
    key = body.get("key", "")
    ...
```

> 注意：`action` 变量从 body 中提取要移到函数开头，原位置（line 314）删除。

---

### Task 4: HTML 页面添加按钮 + JS

**Files:**
- Modify: `tills/server/train_daemon.py:280-300`（`build_page()` 的 HTML）
- Modify: `tills/server/train_daemon.py:175-234`（`_JS_SSE`）

- [ ] **Step 4.1: 在页面 header 区添加扫描控制按钮**

在 `<h1>` 下方、`<div class="info">` 之前插入:

```python
    return f"""<!DOCTYPE html>
<html lang="zh">
...
<body>
  <h1>🚂 v8 Train Daemon — project: {d['project']}</h1>
  <div id="scan-control" style="margin:10px 0;display:flex;align-items:center;gap:10px">
    <button id="btn-scan-start" onclick="doScanToggle('scan_start')"
            style="background:#2e7d32;padding:8px 18px;font-size:14px">▶ 开始扫描</button>
    <button id="btn-scan-stop" onclick="doScanToggle('scan_stop')"
            style="background:#c0392b;padding:8px 18px;font-size:14px;display:none">⏹ 停止扫描</button>
    <span id="scan-status" style="font-size:14px;color:#c0392b">● 扫描已暂停</span>
  </div>
  <div class="info">
    ...
```

> 按钮初始状态: "开始扫描" 可见，"停止扫描" 隐藏（因为默认 `scanning_enabled=False`）。

- [ ] **Step 4.2: JS 添加 `doScanToggle()` 函数 + SSE 状态同步**

在 `_JS_SSE` 的 `<script>` 内添加:

```javascript
  function doScanToggle(cmd) {{
    fetch('/action', {{method:'POST',
     headers:{{'Content-Type':'application/json'}},
     body: JSON.stringify({{action: cmd}})
    }}).then(r => r.json()).then(d => {{
      if (d.status === 'ok') {{
        updateScanUI(d.scanning_enabled);
      }}
    }});
  }}
  function updateScanUI(scanning) {{
    document.getElementById('btn-scan-start').style.display = scanning ? 'none' : '';
    document.getElementById('btn-scan-stop').style.display = scanning ? '' : 'none';
    let st = document.getElementById('scan-status');
    st.textContent = scanning ? '● 扫描中...' : '● 扫描已暂停';
    st.style.color = scanning ? '#2e7d32' : '#c0392b';
  }}
```

- [ ] **Step 4.3: SSE status 事件中同步 scanning 状态**

在现有 SSE `status` 事件处理器末尾添加:

```javascript
  evtSource.addEventListener('status', function(e) {
    const data = JSON.parse(e.data);
    // ... 现有 frame/worker 更新逻辑 ...
    // 同步扫描开关状态
    if (data.hasOwnProperty('scanning_enabled')) {
      updateScanUI(data.scanning_enabled);
    }
  });
```

---

### Task 5: `_emit_status()` 在每次轮询中广播扫描状态

**Files:**
- Modify: `tills/server/train_daemon.py:421-423`（`_emit_status()` 函数）

`_emit_status()` 已经调用 `state.to_dict()` 并广播。Task 1.2 已经在 `to_dict()` 中添加了 `scanning_enabled`，所以 SSE 消息会自动包含该字段。**无需额外修改**。

> 但当前 `_emit_status()` 只在少数路径被调用（无 online workers、emit_status 在每次 scan 后被手动调用）。需要确保主循环在停止扫描时也定期 emit status，以便前端知道当前扫描状态。

- [ ] **Step 5.1: 确保停止扫描时也定期推送状态**

在 Phase 1 守卫之后、`if not state.scanning_enabled:` 分支中，每隔若干周期调用一次 `_emit_status()`:

```python
        if not state.scanning_enabled:
            if _cycle % 12 == 0:
                print(f"  [scan #{_cycle}] 扫描已暂停")
                _emit_log("daemon", f"扫描已暂停 (#{_cycle})")
            _emit_status()   # keep SSE updated so frontend shows correct state
            stop_event.wait(state.poll_interval)
            continue
```

> 注意：`_emit_status()` 不能在 `continue` 之后，因为执行流已经在 while 循环开头了。应将这段逻辑放在 while 循环开头、在线 worker 检查之后。

实际编排:

```python
while not stop_event.is_set():
    _cycle += 1
    try:
        online_workers = [w for w in state.workers if w.is_online]
        if not online_workers:
            _emit_status()
            stop_event.wait(state.poll_interval)
            continue

        if not state.scanning_enabled:
            if _cycle % 12 == 0:
                print(f"  [scan #{_cycle}] 扫描已暂停")
                _emit_log("daemon", f"扫描已暂停 (#{_cycle})")
            _emit_status()
            stop_event.wait(state.poll_interval)
            continue

        # Phase 1: Scan
        ...
```

---

### Task 6: 验证

- [ ] **Step 6.1: Python 语法**

```powershell
cd e:\work\26.7_SKNJ\supersplat
uv run python -c "import py_compile; py_compile.compile('tills/server/train_daemon.py', doraise=True); print('OK')"
```

- [ ] **Step 6.2: 手动测试**

```powershell
uv run python -m tills.server.train_daemon --config CameraData/05/pipeline.json
```

1. 打开 `http://localhost:8080` → 应看到红色 "● 扫描已暂停"，按钮显示"开始扫描"
2. 点击 "开始扫描" → 状态变为绿色 "● 扫描中..."，按钮切换为"停止扫描"
3. 点击 "停止扫描" → 恢复暂停，已有训练进程不受影响
4. 在另一个窗口打开 → SSE 推送的 `scanning_enabled` 字段应与按钮状态一致
5. 已启动的训练在停止扫描后继续运行，Phase 3 正常监控和回收 PLY

- [ ] **Step 6.3: 提交**

```bash
git add tills/server/train_daemon.py
git commit -m "feat(v8): add start/stop scan toggle to train daemon

- Default scanning_enabled=False on startup
- POST /action scan_start/scan_stop toggles scanning
- Phase 1 (scan) and Phase 2 (dispatch) respect the toggle
- Phase 3 (monitor running processes) always runs
- SSE status includes scanning_enabled for frontend sync

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

1. **Spec coverage:** 所有 grill 确认的点均已覆盖 — 默认停止、Phase 3 继续、8080 页面、POST API
2. **Placeholder scan:** 无 TBD/TODO
3. **Type consistency:** `scanning_enabled: bool` 在 `__init__`、`to_dict()`、`handle_action()`、JS 中类型一致
4. **线程安全:** `scanning_enabled` 的读写都是简单 bool 赋值/读取，GIL 保证了原子性，无需额外锁
5. **Phase 3 不受影响:** Phase 3（监控+回收）在 `if state.scanning_enabled:` 块之外，始终执行
