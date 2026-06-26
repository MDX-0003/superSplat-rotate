---
name: project-structure
description: SuperSplat 项目的两套管线体系、目录布局、代码重复关系
metadata: 
  node_type: memory
  type: project
  originSessionId: 200a40e4-a9fb-44c0-aaef-a4778c83a728
---

# 项目结构

## 两大管线体系

### 1. 视频管线（`tills/`）

将实拍序列 + SuperSplat 渲染 MP4 拼接为输出视频。

| 版本 | 文件 | 场景 | 状态 |
|------|------|------|------|
| v1 | `run_pipeline.py` | COLMAP → 圆插值 → 渲染 → 混合 | 遗留 |
| v2 | `run_pipeline_v2.py` | v1 + segments 模式 | 遗留 |
| v3 | `run_pipeline_v3.py` | UE seq + JSON timeline | 遗留 |
| **v4** | `run_pipeline_v4.py` | **扁平图像 + timeline，无 COLMAP/UE 依赖** | **当前主力** |

v4 关键文件:
- `tills/run_pipeline_v4.py` — 主控
- `tills/paths.py` — `project(name)` 解析 `CameraData/<name>`
- `tills/timeline/` — timeline JSON 文件

### 2. PLY 管线（`tills_ply/`）

PLY 点云批处理：生成相机轨迹 → 融合 → 裁剪去噪。

| 文件 | 角色 |
|------|------|
| `tills_ply/ply_pipeline.py` | 编排器（三步 + preset 管理） |
| `tills_ply/interpolate_cameras_circle.py` | Step 1: 圆拟合 → 300 环绕位姿 |
| `tills_ply/fuse_ply.py` | Step 2: 圆柱区域 PLY 融合 |
| `tills_ply/clip_ply.py` | Step 3: 体积裁剪 + denoise + ring_delete |
| `tills_ply/ply_utils.py` | 共享 PLY I/O + `fit_circle()` |
| `tills_ply/presets.json` | 命名参数预设 |
| `tills_ply/interpolate_config.json` | interpolate 专属 config |
| `tills_ply/fuse_config.json` | fuse 专属 config |
| `tills_ply/clip_config.json` | clip 专属 config |

## 代码重复情况

`tills_ply/` 下的 `fuse_ply.py`、`clip_ply.py`、`interpolate_cameras_circle.py` 是从 `tills/` 重构迁移而来：

| `tills_ply/` (新版) | `tills/` (原版) | 差异 |
|---------------------|-----------------|------|
| `fuse_ply.py` | `fuse_ply.py` | 新版导入 `ply_utils`；新版无交互式输入（只接受 `--indices`） |
| `clip_ply.py` | `clip_ply.py` | 新版导入 `ply_utils`；新版多了 `--ring-delete` 功能 |
| `interpolate_cameras_circle.py` | `interpolate_cameras_circle.py` | 新版支持 `--path`（直接传目录）；原版只用 `--project` |
| `ply_utils.py` | — | 共享模块，原版无对应（原版代码内联） |

**原则**: `tills_ply/` 是当前 PLY 处理的规范位置。`tills/` 下原版保留兼容旧管线（`run_pipeline.py` 等）。

## 数据目录

```
CameraData/<project>/
  cameras.json              # COLMAP 重建结果（63 台相机内外参）
  cameras_align.json         # interpolate_cameras_circle 输出（300 环绕位姿）
  raw_images/                # v4 扁平 JPG
  raw_frames/                # v1-v3 多机位帧
  renders/                   # SuperSplat 渲染 MP4
  anchor_frames/             # 提取的实拍帧
  output/                    # 最终 output.mp4
  Train_imgs/                # v4 训练图
  plys/                      # PLY 点云文件（或直接放项目根目录）
  *-clip/                    # clip_ply 输出目录
  *combine*.ply              # fuse_ply 输出
```

## 两管线的关系

- **独立运行**: v4 和 ply_pipeline 互不依赖，可独立执行
- **共享数据源**: 都读取 `cameras.json`；`max_index` 在两套体系中含义相同（圆拟合相机范围）
- **不同目标**: v4 产出视频 MP4；ply_pipeline 产出处理后的 PLY

**Why:** 理解两套体系的位置和边界，避免在错误的目录修改脚本。PLY 管线已从 `tills/` 迁移到 `tills_ply/` 形成独立体系。

**How to apply:** 视频拼接用 v4（`tills/run_pipeline_v4.py`），PLY 处理用 ply_pipeline（`tills_ply/ply_pipeline.py`）。修改时注意对应目录。
