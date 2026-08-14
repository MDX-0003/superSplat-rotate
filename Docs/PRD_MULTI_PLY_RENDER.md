# PRD: 多 PLY 分段渲染（Multi-PLY Segmented Render）

**版本**: v1  
**日期**: 2026-07-27  
**状态**: 待实现

---

## 1. 概述

### 1.1 动机

当前 fuse_server 的渲染流程是 **一个 PLY + 一个 JSON → 一个 MP4**。对于 fuse 了多个时间戳的 combine PLY（如 `0719-combine-200717-200625-200555.ply`，表示 A→B→C 三步 fuse），用户希望在**同一次渲染中**看到 PLY 随帧切换的效果：

- 帧 0~24：使用 PLY A（`0719-combine-200717.ply`）
- 帧 25~49：使用 PLY B（`0719-combine-200717-200625.ply`）
- 帧 50~179：使用 PLY C（`0719-combine-200717-200625-200555.ply`）

从而在一个视频中直观展示 fuse 每一步带来的质量提升。

### 1.2 核心约束

- **不改动现有 render 按钮/函数**：新功能作为独立按钮，复用现有 `render.video` 内部逻辑，通过可选参数激活
- **PLY 大小可控**：所有 PLY 同时加载到 GPU 显存，PLY 文件本身不大
- **切换点可配置**：用户可以在网页 UI 中设置帧号
- **共用同一个 camera JSON**

---

## 2. 架构设计

### 2.1 方案选择：B2（settings 显式参数）

通过在 `render.video` 的 `settings` 对象中新增可选字段 `plySegments`，将 PLY 分段信息从 Python 传递到 TypeScript 渲染循环。

| | B2（选用） |
|---|---|
| 数据通道 | `settings.plySegments`（已有参数通道，不引入新耦合） |
| 向后兼容 | settings 缺字段 → 完全跳过新逻辑 |
| TypeScript 改动 | `VideoSettings` 类型 + `render.video` handler 内部 |
| Python 改动 | `_shared.py` + `fuse_server.py` |

### 2.2 数据流

```
┌─────────────────────────────────────────────────────────────┐
│ fuse_server UI                                               │
│   [input] 切换帧1: 25    [input] 切换帧2: 50                  │
│   [button] "multi-PLY render"                                │
│       ↓  POST /render_multi { ply_index, json_index,         │
│                               switch_frame_1, switch_frame_2 }│
└─────────────────────────────────────────────────────────────┘
        ↓
┌─────────────────────────────────────────────────────────────┐
│ fuse_server.py: run_render_multi()                           │
│   1. 从选中 PLY 名称自动发现 A/B/C PLY 路径                   │
│   2. 汇总实际可用的 PLY 数量（1~3）                            │
│   3. 依次 upload_ply() 上传每个 PLY（追踪 splat index）        │
│   4. upload_json_file() 上传同一个 JSON                       │
│   5. 构建 settings.plySegments 数组                           │
│   6. 调用 render_video() → page.evaluate() 传入 settings      │
└─────────────────────────────────────────────────────────────┘
        ↓
┌─────────────────────────────────────────────────────────────┐
│ render.ts: render.video handler                              │
│   1. 从 settings 解构 plySegments（可选字段）                  │
│   2. 获取 scene 中所有 Splat，按 upload 顺序对应 A/B/C         │
│   3. 在 prepareFrame() 中：                                   │
│      - 根据 frameIndex 和 plySegments.switchBefore 切换       │
│      - splat.visible = (当前段是否匹配)                        │
│   4. 正常执行渲染循环（sort 只考虑 visible splat）              │
│   5. 渲染完成后恢复所有 splat.visible = true                   │
└─────────────────────────────────────────────────────────────┘
```
如果 PlayCanvas 的 asset loader 异步并行加载 3 个 PLY，它们出现在 scene.elements 的顺序可能不等于 upload 顺序。实现时需要验证，必要时用 splat.asset.name 做排序。
---

## 3. 自动发现算法

### 3.1 PLY 命名约定

combine PLY 文件名格式：`{project}-combine-{ts1}-{ts2}-{ts3}.ply`

