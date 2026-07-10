# v8 Preset Editor — Development Plan

> **For agentic workers:** Plan uses checkbox (`- [ ]`) syntax. Each Phase produces working, testable software. TDD for pure functions; manual verification for file I/O and HTML rendering.

**Goal:** Add a `/presets` page to fuse_server for viewing, editing, creating, and deleting presets in `tills_ply/presets.json`. Add a preset selector dropdown to the main page so the active preset can be switched without restarting.

**Architecture:** New route `/presets` handles GET (render editor page) and POST (save/delete/create actions). The main page dropdown is populated at render time by reading `presets.json`. All preset modifications write directly to `presets.json` via atomic write-tmp-rename.

**Tech Stack:** Python 3 stdlib (`json`, `pathlib`, `shutil`), existing `_server.py` SSEHandler.

**Design decisions recorded in:** grill-with-docs session in this conversation thread.

---

## File Structure

```
tills/server/fuse_server.py       # Phase 2+3: +preset routes, +dropdown HTML
tills/server/_server.py           # No changes needed
tills_ply/presets.json            # Phase 1: target of CRUD operations
CameraData/_template/presets.json # Read-only template for new presets
```

No new files. All preset logic lives inside `fuse_server.py`.

---

## Phase 1: Preset CRUD Backend

### Task 1.1: Preset file path + helper functions

**Files:**
- Modify: `tills/server/fuse_server.py`

**Purpose:** Add constants for preset file paths and pure helper functions for loading/saving/writing presets.

- [ ] **Step 1: Define constants and helpers**

Add near the top of `fuse_server.py`, after `SUPERSPLAT_URL`:

```python
PRESETS_FILE = _project_root / "tills_ply" / "presets.json"
PRESET_TEMPLATE_FILE = _project_root / "CameraData" / "_template" / "presets.json"


def _load_all_presets() -> dict:
    """Return the full presets dict {name: {...}} from presets.json."""
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
```

- [ ] **Step 2: Manual verification**

```bash
python -c "
from tills.server.fuse_server import _load_all_presets, _load_template_preset
presets = _load_all_presets()
print(f'Loaded {len(presets)} presets: {list(presets.keys())[:3]}')
tmpl = _load_template_preset()
print(f'Template keys: {list(tmpl.keys())}')
"
```

Expected: prints preset names and template keys.

- [ ] **Step 3: Commit**

```bash
git add tills/server/fuse_server.py
git commit -m "feat(v8): add preset file path constants and load/save helpers"
```

### Task 1.2: Preset CRUD route handlers

**Files:**
- Modify: `tills/server/fuse_server.py`

**Purpose:** Add `GET /presets` (render editor page stub), `POST /presets/save`, `POST /presets/delete`, `POST /presets/create` endpoints.

- [ ] **Step 1: Implement route handlers**

Add inside `_make_fuse_routes()`, alongside the existing `_root`, `_fuse`, `_render` handlers:

```python
def _presets_page(handler):
    """GET /presets — render preset editor page."""
    return _build_presets_page(), "text/html; charset=utf-8"


def _presets_save(handler, body):
    """POST /presets/save — save one preset's parameters."""
    if isinstance(body, str):
        body = json.loads(body)
    name = body.get("name", "")
    params = body.get("params", {})
    presets = _load_all_presets()
    if name not in presets:
        return json.dumps({"status": "error", "message": f"preset not found: {name}"}), \
               "application/json; charset=utf-8"
    # Merge: only update fields that exist in the incoming params
    for section in ("max_index", "fuse", "clip", "interpolate"):
        if section in params:
            if section == "max_index":
                presets[name]["max_index"] = params["max_index"]
            else:
                presets[name].setdefault(section, {})
                presets[name][section].update(params[section])
    _save_all_presets(presets)
    logger.write("presets", f"saved preset '{name}'")
    return json.dumps({"status": "ok", "message": f"preset '{name}' saved"}), \
           "application/json; charset=utf-8"


def _presets_delete(handler, body):
    """POST /presets/delete — delete a preset by name."""
    if isinstance(body, str):
        body = json.loads(body)
    name = body.get("name", "")
    presets = _load_all_presets()
    if name not in presets:
        return json.dumps({"status": "error", "message": f"preset not found: {name}"}), \
               "application/json; charset=utf-8"
    del presets[name]
    _save_all_presets(presets)
    logger.write("presets", f"deleted preset '{name}'")
    return json.dumps({"status": "ok", "message": f"preset '{name}' deleted"}), \
           "application/json; charset=utf-8"


def _presets_create(handler, body):
    """POST /presets/create — create a new preset from template."""
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
    # Deep-copy template
    import copy
    template = _load_template_preset()
    if not template:
        return json.dumps({"status": "error",
                           "message": "template preset not found in _template/presets.json"}), \
               "application/json; charset=utf-8"
    presets[name] = copy.deepcopy(template)
    _save_all_presets(presets)
    logger.write("presets", f"created preset '{name}' from template")
    return json.dumps({"status": "ok", "message": f"preset '{name}' created"}), \
           "application/json; charset=utf-8"
```

