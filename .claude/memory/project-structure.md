---
name: project-structure
description: SuperSplat 项目多层体系：Web 编辑器、Python 管线(v4/v5/v6)、PLY 处理工具、LiteGSWin 训练
metadata:
  type: project
---

# 项目结构

## 四层体系

1. **SuperSplat Web 编辑器** (`src/`) — PlayCanvas 3DGS 编辑器，经由 Playwright 的 `events` API 被管线操控
2. **Python 管线** (`tills/`) — run_pipeline_v4/v5/v6 + _shared.py
3. **PLY 处理工具** (`tills_ply/`) — ply_pipeline.py + fuse_ply.py + clip_ply.py + presets.json
4. **LiteGSWin** (`../LiteGSWin/`) — 3DGS 训练环境，独立仓库，通过 `litegs_path` 配置引用

## 管线版本

| 版本 | 文件 | 场景 | 状态 |
|------|------|------|------|
| v4 | `tills/run_pipeline_v4.py` | 扁平图像 + timeline → 实拍混剪 | 保留 |
| **v5** | `tills/run_pipeline_v5.py` | LiteGS 训练 + Playwright 自动化 + concat | **主力** |
| **v6** | `tills/run_pipeline_v6.py` | 多帧训练 + 交互 fuse + 纯渲染 | **主力** |
| _shared | `tills/_shared.py` | v5/v6 共享函数 | 基础设施 |

## 数据目录

```
CameraData/<project>/
├── pipeline.json     # v5/v6 配置文件
├── cameras.json      # 相机参数（LiteGS 训练产出）
├── raw_images/       # 原始素材 (v5: 扁平 JPG / v6: 每帧子文件夹)
├── Train_imgs/       # 训练素材（自动提取）
├── renders/          # SuperSplat 渲染输出 MP4
├── *.ply             # 训练产出 / combine PLY
├── <project>-clip/   # clip 处理后的 PLY
└── output/           # 拼接视频（仅 v5）

tills_ply/
├── ply_pipeline.py   # Preset 驱动全流程（独立工具）
├── fuse_ply.py       # 多 PLY 融合（单 PLY 自动跳过）
├── clip_ply.py       # 裁剪/去噪（支持 --files + 同名跳过）
└── presets.json      # 命名参数预设
```

## 代码关系

- v5/v6 通过 `tills/_shared.py` 共享 Playwright 函数、preset 加载、文件选择
- v5/v6 从 `presets.json` 读 clip 参数，不内联
- preset["path"] 被 v5/v6 覆盖（始终用 `cfg["project"]` 推导），ply_pipeline 独立使用时不受影响
- v4 和 ply_pipeline 保持独立可用

**Why:** 三层管线并行发展，保持各自独立性但共享核心工具。
**How to apply:** 新增功能优先放入 `_shared.py`。