- 时间戳顺序 = fuse 执行顺序（ts1 是 base，后续依次 fuse 上去）
- combine 后最多 3 个时间戳

### 3.2 发现逻辑（Python 侧）

```python
def discover_ply_segments(selected_ply_path: Path) -> list[Path]:
    """
    给定 C PLY 路径，反向剥离时间戳找到 B 和 A。
    
    例: 0719-combine-200717-200625-200555.ply
      → [0719-combine-200717.ply,           # A (剥掉后两个)
         0719-combine-200717-200625.ply,    # B (剥掉最后一个)
         0719-combine-200717-200625-200555.ply]  # C (自身)
    
    搜索顺序:
      1. clip_dir = CameraData/{proj}-clip/
      2. proj_dir = CameraData/{proj}/
    
    未找到则跳过该段，最终至少保留 C 自身。
    """
```

### 3.3 段数决策

| 实际找到的 PLY 数 | 行为 |
|---|---|
| 1（仅 C 自身） | 单 PLY 渲染，不传 plySegments，行为与现有 render 按钮完全相同 |
| 2（如 B + C） | 使用 switch_frame_1（第一个切换帧），忽略 switch_frame_2 |
| 3（A + B + C） | 使用 switch_frame_1 和 switch_frame_2 |

### 3.4 切换帧语义

- **严格使用 `<`（小于号）**
- `frame < switch_frame_1` → 最早期 PLY（A）
- `switch_frame_1 <= frame < switch_frame_2` → 中间 PLY（B）
- `frame >= switch_frame_2` → 最终 PLY（C）

默认值：`switch_frame_1 = 25`，`switch_frame_2 = 50`

---

## 4. 文件级改动清单

### 4.1 `src/render.ts` — TypeScript 核心

#### 4.1.1 `VideoSettings` 类型扩展

```typescript
// 在 line 50 之前新增:
type PlySegment = {
    switchBefore: number;  // 帧号 < 此值之前使用上一段 PLY
};
```

```typescript
// VideoSettings 新增可选字段:
type VideoSettings = {
    // ... 现有字段不变 ...
    plySegments?: PlySegment[];  // 新增，可选。长度 1 或 2。
                                 // 长度为 0/undefined → 不触发切换逻辑
};
```

**说明**：`plySegments` 数组的语义是"切换点列表"，不直接包含 PLY 路径（TypeScript 侧不需要知道路径，只通过 upload 顺序对应 Splat index）。

- `plySegments = [{switchBefore: 25}, {switchBefore: 50}]` 表示 3 段渲染（A/B/C）
- `plySegments = [{switchBefore: 25}]` 表示 2 段渲染（A/B）

#### 4.1.2 `render.video` handler 改动

在 `renderImpl` 函数内部（约 line 423 之后），新增三个逻辑点：

**A. 获取所有 splat 引用（在 frame loop 之前）**

```typescript
const { plySegments } = videoSettings;
const segmentSplats: Splat[] = [];
if (plySegments && plySegments.length > 0) {
    // Upload 顺序 = splat index，scene.elements 按 add 顺序排列
    segmentSplats.push(...(scene.getElementsByType(ElementType.splat) as Splat[])
        .filter(s => s.visible && s.numSplats > 0));
}
```

**B. `prepareFrame()` 内部：切换 visible（在 line 501 `events.fire('timeline.time', frameTime)` 之后）**

```typescript
// 在 prepareFrame 内，timeline.time fire 之后、sort 之前:
if (plySegments && segmentSplats.length > 1) {
    const frameIndex = Math.floor(frameTime);
    // 确定当前帧属于哪个段
    let activeSegment = segmentSplats.length - 1;  // 默认最后一段
    for (let s = 0; s < plySegments.length; s++) {
        if (frameIndex < plySegments[s].switchBefore) {
            activeSegment = s;
            break;
        }
    }
    // 切换可见性
    for (let i = 0; i < segmentSplats.length; i++) {
        segmentSplats[i].visible = (i === activeSegment);
    }
}
```

这段逻辑放在 `events.fire('timeline.time', frameTime)` 之后、`scene.camera.onUpdate(0)` 之前，确保：
- 相机位置已更新
- splat 可见性在 sort 之前已确定
- sort 只排序 visible 的 splat

