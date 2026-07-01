---
name: pipeline-v5-v6
description: v5/v6 automated pipelines: LiteGS training + Playwright SuperSplat rendering + video concat
metadata:
  type: project
---

# v5 / v6 自动化管线

## v5：单段训练 + 实拍混剪

`tills/run_pipeline_v5.py --config CameraData/<project>/pipeline.json`

流程：train → clip → render
- train: 从 raw_images 扁平 JPG 提取训练帧 → LiteGS 训练 → 拷贝 PLY + cameras.json
- clip: 对项目下全部 PLY 跑 clip_ply
- render: Playwright 上传 PLY + JSON → 自动渲染 → concat → output.mp4

Config 核心字段：`project`, `preset`, `litegs_path`, `jsons_path?`, `output.segments/fps/crf/resolution/source`
所有阶段幂等，断开重跑不加 --steps 即可继续。

## v6：多帧批量训练 + 交互 fuse + 纯渲染

`tills/run_pipeline_v6.py --config CameraData/<project>/pipeline.json`

流程：train → fuse → render
- train: 扫描 raw_images/*/ 帧文件夹 → LiteGS 批量训练
- fuse: 交互式选择 PLY 索引 (Enter=preset 默认) → fuse_ply → clip_ply (只处理新合成的 PLY)
- render: Playwright 上传 + 渲染 → renders/<project>.mp4

Config 核心字段：`project`, `preset`, `litegs_path`, `jsons_path?`, `fps`, `resolution`
preset["path"] 被 v6 忽略——始终用 cfg["project"] 推导项目路径。

## 共享代码

`tills/_shared.py` — v5/v6 共用函数：
- `step()`, `check_dev_server()` — 工具函数
- `load_preset()`, `build_clip_args()`, `parse_frame_dirname()` — preset 参数读取
- `_select_from_list()`, `select_ply()`, `select_json()` — 交互式文件选择
- `ensure_browser()`, `upload_ply()`, `upload_json_file()`, `verify_timeline()`, `render_video()` — Playwright 自动化
- `ROOT`, `TILLS_PLY_DIR` — 常量

## SuperSplat 源码改动

`src/file-handler.ts:163`: `cameraImportSessionMode` 默认改为 `'both'` — JSON 导入不弹对话框，直接 GT+Timeline。
**Why:** 消除 Playwright 自动化中需要处理 PCUI 对话框的复杂性。
**How to apply:** `npm run build` 后生效。

## [Pipeline v4](pipeline-v4.md) — 旧版，保留独立运行
## [PLY Pipeline](ply-pipeline.md) — ply_pipeline + presets.json，独立工具