Add these to the returned dict:

```python
return {
    "/": _root, "/fuse": _fuse, "/render": _render,
    "/presets": _presets_page,
    "/presets/save": _presets_save,
    "/presets/delete": _presets_delete,
    "/presets/create": _presets_create,
}
```

- [ ] **Step 2: Manual verification**

Start fuse_server, then:
```bash
# List presets (renders stub page)
curl http://localhost:8081/presets

# Create a test preset
curl -X POST http://localhost:8081/presets/create \
  -H "Content-Type: application/json" \
  -d '{"name":"_test_from_curl"}'

# Verify it appears in presets.json
python -c "import json; d=json.load(open('tills_ply/presets.json')); print(list(d['presets'].keys()))"

# Delete it
curl -X POST http://localhost:8081/presets/delete \
  -H "Content-Type: application/json" \
  -d '{"name":"_test_from_curl"}'
```

Expected: create adds to presets.json, delete removes it.

- [ ] **Step 3: Commit**

```bash
git add tills/server/fuse_server.py
git commit -m "feat(v8): add preset CRUD API — save, delete, create endpoints"
```

---

## Phase 2: Preset Editor Page (`/presets`)

### Task 2.1: `_build_presets_page()` — HTML with parameter editor

**Files:**
- Modify: `tills/server/fuse_server.py`

**Purpose:** Replace the stub `_build_presets_page()` with a full editor page. The page:
- Shows a dropdown to select which preset to edit
- Parameter groups (fuse, clip, interpolate) appear only when a preset is selected
- Each field is an `<input>` bound to its parameter
- `[保存]` button POSTs the entire preset
- `[删除]` button with confirm
- `[+ 新建 Preset]` button

- [ ] **Step 1: Implement `_build_presets_page()`**

Add before `_make_fuse_routes()`:

```python
def _build_presets_page() -> str:
    """Render the preset editor page."""
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
    .nav a:hover{{text-decoration:underline}}
    .toolbar{{display:flex;gap:10px;align-items:center;margin-bottom:15px;flex-wrap:wrap}}
    select,input[type="text"]{{padding:4px 8px;border:1px solid #d9cfb8;
           border-radius:3px;font-size:13px;background:#fffdf7}}
    button{{background:#6b8e6b;color:#fff;border:none;padding:5px 12px;
            cursor:pointer;border-radius:3px;font-size:13px}}
    button.danger{{background:#c0392b}}
    button:disabled{{opacity:0.4;cursor:default}}
    .section{{background:#fffdf7;border:1px solid #d9cfb8;border-radius:6px;
              padding:12px 16px;margin-bottom:12px}}
    .section h2{{color:#5b7c5a;font-size:15px;margin-bottom:10px;
                 padding-bottom:4px;border-bottom:2px solid #d9cfb8}}
    .field{{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}}
    .field label{{font-size:13px;color:#5b5a4e;min-width:160px}}
    .field input[type="text"],.field input[type="number"]{{width:100px}}
    .field input[type="checkbox"]{{width:auto;margin-right:4px}}
    .field .hint{{font-size:11px;color:#aaa}}
    #editor{{display:none}}
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

  <div id="editor">
    <!-- fuse section -->
    <div class="section">
      <h2>fuse 参数</h2>
      <div class="field"><label>max_index</label>
        <input type="number" id="f-max_index" step="1"></div>
      <div class="field"><label>radius_scale</label>
        <input type="number" id="f-radius_scale" step="0.01"></div>
      <div class="field"><label>height_up</label>
        <input type="number" id="f-height_up" step="0.1"></div>
      <div class="field"><label>height_down</label>
        <input type="number" id="f-height_down" step="0.1"></div>
      <div class="field"><label>bias</label>
        <input type="checkbox" id="f-bias" onchange="toggleBias()"></div>
      <div class="field"><label>bias_margin</label>
        <input type="number" id="f-bias_margin" step="0.01"></div>
      <div class="field"><label>bias_radius_percentile</label>
        <input type="number" id="f-bias_radius_percentile" step="1"></div>
    </div>
    <!-- clip section -->
    <div class="section">
      <h2>clip 参数</h2>
      <div class="field"><label>clip_percent</label>
        <input type="number" id="c-clip_percent" step="0.01"></div>
      <div class="field"><label>denoise</label>
        <input type="checkbox" id="c-denoise"></div>
      <div class="field"><label>denoise_method</label>
        <input type="text" id="c-denoise_method"></div>
      <div class="field"><label>denoise_grid_cell</label>
        <input type="number" id="c-denoise_grid_cell" step="0.01"></div>
      <div class="field"><label>denoise_min_points</label>
        <input type="number" id="c-denoise_min_points" step="1"></div>
      <div class="field"><label>denoise_voxel_size</label>
        <input type="number" id="c-denoise_voxel_size" step="0.01"></div>
      <div class="field"><label>height_up</label>
        <input type="number" id="c-height_up" step="0.1"></div>
      <div class="field"><label>height_down</label>
        <input type="number" id="c-height_down" step="0.1"></div>
      <div class="field"><label>radius_scale</label>
        <input type="number" id="c-radius_scale" step="0.01"></div>
      <div class="field"><label>ring_delete</label>
        <input type="checkbox" id="c-ring_delete"></div>
      <div class="field"><label>ring_outer_delta</label>
        <input type="number" id="c-ring_outer_delta" step="0.01"></div>
      <div class="field"><label>ring_inner_delta</label>
        <input type="number" id="c-ring_inner_delta" step="0.01"></div>
      <div class="field"><label>ring_height_up</label>
        <input type="number" id="c-ring_height_up" step="0.1"></div>
      <div class="field"><label>ring_height_down</label>
        <input type="number" id="c-ring_height_down" step="0.1"></div>
    </div>
    <!-- interpolate section -->
    <div class="section">
      <h2>interpolate 参数</h2>
      <div class="field"><label>total</label>
        <input type="number" id="i-total" step="1"></div>
      <div class="field"><label>anchor_camera</label>
        <input type="text" id="i-anchor_camera"></div>
      <div class="field"><label>radius_scale</label>
        <input type="number" id="i-radius_scale" step="0.01"></div>
    </div>
    <div style="display:flex;gap:10px;margin-top:10px">
      <button onclick="doSave()">保存</button>
      <button class="danger" onclick="doDelete()">删除此 Preset</button>
    </div>
  </div>

  <script>
    let currentName = '';
    let presetData = null;

    const FIELD_MAP = {{
      'max_index': {{id:'f-max_index', section:'max_index'}},
      'radius_scale': {{id:'f-radius_scale', section:'fuse'}},
      'height_up': {{id:'f-height_up', section:'fuse'}},
      'height_down': {{id:'f-height_down', section:'fuse'}},
      'bias': {{id:'f-bias', section:'fuse'}},
      'bias_margin': {{id:'f-bias_margin', section:'fuse'}},
      'bias_radius_percentile': {{id:'f-bias_radius_percentile', section:'fuse'}},
      'clip_percent': {{id:'c-clip_percent', section:'clip'}},
      'denoise': {{id:'c-denoise', section:'clip'}},
      'denoise_method': {{id:'c-denoise_method', section:'clip'}},
      'denoise_grid_cell': {{id:'c-denoise_grid_cell', section:'clip'}},
      'denoise_min_points': {{id:'c-denoise_min_points', section:'clip'}},
      'denoise_voxel_size': {{id:'c-denoise_voxel_size', section:'clip'}},
      'height_up_c': {{id:'c-height_up', section:'clip'}},
      'height_down_c': {{id:'c-height_down', section:'clip'}},
      'radius_scale_c': {{id:'c-radius_scale', section:'clip'}},
      'ring_delete': {{id:'c-ring_delete', section:'clip'}},
      'ring_outer_delta': {{id:'c-ring_outer_delta', section:'clip'}},
      'ring_inner_delta': {{id:'c-ring_inner_delta', section:'clip'}},
      'ring_height_up': {{id:'c-ring_height_up', section:'clip'}},
      'ring_height_down': {{id:'c-ring_height_down', section:'clip'}},
      'total': {{id:'i-total', section:'interpolate'}},
      'anchor_camera': {{id:'i-anchor_camera', section:'interpolate'}},
      'radius_scale_i': {{id:'i-radius_scale', section:'interpolate'}},
    }};

    async function loadPreset() {{
      currentName = document.getElementById('preset-select').value;
      if (!currentName) {{
        document.getElementById('editor').style.display = 'none';
        return;
      }}
      // Fetch presets.json via API
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
      toggleBias();
    }}

    function setVal(id, val, isCheckbox, isText) {{
      let el = document.getElementById(id);
      if (el === null) return;
      if (isCheckbox) el.checked = !!val;
      else if (val !== undefined && val !== null) el.value = val;
    }}

    function toggleBias() {{
      let bias = document.getElementById('f-bias').checked;
      document.getElementById('f-bias_margin').disabled = !bias;
      document.getElementById('f-bias_radius_percentile').disabled = !bias;
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

    function floatVal(id) {{ let v = parseFloat(document.getElementById(id).value); return isNaN(v) ? null : v; }}
    function intVal(id) {{ let v = parseInt(document.getElementById(id).value); return isNaN(v) ? null : v; }}

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


def _build_presets_data(handler):
    """GET /presets/data — return full presets.json as JSON for the editor JS."""
    presets = _load_all_presets()
    return json.dumps({"presets": presets}, ensure_ascii=False), \
           "application/json; charset=utf-8"
```