**C. 渲染完成后恢复 visible（在 finally 块中）**

```typescript
// 在 encoder.close() / cleanup 之后:
if (plySegments && segmentSplats.length > 1) {
    segmentSplats.forEach(s => { s.visible = true; });
}
```

---

### 4.2 `tills/_shared.py` — Playwright 自动化

#### 4.2.1 `upload_ply()` 扩展

新增返回值，返回上传后 splat 的 scene index（用于后续对应）。也可以通过在每次上传后查询 `scene.splats.length` 来追踪。

**改动点**：新增可选参数 `reset_done_flag=True`，允许调用方在连续上传时控制。

```python
async def upload_ply(page, ply_path: Path, done_flag: str = "__v5_importDone"):
    """
    上传单个 PLY 文件到 SuperSplat。
    
    返回值: bool — 是否成功
    """
    # ... 现有逻辑，done_flag 可配置以支持多次上传 ...
```

#### 4.2.2 `render_video()` 扩展

新增可选参数 `ply_segments: list[dict] | None = None`：

```python
async def render_video(
    page, total_frames, renders_dir, expected_filename, fps,
    start_frame: int = 0,
    ply_segments: list[dict] | None = None,
) -> bool:
```

在构建 `settings` 时：

```python
settings = {
    "startFrame": start_frame,
    "endFrame": total_frames - 1,
    # ... 现有字段 ...
}
if ply_segments:
    settings["plySegments"] = ply_segments
```

---

### 4.3 `tills/server/fuse_server.py` — 服务端 & UI

#### 4.3.1 新增函数：`discover_ply_segments()`

```python
def discover_ply_segments(ply_path: Path, proj_dir: Path, clip_dir: Path) -> list[Path]:
    """
    从 combine PLY 文件名自动发现中间 PLY。
    
    返回按 fuse 顺序排列的 PLY 路径列表（A → B → C），
    长度为 1~3。
    """
```

#### 4.3.2 新增函数：`run_render_multi()`

类似 `run_render()`，但：
- 接收 `switch_frame_1` 和 `switch_frame_2` 参数
- 调用 `discover_ply_segments()` 获取实际 PLY 列表
- 循环调用 `upload_ply()` 上传所有 PLY
- 构建 `ply_segments` 数组传入 `render_video()`

```python
def run_render_multi(
    state: FuseState, cfg: dict,
    ply_index: int, json_index: int,
    switch_frame_1: int, switch_frame_2: int,
    broadcaster: SSEBroadcaster, logger: FileLogger,
):
```

#### 4.3.3 新增路由：`POST /render_multi`

```python
def _render_multi(handler, body):
    # 解析 ply_index, json_index, switch_frame_1, switch_frame_2
    # 启动 run_render_multi 线程
```

#### 4.3.4 UI 改动

在 Render 列底部，现有 render 按钮下方，新增：

```html
<!-- 多 PLY 分段渲染 -->
<div style="margin-top:12px;padding-top:8px;border-top:1px dashed #d9cfb8">
  <h3 style="font-size:13px;color:#5b7c5a;margin-bottom:4px">
    🔀 多 PLY 分段渲染
  </h3>
  <p style="font-size:11px;color:#7a7368;margin-bottom:6px">
    自动发现 combine PLY 的中间产物，按帧切换可见性。
  </p>
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px;flex-wrap:wrap">
    <label style="font-size:12px">切换帧1:</label>
    <input type="number" id="switch-frame-1" value="25" min="1"
           style="width:60px;padding:2px 4px;font-size:12px;border:1px solid #d9cfb8;border-radius:3px">
    <label style="font-size:12px">切换帧2:</label>
    <input type="number" id="switch-frame-2" value="50" min="1"
           style="width:60px;padding:2px 4px;font-size:12px;border:1px solid #d9cfb8;border-radius:3px">
  </div>
  <p style="font-size:10px;color:#aaa295;margin-bottom:4px">
    帧 &lt; 切换帧1 → A PLY | 切换帧1 ≤ 帧 &lt; 切换帧2 → B PLY | 帧 ≥ 切换帧2 → C PLY
  </p>
  <button class="render-btn" id="btn-render-multi" {render_disabled}
          onclick="doRenderMulti()"
          style="background:#b87333">
    🔀 multi-PLY render
  </button>
</div>
```

