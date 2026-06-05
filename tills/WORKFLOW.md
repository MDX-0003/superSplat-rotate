# 02 数据集工作流文档

## 目录结构

```
CameraData/02/
├── 2026-06-04-214336/          ← 原始多机位采集 (168帧 × 63相机)
│   ├── 0001/  (001.jpg~063.jpg)
│   └── ...
├── 2026-06-04-214336_cam06/    ← extract_camera.py 提取的相机6单路序列 (168张)
├── 未对齐文件/                   ← COLMAP 各帧独立重建产物 (坐标系不共用，已废弃)
├── cameras.json                ← 统一坐标系下的相机内外参 (45个关键帧, id 0~44)
├── cameras_align.json          ← 最终输出的300个插值位姿
├── combine.ply                 ← 合并点云
├── align_frame*_cam06.ply      ← 对齐后的各帧点云
├── blended_frames/             ← blend_frames.py 输出 (372帧混合序列)
├── MQR_output/                 ← pngs_to_mp4.py 输出 (MP4视频)
├── pngs_to_mp4.py              ← 图像序列→MP4
├── cameras_frame65_cam06.json  ← 帧65的相机位姿 (备用)
├── 02.ply / 03.ply             ← 临时ply
└── point_cloud_*.ply           ← 临时ply
```

## 完整工作流

```
[1] 原始采集 → 2026-06-04-214336/ (168帧×63相机, JPG)
        │
        ▼  tills/extract_camera.py
[2] 提取相机6  → 2026-06-04-214336_cam06/ (0001~0168.jpg)
        │
        ▼  COLMAP (外部, 使用统一相机内外参)
[3] 多帧重建   → align_frame065_cam06.ply 等对齐点云
        │         cameras.json (统一坐标系, 45个相机位姿)
        │
        ▼  tills/interpolate_cameras_circle.py
[4] 圆插值     → cameras_align.json (300个位姿, 360°闭环)
        │
        ▼  SuperSplat (网页端, 手动)
[5] 渲染300帧  → C:/Users/Administrator/Downloads/circle_0001~0300.png
        │
        ▼  tills/blend_frames.py
[6] 混合编排   → blended_frames/ (64实拍 + 300渲染 + 8实拍 = 372帧)
        │
        ▼  CameraData/02/pngs_to_mp4.py
[7] 编码视频   → MQR_output/frame.mp4
```

## 各步骤详情

### [1] 原始采集
- **输入**: 无 (硬件采集)
- **输出**: `2026-06-04-214336/0001~0168/001~063.jpg`
- **状态**: 手动完成，无需自动化

### [2] 提取相机6
- **脚本**: `tills/extract_camera.py`
- **输入**: `2026-06-04-214336/` (168个帧目录, 每个含63张JPG)
- **输出**: `2026-06-04-214336_cam06/0001~0168.jpg`
- **命令**: `python tills/extract_camera.py --camera 6`
- **自动化可行性**: 高 (输入/输出路径固定)

### [3] COLMAP 多帧重建
- **输入**: `2026-06-04-214336_cam06/` + 多帧原始图像
- **输出**: `align_frame*_cam06.ply`, `cameras.json`
- **关键要求**: 所有帧使用同一组相机内外参 (保证坐标系统一)
- **状态**: 外部工具，手动执行

### [4] 圆插值生成300位姿
- **脚本**: `tills/interpolate_cameras_circle.py`
- **输入**: `CameraData/02/cameras.json` (45个关键帧)
- **输出**: `CameraData/02/cameras_align.json` (300个插值位姿)
- **参数**: `ANCHOR=6`, `START_IDX=6`, `--radius-scale 0.85`, `--total 300`
- **命令**: `python tills/interpolate_cameras_circle.py --radius-scale 0.85`
- **自动化可行性**: 高 (输入/输出路径固定)

