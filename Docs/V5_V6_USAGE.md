# SuperSplat v5 / v6 管线使用文档

> 最后更新: 2026-06-27

## 1. 两个管线的定位

| | v5 | v6 |
|------|-----|------|
| 适用场景 | 单帧 3DGS 训练 + 实拍混合渲染 | 多帧批量训练 + 交互式 fuse + 纯渲染 |
| 训练素材 | 从 `raw_images/` 大量 JPG 中提取指定帧范围 | `raw_images/` 下每帧一个子文件夹，直接可用 |
| 输出 | 实拍 + 渲染混合 MP4 (`output/output.mp4`) | 纯 SuperSplat 渲染 MP4 (`renders/<project>.mp4`) |
| 视频拼接 | ✅ real → render → real concat | ❌ 不拼接 |
| PLY 处理 | 仅 clip（参数来自 preset） | fuse + clip（交互式选择 fuse indices） |

## 2. 前置条件

两个管线都依赖：

- **SuperSplat dev server**：`npm run serve` 在项目根目录运行
- **Playwright**：用于浏览器自动化（`pip install playwright`）
- **presets.json**：PLY 处理参数预设，位于 `tills_ply/presets.json`
- **SuperSplat 源码修改**：`src/file-handler.ts:163`，`cameraImportSessionMode` 默认改为 `'both'`（消除 JSON 导入弹窗）

v5 额外依赖：
- **ffmpeg**：用于实拍 MP4 编码和 TS concat

v6 额外依赖：
- **LiteGSWin**：3DGS 训练环境（需 `uv run` 可用）
- **fuse_ply.py**：多 PLY 融合

## 3. pipeline.json 字段

### v5

```json
{
  "project": "01",
  "preset": "01-0625测试-3人",
  "jsons_path": "E:/path/to/camera/jsons",
  "litegs_path": "E:/path/to/LiteGSWin",
  "output": {
    "fps": 25,
    "crf": 6,
    "resolution": "3840x2160",
    "source": "raw_images",
    "segments": [
      { "type": "real",   "start": 0,   "end": 74 },
      { "type": "render", "replace_frames": 114 },
      { "type": "real",   "start": 188, "end": 262 }
    ]
  }
}
```

| 字段 | 必填 | 说明 |
|------|:--:|------|
| `project` | ✅ | 项目名，对应 `CameraData/<name>/` |
| `preset` | ✅ | 指向 `presets.json` 中的 preset。v5 从中读 `clip` 参数和 `max_index` |
| `jsons_path` | ❌ | 相机 JSON 文件夹（绝对路径）。不存在时跳过自动导入 |
| `litegs_path` | ✅ | LiteGSWin 仓库路径。训练步骤必需 |
| `output.fps` | ✅ | 视频帧率 |
| `output.crf` | ✅ | H.264 CRF 质量参数（越小质量越高） |
| `output.resolution` | ✅ | 视频分辨率（如 `3840x2160`） |
| `output.source` | ✅ | 实拍图片源，相对于 `CameraData/<project>/` 的子目录名 |
| `output.segments` | ✅ | 时间轴段落定义，见下 |

**segments 语法**：

```json
{ "type": "real",   "start": 0,   "end": 74 }
{ "type": "render", "replace_frames": 114 }
```

- `real`：实拍帧范围（0-indexed，闭区间）。`start`/`end` 对应源图片的文件名序号
- `render`：渲染段。不再需要手写 `seq` 字段——从上一个 `real` 的 `end+1` 自动推导 MP4 文件名（如 `74+1=75` → `renders/seq_f075.mp4`）
- `replace_frames`：文档性字段，记录此渲染段替换了多少帧实拍（仅用于训练素材提取）

### v6

```json
{
  "project": "02",
  "preset": "02-0618测试-2人",
  "jsons_path": "E:/path/to/camera/jsons",
  "litegs_path": "E:/path/to/LiteGSWin",
  "fps": 25,
  "resolution": "3840x2160"
}
```