对应的 JavaScript：

```javascript
async function doRenderMulti() {
    let ply_idx = getRenderPlyIndex();
    let json_idx = getJsonIndex();
    if (ply_idx === null) { alert('请选择一个 Render PLY'); return; }
    if (json_idx === null) { alert('请选择一个 JSON 文件'); return; }
    let sf1 = parseInt(document.getElementById('switch-frame-1').value) || 25;
    let sf2 = parseInt(document.getElementById('switch-frame-2').value) || 50;
    if (sf1 >= sf2) { alert('切换帧1 必须小于 切换帧2'); return; }
    let r = await fetch('/render_multi', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
            ply_index: ply_idx, json_index: json_idx,
            switch_frame_1: sf1, switch_frame_2: sf2
        })
    });
    let d = await r.json();
    if (d.status === 'ok') location.reload();
    else alert(d.message || JSON.stringify(d));
}
```

---

## 5. 边界情况

| 场景 | 处理 |
|------|------|
| combine PLY 只有 1 个时间戳（如 `0719-combine-124508.ply`） | `discover_ply_segments()` 返回长度为 1，不传 `plySegments`，等效单 PLY 渲染 |
| combine PLY 有 2 个时间戳 | 返回 2 个 PLY 路径，只用 `switch_frame_1`，忽略 `switch_frame_2` |
| 中间 PLY 在 clip/ 和 proj/ 都不存在 | 跳过该段，可用 PLY 减少。如果只剩 1 个 → 退化为单 PLY 渲染 |
| 中间 PLY 在 project dir 存在但不在 clip/ | 使用 project dir 的版本（未经 clip 处理） |
| 用户输入的切换帧超出总帧数 | `switch_frame >= total_frames` → 该切换点不生效（所有帧使用更早的 PLY） |
| 切换帧1 ≥ 切换帧2 | 前端校验，弹 alert 阻止提交 |
| 现有 render 按钮行为 | 完全不受影响，`plySegments` 不传入，不触发新逻辑 |
| PLY 同名但不在 combine 目录 | 仅对选中的 combine PLY 触发自动发现；普通 PLY 不触发 |

---

## 6. 实现步骤

| 步骤 | 文件 | 内容 |
|------|------|------|
| 1 | `src/render.ts` | `VideoSettings` 加 `plySegments?: PlySegment[]`；`render.video` handler 加切换逻辑 |
| 2 | `tills/_shared.py` | `render_video()` 加 `ply_segments` 参数；`upload_ply()` 支持多次调用 |
| 3 | `tills/server/fuse_server.py` | 新增 `discover_ply_segments()` |
| 4 | `tills/server/fuse_server.py` | 新增 `run_render_multi()` |
| 5 | `tills/server/fuse_server.py` | 新增 `POST /render_multi` 路由 |
| 6 | `tills/server/fuse_server.py` | UI：切换帧输入框 + multi-PLY render 按钮 + JS |
| 7 | — | `npm run build` 编译 SuperSplat |
| 8 | — | 端到端测试：选 combine PLY + JSON → 渲染 → 检查视频中 PLY 切换 |

---

## 7. 未决问题

1. **显存压力**：3 个 PLY 同时驻留 GPU。当前假设 PLY 不超过 ~200MB 一个，3 个总计 < 600MB。如果未来 PLY 增大，可降级为方案 A（分段渲染 + ffmpeg concat）。

2. **splat upload 顺序可靠性**：当前方案依赖 `scene.elements` 中 splat 的排列顺序与 upload 顺序一致。如果 PlayCanvas 的 asset loader 是异步并行的，需要额外的排序逻辑（如通过 `splat.asset.name` 匹配）。

3. **进度条**：多 PLY 渲染的 progress 报告是否需要分段展示（如 "A 段渲染中..."）还是沿用现有百分比。

---

*关联文档: [[V5_V6_USAGE.md]] [[v8-daemon-design.md]] [[V5_AUTOMATION.md]]*