### [5] SuperSplat 渲染 (手动步骤)
- **输入**: `cameras_align.json` (拖入SuperSplat) + `combine.ply` (加载模型)
- **输出**: `C:/Users/Administrator/Downloads/circle_0001~0300.png`
- **操作**:
  1. 打开 SuperSplat → 加载 `combine.ply`
  2. 导入 `cameras_align.json`
  3. View Panel → GT Camera → 设置分辨率/参数 → Export All
  4. 浏览器自动下载 300张 PNG 到 Downloads
- **痛点**: 手动拖文件、手动点 Export、依赖浏览器下载目录
- **半自动化方案**: 通过 URL 参数或脚本预设 ply/cameras 路径

### [6] 混合编排
- **脚本**: `tills/blend_frames.py`
- **输入**:
  - 实拍: `2026-06-04-214336_cam06/0001~0168.jpg`
  - 渲染: `C:/Users/Administrator/Downloads/circle_0001~0300.png`
- **输出**: `blended_frames/frame_0001~0372.{jpg,png}`
- **配置**: `KEEP_END=64`, `TAIL_START=161`, `RENDER_COUNT=300`
- **命令**: `python tills/blend_frames.py`
- **自动化可行性**: 中 (依赖 Downloads 路径, 需要渲染步骤先完成)

### [7] 编码视频
- **脚本**: `CameraData/02/pngs_to_mp4.py`
- **输入**: `blended_frames/frame_*.jpg`
- **输出**: `MQR_output/frame.mp4`
- **命令**: `python pngs_to_mp4.py`
- **自动化可行性**: 高

## 当前手工操作清单

| 步骤 | 手工操作 | 可自动化? |
|------|---------|-----------|
| [3] COLMAP | 命令行执行重建 | 外部工具 |
| [5] SuperSplat | 打开网页 → 拖PLY → 导JSON → 点Export → 等下载 | **最大痛点** |
| [5b] | 把 Downloads 的 circle_*.png 移入工作目录 | 可脚本化 |
| [2][4][6][7] | 逐个执行脚本 | **可合并为单条 pipeline** |

## 建议自动化方案

### 主控脚本 `tills/run_pipeline.py`

```python
# 按顺序调用各个步骤，检查输入/输出文件是否存在
# 用法: python tills/run_pipeline.py [--skip step1,step2] [--radius-scale 0.85]

1. 检查 cameras.json 是否存在 → 否则报错并提示执行 COLMAP 重建
2. 运行 interpolate_cameras_circle.py
3. 检查 cameras_align.json 生成成功
4. 提示用户在 SuperSplat 中完成渲染 (或自动打开浏览器)
5. 等待/检查 Downloads/circle_*.png 数量 == 300
6. 拷贝 circle_*.png 到 CameraData/02/renders/
7. 运行 blend_frames.py
8. 检查 blended_frames/ 文件数正确
9. 运行 pngs_to_mp4.py
```

### 路径统一化

所有脚本的路径常量集中到 `tills/paths.py`:

```python
# tills/paths.py
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "CameraData" / "02"
RAW_FRAMES      = DATA / "2026-06-04-214336"
CAM06_EXTRACT   = DATA / "2026-06-04-214336_cam06"
CAMERAS_JSON    = DATA / "cameras.json"
CAMERAS_ALIGN   = DATA / "cameras_align.json"
RENDERS_DIR     = DATA / "renders"           # circle_*.png in here (copy from Downloads)
BLENDED_DIR     = DATA / "blended_frames"
MQR_OUTPUT      = DATA / "MQR_output"
```

### SuperSplat 半自动化

SuperSplat 当前完全手动。可选改进方向:
1. 写一个本地 HTTP 服务器脚本, 自动 serve `cameras_align.json` 和 `combine.ply`
2. 打开浏览器 `http://localhost:3000?ply=combine.ply&cameras=cameras_align.json`
3. 仍需手动点 Export All, 但至少文件加载可自动

是否需要我按这个方案实现自动化脚本?