| 字段 | 必填 | 说明 |
|------|:--:|------|
| `project` | ✅ | 项目名，对应 `CameraData/<name>/` |
| `preset` | ✅ | 指向 `presets.json` 中的 preset。v6 从中读 `fuse`/`clip` 参数和 `max_index`。**`preset["path"]` 被 v6 忽略**（始终用 `cfg["project"]` 推导路径） |
| `jsons_path` | ❌ | 相机 JSON 文件夹（绝对路径） |
| `litegs_path` | ✅ | LiteGSWin 仓库路径 |
| `fps` | ✅ | 渲染帧率 |
| `resolution` | ✅ | 渲染分辨率（如 `3840x2160`） |

### v5 vs v6 关键差异

| | v5 | v6 |
|------|-----|------|
| `output` 字段 | 必填（含 segments/fps/crf/resolution/source） | 不需要 |
| fps/resolution | 在 `output` 内 | 顶级字段 |
| `segments` | 必填（定义实拍+渲染段落） | 不需要（不拼接） |
| preset 的 path | 不用（路径由 `cfg["project"]` + `path_override` 控制） | 同左 |
| preset 的 fuse 参数 | 不读（v5 只 clip） | 读取（用于交互式 fuse） |

## 4. 默认流程（不指定 `--steps`）

运行 `python tills/run_pipeline_v5.py --config CameraData/<project>/pipeline.json` 时，
按顺序执行以下阶段。每一步都是**幂等**的——中间产物已存在时自动跳过。

### 4.1 v5 流程

```
train → clip → render

┌─ train ─────────────────────────────────────────────────────┐
│ T1  从 raw_images 提取训练素材 → Train_imgs/<date>/        │
│ T2  复制到 LiteGSWin/data/<MMDD>/<date>/                   │
│ T3  uv run python batch_run.py --sub_dir <MMDD>            │
│     (已有结果 PLY 的帧自动跳过)                              │
│ T4  复制 results/<MMDD>/<MMDD>-<HHMMSS>.ply → CameraData/  │
│ T5  复制 cameras.json → CameraData/                         │
│                                                             │
│  幂等规则: T2 帧目录存在→跳过 / T3 PLY存在→跳过 / T5 存在→跳过 │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
┌─ clip ──────────────────────────────────────────────────────┐
│  对 CameraData/<project>/ 下所有 PLY 跑 clip_ply            │
│  输出 → <project>-clip/*.ply                                │
│                                                             │
│  幂等规则: --force 或 <project>-clip/ 不存在时才跑           │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
┌─ render ────────────────────────────────────────────────────┐
│  [交互] 列出 <project>-clip/*.ply → 输入 idx 选择           │
│  [交互] 列出 jsons_path/*.json → 输入 idx (如有)            │
│  [自动] Playwright 启动 Chrome → 上传 PLY + JSON            │
│  [自动] 渲染视频 (OPFS 流式) → renders/seq_f075.mp4        │
│  [自动] 提取实拍帧 → 编码 MP4 → TS concat → output.mp4     │
└─────────────────────────────────────────────────────────────┘
```

**初次使用**需要预先完成：
1. `npm run build` + `npm run serve`（启动 SuperSplat）
2. 确保 `LiteGSWin/data/calibration/<MMDD>/` 下有相机标定数据
3. 确保 `CameraData/<project>/raw_images/` 下有实拍帧

### 4.2 v6 流程

