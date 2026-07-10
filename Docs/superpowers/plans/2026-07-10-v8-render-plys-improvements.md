# v8 Fuse Server — Render PLYs 滚动 + 倒序 + 项目 JSON 可选

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) Render PLYs 区域限制高度、支持滚动；(2) Render PLYs 按 mtime 倒序（最新在最上面）；(3) 项目目录下的 `cameras.json` 和 `cameras_align.json` 作为可选的 JSON 来源，与 jsons_path 的 JSON 分开展示但共享单选框（全局单选）。

**Architecture:** 仅改 `tills/server/fuse_server.py`。三处改动：`scan_all()` 中排序 + 扫描逻辑、HTML 渲染（CSS + 表结构）、JS 中 `getJsonIndex()` 和 render 按钮状态判断。不涉及 `run_render()` 后端逻辑变更 — — `json_index` 直接映射到合并后的 `json_files` 列表。

**Tech Stack:** Python 3, HTML/CSS, vanilla JS

---

## 需求分解

### 需求 1: Render PLYs 滚动

当前 Render PLYs 表无限增长。需要加 `max-height` + `overflow-y: auto`。

### 需求 2: Render PLYs 倒序

当前 `sorted()` 按字母序排列。改为按 `st_mtime` 降序，使最新 clip 的 PLY 在顶端。

### 需求 3: 项目目录 JSON 可选

`CameraData/<proj>/` 下的 `cameras.json`（始终存在）和 `cameras_align.json`（interpolate 后出现）应作为 JSON 选项。与 jsons_path 的外部 JSON **分开展示**（两个子区域），但**共享同一个 radio 组**（`name="render-json"`），确保全局单选且 `json_index` 无缝传递。

---

### Task 1: `scan_all()` — 倒序 + 扫描项目 JSON

**Files:**
- Modify: `tills/server/fuse_server.py:112-127`（`scan_all()` 中 render PLYs 和 JSON 扫描逻辑）

- [ ] **Step 1.1: Render PLYs 按 mtime 倒序排列**

将 `scan_all()` line 115 从字母序 `sorted()` 改为按修改时间降序：

```python
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
```

- [ ] **Step 1.2: 扫描项目目录下的 cameras.json 和 cameras_align.json**

在 `scan_all()` 的 "JSON files" 段（line 123-127）之前，插入项目 JSON 扫描：

```python
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
```

> 项目 JSON 在列表前部，索引更小，不影响外部 JSON 的索引稳定性。

---

### Task 2: HTML — Render PLYs 滚动 + JSON 分区展示

**Files:**
- Modify: `tills/server/fuse_server.py:159-197`（`_CSS` 中添加滚动样式）
- Modify: `tills/server/fuse_server.py:351-372`（Render 列和 JSON 列的 HTML）

- [ ] **Step 2.1: 添加 Render PLYs 滚动 CSS**

在 `_CSS` 变量中追加一条样式（例如在 table 样式之后）：

```css
  .render-table-wrap{max-height:320px;overflow-y:auto;border-radius:6px;
                      box-shadow:0 1px 3px rgba(0,0,0,.06)}
  .render-table-wrap table{box-shadow:none;border-radius:0;margin-bottom:0}
  .json-section{margin-bottom:8px}
  .json-section h3{font-size:12px;color:#7a7368;margin:6px 0 3px;
                   padding-bottom:2px;border-bottom:1px solid #e8e0d3}
```

- [ ] **Step 2.2: Render PLYs 表包裹滚动容器**

将 Render 列的 `<table>` 包裹在 `<div class="render-table-wrap">` 中：

```python
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
```

- [ ] **Step 2.3: JSON 列分区展示（项目 JSONs + 外部 JSONs）**

将 JSON 列的单一表拆为两个子区域。在 `build_fuse_page()` 中修改 JSON rows 生成逻辑，按 `source` 字段分流：

```python
    # ── JSON column: build rows with source flag ──
    proj_json_rows = ""
    ext_json_rows = ""
    for i, j in enumerate(json_files):
        row_html = f"""
        <tr class="row" data-col="json" data-idx="{i}"
            onclick="selectOne(this)">
          <td><input type="radio" name="render-json" value="{i}"
                     onclick="event.stopPropagation()"></td>
          <td colspan="3">{j['name']}</td>
        </tr>"""
        if j.get("source") == "proj":
            proj_json_rows += row_html
        else:
            ext_json_rows += row_html
```

