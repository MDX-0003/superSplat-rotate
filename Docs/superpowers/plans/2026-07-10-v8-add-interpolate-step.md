# v8 Fuse Server — 给 fuse+clip 按钮添加 interpolate 步骤

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 8081 Web 页面的 "fuse + clip" 按钮执行流程中增加 interpolate 步骤（interpolate_cameras_circle.py → fuse_ply.py → clip_ply.py），参数从 preset 的 `interpolate` 段读取。

**Architecture:** 仅修改 `tills/server/fuse_server.py` 中 `run_fuse_clip()` 一个函数。在现有 fuse 子进程之前新增 interpolate 子进程调用，读取 preset 中的 `interpolate` 字段（`total`, `anchor_camera`, `radius_scale`），拼装命令行参数后执行 `interpolate_cameras_circle.py`，输出 `cameras_align.json` 到项目目录。

**Tech Stack:** Python 3, subprocess, threading（现有架构）

---

## 背景

### 当前流程

```
浏览器点击 "fuse + clip 选中"
  → POST /fuse (body: {ply_indices, preset_name})
  → fuse_server.py: FuseHandler 路由到 do_fuse()
  → 线程启动 run_fuse_clip(state, cfg, preset, ply_indices, force, broadcaster, logger)
      → Step 1: subprocess.run(fuse_ply.py ...)  → combine-*.ply
      → Step 2: subprocess.run(clip_ply.py ...)  → *-clip/*.ply
```

### 目标流程

```
浏览器点击 "fuse + clip 选中"
  → ...同上...
  → 线程启动 run_fuse_clip(...)
      → Step 0: subprocess.run(interpolate_cameras_circle.py ...)  → cameras_align.json
      → Step 1: subprocess.run(fuse_ply.py ...)  → combine-*.ply
      → Step 2: subprocess.run(clip_ply.py ...)  → *-clip/*.ply
```

### 关键发现

1. `run_fuse_clip()` **不调用** `ply_pipeline.py` — 它直接以子进程方式跑各脚本
2. `ply_pipeline.py` 的 `run_pipeline()` 是 CLI 入口，走完整 interpolate → fuse → clip
3. 目前的 Web 按钮跳过了 interpolate，导致 render 步骤缺少 `cameras_align.json`
4. Preset 中已有 `interpolate` 段（`total`, `anchor_camera`, `radius_scale`），目前 Web UI 未使用

### 参数来源

Preset（如 `通用合并方案`）的结构：
```json
{
  "path": "CameraData/02",
  "max_index": 89,
  "interpolate": {
    "total": 250,
    "anchor_camera": "006",
    "radius_scale": 0.8
  },
  "fuse": { ... },
  "clip": { ... }
}
```

`max_index` 是顶层字段，fuse 和 interpolate 共用。

---

### Task 1: 在 `run_fuse_clip()` 中添加 interpolate 步骤

**Files:**
- Modify: `tills/server/fuse_server.py:767-810`（在 Step 1 fuse 之前插入 Step 0 interpolate）

**变更要点：**
- 从 `preset.get("interpolate", {})` 读取参数
- `max_index` 复用已有的 `max_index` 变量（line 759，来自 `preset.get("max_index", 89)`）
- 默认值对齐 `build_interpolate_args()`：`total=300`, `anchor_camera="006"`, `radius_scale=1.0`
- `TILLS_PLY_DIR / "interpolate_cameras_circle.py"` 已在目录中存在
- 失败时 `return`（与 fuse 失败处理一致，不继续执行后续步骤）
- 超时 3600s（与 fuse/clip 一致）

- [ ] **Step 1: 在 `fuse_script` / `clip_script` 变量定义处添加 interpolate 脚本路径**

在 `fuse_server.py` line 757-758 附近，添加 `interp_script`：

```python
fuse_script = TILLS_PLY_DIR / "fuse_ply.py"
clip_script = TILLS_PLY_DIR / "clip_ply.py"
interp_script = TILLS_PLY_DIR / "interpolate_cameras_circle.py"
```

- [ ] **Step 2: 在 `try:` 块最前面插入 Step 0: Interpolate**

在 `run_fuse_clip()` 的 `try:` 块中，在 `# Step 1: Fuse` 注释行之前插入以下代码块：

```python
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
```

- [ ] **Step 3: 验证 Python 语法和 JS brace 平衡**

```powershell
cd e:\work\26.7_SKNJ\supersplat
python -c "import py_compile; py_compile.compile('tills/server/fuse_server.py', doraise=True); print('OK')"
python -c "
from tills.server.fuse_server import build_fuse_page, FuseState, _build_presets_page
import re
for label, page in [('Main', build_fuse_page(FuseState(project='05', preset_name='test', jsons_dir=None))),
                    ('Presets', _build_presets_page())]:
    for i, s in enumerate(re.findall(r'<script>(.*?)</script>', page, re.DOTALL)):
        o, c = s.count('{'), s.count('}')
        print(f'{label} Script {i}: {{ = {o}, }} = {c}', 'OK' if o==c else '*** MISMATCH ***')
"
```

预期：所有 script 块 brace 平衡，Python 编译通过。

- [ ] **Step 4: 启动 fuse server 并手动测试**

```powershell
cd e:\work\26.7_SKNJ\supersplat
python -m tills.server.fuse_server --config CameraData/05/pipeline.json
```

浏览器打开 `http://localhost:8081`：
- 选择 PLY → 点击 "fuse + clip" 按钮
- 观察日志面板：应该先输出 `interpolate: ...` → `interpolate 完成` → 然后才是 fuse 和 clip
- 检查 `CameraData/05/` 中是否生成了 `cameras_align.json`

- [ ] **Step 5: 提交**

```bash
git add tills/server/fuse_server.py
git commit -m "feat(v8): add interpolate step to fuse+clip button in web UI

Interpolate runs before fuse, using params from preset's 'interpolate'
section (total, anchor_camera, radius_scale). Outputs cameras_align.json
to project directory for subsequent render step.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 更新按钮文本（可选 — 用户决定）

当前按钮文本是 `fuse + clip 选中`（line 348）。如果用户希望反映三步骤，可改为 `interpolate + fuse + clip`。

**Files:**
- Modify: `tills/server/fuse_server.py:348`

```python
# 当前
<button class="fuse" {fuse_disabled} onclick="doFuse()">fuse + clip 选中</button>
# 改为
<button class="fuse" {fuse_disabled} onclick="doFuse()">interpolate + fuse + clip</button>
```

> ⚠ 用户需确认是否要做此改动。

---

## Self-Review

1. **Spec coverage:** 需求为"给网页按钮加上 interpolate 步骤，参数从 preset 读取" — Task 1 完整覆盖，Task 2 为可选
2. **Placeholder scan:** 无 TBD / TODO / "add error handling" 等占位符。所有代码都是完整的可复制执行的
3. **Type consistency:** `max_index` 变量已在 line 759 定义为 `preset.get("max_index", 89)`，与 `run_fuse_clip()` 现有代码一致。`interp_script` 路径使用已有常量 `TILLS_PLY_DIR`