```
train → fuse → render

┌─ train ─────────────────────────────────────────────────────┐
│ T1  扫描 raw_images/*/ 子文件夹（每个是一帧）                │
│     自动识别帧号（兼容 "YYYY-MM-DD-HHMMSS" 和               │
│     "114-YYYY-MM-DD-HHMMSS" 两种命名）                       │
│ T2  复制帧文件夹 → LiteGSWin/data/<MMDD>/<dirname>/         │
│ T3  uv run python batch_run.py --sub_dir <MMDD>            │
│     (已有 PLY 或 CameraData 已有 PLY → 跳过)                │
│ T4  复制 results/<MMDD>/cameras.json → CameraData/          │
│ T5  复制 results/<MMDD>/<MMDD>-<HHMMSS>.ply → CameraData/  │
│                                                             │
│  幂等规则: T2 帧目录存在→跳过 / T3 PLY存在→跳过 /            │
│           T4 始终覆盖 / T5 PLY存在→跳过                      │
└─────────────────────────────────────────────────────────────┘
         │
         ▼  [输入 Enter 开始扫描可合并 PLY]
         │   (用户可在此间隙手动拷贝额外 PLY 到项目目录)
┌─ fuse ──────────────────────────────────────────────────────┐
│  [交互] 列出项目下所有 PLY (idx + 大小 + 修改时间)           │
│         输入要合并的 PLY 编号 (逗号分隔, 回车=preset 默认)    │
│  [交互] 按 ENTER 确认                                       │
│  [自动] fuse_ply → combine PLY                              │
│  [自动] clip_ply → <project>-clip/*.ply                     │
│                                                             │
│  幂等规则: combine*.ply 已存在→跳过 / <project>-clip 存在→跳过│
└─────────────────────────────────────────────────────────────┘
         │
         ▼
┌─ render ────────────────────────────────────────────────────┐
│  [交互] 列出 <project>-clip/*.ply → 输入 idx 选择           │
│  [交互] 列出 jsons_path/*.json → 输入 idx (如有)            │
│  [自动] Playwright 启动 Chrome → 上传 PLY + JSON            │
│  [自动] 渲染视频 → renders/<project>.mp4                    │
└─────────────────────────────────────────────────────────────┘
```

**初次使用**需要预先完成：
1. `npm run build` + `npm run serve`
2. 确保 `LiteGSWin/data/calibration/<MMDD>/` 下有相机标定数据
3. 在 `CameraData/<project>/raw_images/` 下按帧创建子文件夹，放入训练图片
4. 在 `presets.json` 中创建对应 preset（含 fuse.indices 作为默认合并索引）

### 4.3 断开重跑

两个管线的所有阶段都是**幂等**的。中途断开后，再次运行同一条命令（不加 `--steps`）即可从断开处继续：

```
v5: python tills/run_pipeline_v5.py --config CameraData/01/pipeline.json
v6: python tills/run_pipeline_v6.py --config CameraData/02/pipeline.json
```

- train 已完成 → 所有 T 步骤跳过
- fuse 已完成 → combine PLY 和 `<project>-clip/` 存在 → 跳过
- render 阶段永远是交互式的（选 PLY + 可选选 JSON）

## 5. `--steps` 分步运行（可选，调试用）

```bash
# v5
python tills/run_pipeline_v5.py --config CameraData/01/pipeline.json --steps train
python tills/run_pipeline_v5.py --config CameraData/01/pipeline.json --steps clip
python tills/run_pipeline_v5.py --config CameraData/01/pipeline.json --steps render

# v6
python tills/run_pipeline_v6.py --config CameraData/02/pipeline.json --steps train
python tills/run_pipeline_v6.py --config CameraData/02/pipeline.json --steps fuse
python tills/run_pipeline_v6.py --config CameraData/02/pipeline.json --steps render
```

组合步骤：`--steps fuse,render`

强制重新生成：`--force`

调试模式（保留中间 MP4/TS）：`--debug`（仅 v5）

## 6. 新建项目时需准备的文件

以新建 `CameraData/03/` 为例。

### 6.1 共同准备工作