Add `/presets/data` to the routes dict.

- [ ] **Step 2: Manual verification**

Open `http://localhost:8081/presets` in browser.
- Verify: dropdown shows preset names
- Select a preset → parameter fields populate
- Modify a value → click [保存] → refresh → verify value persisted in presets.json

- [ ] **Step 3: Commit**

```bash
git add tills/server/fuse_server.py
git commit -m "feat(v8): add /presets editor page with fuse/clip/interpolate parameter editing"
```

---

## Phase 3: Main Page Preset Dropdown

### Task 3.1: Dropdown in main page + dynamic preset loading in run_fuse_clip

**Files:**
- Modify: `tills/server/fuse_server.py`

**Purpose:** Add a preset selector `<select>` near the fuse button. The selected preset name is sent with the fuse POST request. `run_fuse_clip` reads the preset from the request body instead of from the captured closure.

- [ ] **Step 1: Add dropdown HTML to `build_fuse_page()`**

In the fuse column, above the `[fuse + clip]` button:

```python
# In build_fuse_page(), after fuse_rows generation, add:
presets = _load_all_presets()
preset_options = ""
default_preset = state.preset_name
for n in sorted(presets.keys()):
    sel = ' selected' if n == default_preset else ''
    preset_options += f'<option value="{n}"{sel}>{n}</option>'

# Then in the HTML, after the table and before the fuse button:
f"""
  <label style="font-size:12px;color:#5b5a4e;margin-top:6px;display:block">
    Preset:
    <select id="fuse-preset" style="margin-left:4px;padding:2px 6px;
           border:1px solid #d9cfb8;border-radius:3px;font-size:12px;
           background:#fffdf7">
      {preset_options}
    </select>
  </label>
  <button class="fuse" {fuse_disabled} onclick="doFuse()">fuse + clip 选中</button>
"""
```

- [ ] **Step 2: Update `doFuse()` JS to include preset name**

```javascript
async function doFuse() {{
  if (!fuseOrder.length) {{ alert('请至少选择一个 PLY'); return; }}
  let ply_indices = fuseOrder.map(f => f.idx);
  let preset = document.getElementById('fuse-preset').value;
  let r = await fetch('/fuse', {{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{ply_indices: ply_indices, preset_name: preset}})
  }});
  ...
}}
```

- [ ] **Step 3: Update `_fuse` route handler to pass preset to `run_fuse_clip`**

```python
def _fuse(handler, body):
    ...
    preset_name = body.get("preset_name", state.preset_name)
    preset = _load_all_presets().get(preset_name)
    if not preset:
        return json.dumps({"status": "error",
                           "message": f"preset not found: {preset_name}"}), ...
    with state._lock:
        state.active_tasks.add("fuse")
    t = threading.Thread(
        target=run_fuse_clip,
        args=(state, cfg, preset, ply_indices, force, broadcaster, logger),
        daemon=True,
    )
    ...
```

- [ ] **Step 4: Remove `preset` from `_make_fuse_routes` closure**

Since the preset is now loaded per-request inside `_fuse`, the `preset` parameter of `_make_fuse_routes` is no longer needed for fuse operations. Remove it from the function signature, and update the `main()` call site.

- [ ] **Step 5: Manual verification**

- Open main page → verify dropdown shows all presets
- Select a different preset → [fuse + clip] → check logs that the correct preset params were used
- Change parameters in /presets, save → reload main page → verify new values appear

- [ ] **Step 6: Commit**

```bash
git add tills/server/fuse_server.py
git commit -m "feat(v8): add preset selector dropdown to main fuse page"
```

---

## Task Summary

| Phase | Task | Files | Est. Time |
|:---:|------|------|:---:|
| 1.1 | Preset helpers + file I/O | `fuse_server.py` | 20 min |
| 1.2 | CRUD API endpoints | `fuse_server.py` | 30 min |
| 2.1 | `/presets` editor page | `fuse_server.py` | 60 min |
| 3.1 | Main page dropdown + dynamic load | `fuse_server.py` | 30 min |

**Total estimate:** ~2.5 hours. All changes in `fuse_server.py` only.

---

*Design decisions from grill-with-docs. Template-based creation from `CameraData/_template/presets.json`.*
