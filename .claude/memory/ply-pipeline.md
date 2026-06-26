---
name: ply-pipeline
description: "tills_ply/ply_pipeline.py — 三步 PLY 处理管线: interpolate → fuse → clip，基于 preset 管理参数"
metadata: 
  node_type: memory
  type: project
  originSessionId: 200a40e4-a9fb-44c0-aaef-a4778c83a728
---

# PLY Pipeline (`tills_ply/ply_pipeline.py`)

**定位**: PLY 点云批处理管线。从相机位姿出发，生成环绕轨迹 → 融合多个 PLY → 裁剪/去噪。

**何时使用**: 当项目有 `cameras.json`（COLMAP 重建结果）和多个 PLY 点云文件需要处理时。

## 三步流程

```
interpolate → fuse → clip
```

| Step | 脚本 | 输入 | 输出 |
|------|------|------|------|
| interpolate | `tills_ply/interpolate_cameras_circle.py` | `cameras.json` | `cameras_align.json`（300 环绕位姿） |
| fuse | `tills_ply/fuse_ply.py` | `cameras.json` + 多 PLY | `*combine*.ply`（圆柱区域融合） |
| clip | `tills_ply/clip_ply.py` | `*combine*.ply` + `cameras.json` | `-clip/*.ply`（体积裁剪 + denoise） |

## 共享参数

- `path`: 项目目录（如 `CameraData/08`）
- `max_index`: 圆拟合所用的相机范围 `id=0..max_index`（**顶层共用**）
- `cameras.json`: 三步都需要读取（圆拟合）

## Preset 格式 (`tills_ply/presets.json`)

```json
{
  "path": "CameraData/08",
  "max_index": 89,
  "interpolate": {
    "total": 300,
    "anchor_camera": "006",
    "radius_scale": 1.0
  },
  "fuse": {
    "radius_scale": 1.0,
    "height_up": 2, "height_down": 0.5,
    "bias": true, "bias_margin": 0.35,
    "indices": [1, 2]
  },
  "clip": {
    "clip_percent": 0.0,
    "denoise": true,
    "ring_delete": true, ...
  }
}
```

注意 `radius_scale` 在三个 section 中语义不同（轨道半径 vs 圆柱半径），各自保留。

## Config 文件（`tills_ply/`）

| Config | 对应脚本 | 用途 |
|--------|----------|------|
| `interpolate_config.json` | `interpolate_cameras_circle.py` | total, anchor_camera, radius_scale |
| `fuse_config.json` | `fuse_ply.py` | path, max_index, radius_scale, height, bias, indices |
| `clip_config.json` | `clip_ply.py` | path, max_index, clip_percent, denoise, ring_delete |

`--save <name>` 读取这三个 config 生成 preset（`max_index` 自动提取到顶层，`path` 从 fuse/clip 复用）。

## 命令行

```bash
# 管理
python tills_ply/ply_pipeline.py --list
python tills_ply/ply_pipeline.py --show "08-0623测试"
python tills_ply/ply_pipeline.py --save <name>
python tills_ply/ply_pipeline.py --del <name>

# 执行
python tills_ply/ply_pipeline.py --preset "08-0623测试"              # 全三步
python tills_ply/ply_pipeline.py --preset "08-0623测试" --step fuse  # 单步
python tills_ply/ply_pipeline.py                                     # 交互式
```

- **默认 force**: 每次运行删除旧产出后重新生成，无需 `--force` 标志
- `--presets-file`: 可指定其他 preset 文件路径

## 工具模块

`tills_ply/ply_utils.py` → 共享 `read_ply()`, `write_ply()`, `fit_circle()`，被 `fuse_ply.py` 和 `clip_ply.py` 引用。

**Why:** PLY 处理需要圆拟合来定义空间参考系（圆柱裁剪、denoise），这与 `interpolate_cameras_circle` 的圆拟合完全一致。将三者统一到同一 preset 管理框架避免参数漂移。

**How to apply:** 新项目时编辑三个 config → `--save` → `--preset` 一键执行。`max_index` 决定圆拟合范围，是所有步骤的核心参数。