1. 在 `tills_ply/presets.json` 中创建对应 preset（见 [presets.json 示例](#31-presetsjson-示例)）
2. 创建 `CameraData/03/pipeline.json`（按 v5 或 v6 格式填写）
3. 在 `CameraData/03/` 下创建 `raw_images/` 目录并放入图片
4. 如果使用 `jsons_path`：确保目标文件夹存在且内含 `.json` 相机文件
5. 确保 LiteGSWin 的 `data/calibration/<MMDD>/` 下有相机标定数据

**以上是用户唯一需要手动准备的。** 其余目录（`Train_imgs/`、`renders/`、`output/`、`03-clip/`、`*.ply`）
均在管线运行过程中自动生成。

### 6.2 v5 特有：raw_images 的图片格式

```
CameraData/03/
├── pipeline.json          # 用户手写
└── raw_images/            # 用户放入
    └── DJD-2026-06-23-175925 000.jpg   ← 扁平序号 JPG
        DJD-2026-06-23-175925 001.jpg
        DJD-2026-06-23-175925 002.jpg
        ...
```

- 图片必须是**扁平序号**格式：`<前缀> <序号>.jpg`（如 `DJD-2026-06-23-175925 000.jpg`）
- 前缀中的时间戳（`2026-06-23-175925`）用于自动推导训练参数
- 也可以放在 `raw_images/` 下的**单个子文件夹**中（管线自动发现）

### 6.3 v6 特有：raw_images 的图片格式

```
CameraData/03/
├── pipeline.json              # 用户手写
└── raw_images/                # 用户放入
    ├── 2026-06-18-171609/     ← 帧 1（无需前缀）
    │   ├── 001.jpg
    │   ├── 002.jpg
    │   └── ...
    └── 114-2026-06-18-171705/ ← 帧 2（带 114- 前缀）
        ├── 001.jpg
        ├── 002.jpg
        └── ...
```

- 每个帧是一个**子文件夹**，文件夹名包含日期时间戳
- 支持两种命名：
  - `YYYY-MM-DD-HHMMSS`（无前缀）
  - `<前缀>-YYYY-MM-DD-HHMMSS`（带前缀，如 `114-2026-06-18-171705`）
- 子文件夹内的图片命名不重要（会被 LiteGS 统一重命名）
- 文件夹名中的 MMDD（如 `0618`）自动推导为 LiteGS 的 `sub_dir`

### 6.4 初始状态对照

| 路径 | 用户准备 | 管线生成 |
|------|:--:|:--:|
| `CameraData/03/pipeline.json` | ✅ | |
| `CameraData/03/raw_images/` | ✅ | |
| `CameraData/03/cameras.json` | | ✅ (v6 T4) |
| `CameraData/03/Train_imgs/` | | ✅ (v5 T1) |
| `CameraData/03/*.ply` | | ✅ (训练产出) |
| `CameraData/03/renders/` | | ✅ |
| `CameraData/03/output/` | | ✅ (仅 v5) |
| `CameraData/03-clip/` | | ✅ |
| `tills_ply/presets.json` (preset 条目) | ✅ | |
| `LiteGSWin/data/calibration/<MMDD>/` | ✅ | |

### 6.5 presets.json 示例

```json
{
  "presets": {
    "03-0618测试": {
      "path": "CameraData/03",
      "max_index": 89,
      "fuse": {
        "radius_scale": 0.8,
        "height_up": 3,
        "height_down": 0.5,
        "bias": true,
        "bias_margin": 0.35,
        "bias_radius_percentile": 15,
        "indices": [1, 2]
      },
      "clip": {
        "clip_percent": 0.1,
        "denoise": true,
        "denoise_voxel_size": 0.1,
        "denoise_min_points": 20,
        "radius_scale": 1.0,
        "ring_delete": true,
        "ring_outer_delta": 0.2,
        "ring_inner_delta": 0.3,
        "ring_height_up": 1.5,
        "ring_height_down": 0.3
      }
    }
  }
}
```

> **注意**：`path` 字段对 v5/v6 均不起实际作用（两个管线都从 `pipeline.json` 的 `project` 字段推导路径）。
> 保留它是因为 ply_pipeline.py 独立运行时需要。

## 7. 项目目录结构

```
CameraData/<project>/
├── pipeline.json          # 管线配置
├── cameras.json           # 相机参数（LiteGS 训练产出或手动放置）
├── raw_images/            # 原始素材
│   ├── *.jpg              # v5: 扁平序号 JPG
│   └── <frame_dir>/       # v6: 每帧一个子文件夹
│       └── *.jpg
├── Train_imgs/            # 训练素材（自动提取）
│   └── <YYYY-MM-DD-HHMMSS>/
│       └── *.jpg
├── renders/               # SuperSplat 渲染输出
│   └── *.mp4
├── *.ply                  # 训练产出 / 融合结果 PLY
└── output/                # 最终拼接视频（仅 v5）
    └── output.mp4

CameraData/<project>-clip/ # clip 处理后的 PLY
└── *.ply
```

## 8. 关联文档

- [V5 自动化技术文档](V5_AUTOMATION.md) — Playwright/OPFS 原理、Bug 分析
- `tills_ply/presets.json` — PLY 处理参数预设
- `Docs/HANDOFF_*.md` — 历次交接文档
