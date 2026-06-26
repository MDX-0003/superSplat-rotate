# Pipeline v2 — 实拍+自定义UE轨迹混合视频管线

## 目标

从「单相机固定圆轨迹」升级为「多相机任意实拍 + 自定义UE序列 + 桥接过渡」的通用混合管线。

## 现状 vs 目标

| | v1（现状） | v2（目标） |
|---|---|---|
| 轨迹来源 | 圆插值（programmatic） | UE Sequence 直接导出 JSON |
| 实拍选取 | 单相机 head/tail | 任意相机 + 任意帧段，显式指定 |
| 虚实衔接 | 硬切（无过渡） | 圆弧桥接插值 |
| 桥接路径 | 无 | 绕场景中心圆弧，维持UE序列方向 |
| GT相机 | 同时进 timeline + GT面板 | GT面板专用（不作为渲染帧） |
| 序列JSON | 同时进 timeline + GT面板 | Timeline专用 |

## 数据结构约定

### GT Camera JSON（来自 COLMAP bin）

```json
[
  { "id": 0, "img_name": "001", "width": 4051, "height": 2192,
    "position": [-3.07, -0.35, -1.91],
    "rotation": [[0.52,...], [-0.14,...], [-0.84,...]],
    "fx": 3439.13, "fy": 3439.13 }
]
```

**用途**：右侧GT相机面板参考 + 桥接段锚点计算。不进timeline。

### Sequence/Trajectory JSON（来自UE或不含intrinsics的来源）

**用途**：timeline 渲染序列。不进GT面板。

### 区分策略：导入弹窗

不依赖 JSON 内容启发式判断。原因：UE 端导出格式会持续演进，不同来源的 JSON 最终可能字段完全一致，无法靠内容区分。

**SuperSplat 修改**：拖入 / 导入 `.json` 文件时，弹出选择对话框：

```
┌──────────────────────────────────────────┐
│  Import Camera Poses                     │
│                                          │
│  How should these camera poses be used?  │
│                                          │
│  ○ Add to GT Cameras                     │
│    (reference only, shown in right panel)│
│                                          │
│  ○ Replace Timeline                      │
│    (animation track, for rendering)      │
│                                          │
│  ○ Both                                  │
│    (current behavior, for compatibility) │
│                                          │
│  [x] Remember my choice for this session │
│                                          │
│              [Cancel]  [Import]          │
└──────────────────────────────────────────┘
```

- **Add to GT Cameras** → 只触发 `camera.addImportedPose`，进右侧GT面板
- **Replace Timeline** → 只触发 `camera.addPose`，进timeline用于渲染
- **Both** → 当前行为，同时写入两边
- "Remember" 勾选后同session内不再弹窗，可随时在设置中重置

---

## 流水线步骤（v2）

### Step 1 — COLMAP bin → cameras.json（不变）

```
python tills/colmap_bin_to_json.py --project <project>
```

输出：63台GT相机的内外参（含intrinsics）。

### Step 2 — 提取指定实拍相机帧段（扩展）

```
python tills/extract_camera.py --project <project> \
    --segments "cam006:1-64,cam032:161-168"
```

输出：`anchor_frames/` 下按来源组织的实拍帧。

**改动**：从单相机提取扩展为多相机多帧段提取。

### Step 3 — 轨迹准备（替换圆插值）

```
# 3a. UE JSON 直接作为序列（或从 glTF 转换）
python tills/gltf_to_cameras_json.py SequenceData/01/CamSqe.gltf \
    -o SequenceData/01/ue_cameras.json

# 3b. 桥接插值 + 合并
python tills/merge_trajectory.py --project <project> \
    --gt-camera 006 --seq SequenceData/01/ue_cameras.json \
    --tail-gt-camera 032 --bridge-fps 60
```

输出：`cameras_align.json`（完整合成轨迹）

#### 桥接插值算法

**输入**：GT锚点位姿 P_gt, 序列起点位姿 P_seq0, 序列终点 P_seq[-1], GT尾锚点 P_tail