然后在 HTML 中渲染两个区域：

```python
    <!-- JSON column -->
    <div class="col">
      <h2>JSONs（单选 · 全局单选）</h2>
      {"<div class='json-section'><h3>📁 项目 JSON</h3><table><thead><tr><th></th><th>文件名</th></tr></thead><tbody>" + proj_json_rows + "</tbody></table></div>" if proj_json_rows else "<p style='color:#aaa295;font-size:11px;margin:4px 0'>无项目 JSON</p>"}
      {"<div class='json-section'><h3>📂 外部 JSON (jsons_path)</h3><table><thead><tr><th></th><th>文件名</th></tr></thead><tbody>" + ext_json_rows + "</tbody></table></div>" if ext_json_rows else "<p style='color:#aaa295;font-size:11px;margin:4px 0'>jsons_path 无文件</p>"}
      <button class="render-btn" {render_disabled} onclick="doRender()">render 选中</button>
      ...
    </div>
```

> 两个 section 中的 `<input type="radio" name="render-json">` 使用相同的 `name`，浏览器自动保证全局单选。

- [ ] **Step 2.4: 更新 render_disabled 判断**

`render_no_json` 原来只判断 `not json_files`。项目 JSON 加入后这个判断仍然正确（`json_files` 现在包含所有 JSON）。

但顶部的 info 行中 `JSON: {len(json_files)} 个` 统计会自动包含项目 JSON，无需额外修改。

---

### Task 3: JS — 无需改动，验证确认

**Files:**
- Modify: 无（仅验证）

- [ ] **Step 3.1: 验证 `selectOne()` 和单选逻辑**

`selectOne()` (line 449) 通过 `tr.dataset.col` 限定高亮范围，所有 JSON row 的 `data-col="json"` 相同，跨两个 section 的点击行为一致 — — 新选中行高亮，旧行取消。

`getJsonIndex()` (line 463) 通过 `document.querySelector('input[name="render-json"]:checked')` 获取选中值，两个 section 共享同一个 radio name，浏览器原生保证只有一个被选中。

`doRender()` (line 483) 调用 `getJsonIndex()` 获取 `json_index`，POST 到 `/render` 后端用 `state.json_files[json_index]` 取 `path`。因为 `scan_all()` 已将项目 JSON 放在列表前部且带 `path` 字段，`run_render()` 无需任何改动。

**结论：JS 和后端均无需改动。**

---

### Task 4: 验证

- [ ] **Step 4.1: Python 语法 + JS brace 平衡**

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

预期：全部通过。

- [ ] **Step 4.2: 启动服务手动验证**

```powershell
python -m tills.server.fuse_server --config CameraData/05/pipeline.json
```

浏览器 `http://localhost:8081` 检查：
1. Render PLYs 列 — 有滚动条（PLY 多时），最新日期的在最上面
2. JSONs 列 — 分为"项目 JSON"和"外部 JSON"两个区域；`cameras.json` 和 `cameras_align.json`（若存在）出现在项目 JSON 区
3. 单选 — 点击项目 JSON 中的条目，外部 JSON 中的选中自动取消（反之亦然）

- [ ] **Step 4.3: 提交**

```bash
git add tills/server/fuse_server.py
git commit -m "feat(v8): scrollable render PLYs, reverse sort, project JSONs as selectable

- Render PLYs: max-height 320px with overflow scroll, newest first (mtime desc)
- Project dir cameras.json / cameras_align.json shown alongside jsons_path JSONs
- Single radio group across all JSON sources ensures global single-select

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

1. **Spec coverage:**
   - 需求 1 (滚动): Task 2.1 + 2.2 — CSS `render-table-wrap` + HTML wrapper
   - 需求 2 (倒序): Task 1.1 — `sorted(..., key=mtime, reverse=True)`
   - 需求 3 (项目 JSON): Task 1.2 + 2.3 — scan 追加 + HTML 分区渲染

2. **Placeholder scan:** 所有代码块都是完整可执行的，无 TBD/TODO。

3. **Type consistency:**
   - `json_files` 列表元素新增 `source` 字段 (`"proj"` | `"external"`)，在 scan、HTML 渲染、JS 中一致使用
   - `json_index` 仍然是 0-based 索引，指向合并后的 `json_files`，`run_render()` 无需改动
   - `render_plys` 元素结构不变，仅排序改变