**圆心**：63台GT相机 position 的质心（水平面投影）

**桥接段1**（GT锚点 → 序列起点）：
1. 计算 P_gt 和 P_seq0 相对圆心在水平面的极角 θ_gt, θ_seq0
2. 检测UE序列前10帧的平均旋转方向（顺时针/逆时针）
3. 若最短弧方向与序列方向相反，补360°
4. 沿圆弧均匀采样 N 帧（N = ⌈|Δθ| / (360°/300)⌉，或最小30帧）
5. position：圆弧插值 + 高度线性 + 半径线性过渡
6. target：始终指向圆心（或从GT视线平滑过渡到序列起点视线）

**桥接段2**（序列终点 → 尾锚点）：同上逻辑。

**合并顺序**：桥接1 → UE序列 → 桥接2

### Step 4 — SuperSplat 渲染（手动，流程不变）

- 导入 PLY 模型
- Import `cameras_align.json` → 添加到 Timeline（仅timeline，不进GT面板）
- 如需参考GT相机：单独 import `cameras.json` → 添加到 GT Cameras
- Export All → `renders/circle_*.png`

### Step 5 — 混合编排（扩展）

```
python tills/blend_frames.py --project <project> \
    --segments "real:cam006:1-64, render:0:300, real:cam032:161-168"
```

输出：`blended/frame_*.{jpg,png}`

**改动**：从固定的 head/render/tail 结构扩展为任意 ordered segments。

### Step 6 — 编码 MP4（不变）

```
python tills/pngs_to_mp4.py --project <project> --fps 60 --crf 6
```

---

## 文件变更清单

### SuperSplat（TypeScript）

| 文件 | 动作 | 变更 |
|------|------|------|
| `src/file-handler.ts` | 修改 | `loadCameraPoses` 增加 import mode，区分 GT-only / timeline-only / both |
| `src/camera-poses.ts` | 修改 | 允许独立操作 importedPoses 和 track poses |
| `src/ui/view-panel.ts` | 可能修改 | GT面板支持"仅显示原始GT"的过滤 |

### Python 脚本

| 文件 | 动作 | 变更 |
|------|------|------|
| `tills/bridge_interpolate.py` | **新增** | 圆弧桥接位姿生成（核心算法） |
| `tills/merge_trajectory.py` | **新增** | 桥接段+UE序列合并 → cameras_align.json |
| `tills/extract_camera.py` | 修改 | 支持 `--segments` 多段多相机提取 |
| `tills/blend_frames.py` | 修改 | 支持 `--segments` 任意顺序混合 |
| `tills/run_pipeline.py` | 修改 | 新版 CLI 参数，旧参数兼容保留 |

---

## CLI 设计

```bash
python tills/run_pipeline_v2.py --project 02 `
    --gt-camera 001 `
    --tail-gt-camera 031 `
    --ue-seq "E:\Programs\UE Project\ProjectSplat\Content\SFM_camera_json\Sq_SKNJ_H_2_Animation.json" `
    --head-segments "cam001:1-64" `
    --tail-segments "cam031:66-168" `
    --fps 60 --crf 6 `
    --force

```

| 参数 | 含义 |
|------|------|
| `--gt-camera` | GT锚点相机编号（桥接起点） |
| `--tail-gt-camera` | 尾部GT锚点相机编号（可选，不指定则视频以UE序列终点结束） |
| `--ue-seq` | UE导出的序列JSON路径 |
| `--segments` | 混合编排, `camNNN:start-end` 或 `render` |
| `--bridge-min-frames` | 桥接段最少帧数（默认30） |
| `--head` / `--tail` | 兼容旧版参数 |

---

## 已确认决策

1. **SuperSplat JSON 导入**：弹窗选择模式，不依赖内容推断。UE导出格式会演进，靠内容区分不可靠。
2. **圆心计算**：63台GT相机质心，暂不开放手动指定。
3. **桥接段 look-at**：始终指向圆心，不做平滑过渡。
