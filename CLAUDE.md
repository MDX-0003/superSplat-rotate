# CLAUDE.md — SuperSplat 项目指南

## 项目概述

本项目包含两套代码体系：

### 1. SuperSplat Web 编辑器（`src/`）

PlayCanvas 3DGS 编辑器，开源。我们在此之上添加了**批量导出 GT 相机渲染**功能。

| 层级 | 技术栈 |
|------|--------|
| 框架 | TypeScript + PlayCanvas 引擎 + PCUI 组件库 |
| 构建 | Rollup + SCSS (Dart Sass) |
| 运行 | `npm run build` → `npm run serve` (node serve-dist.mjs) 或 `npm run develop` (watch + serve) |
| 渲染 | WebGL 2.0, splat rendering via gsplat, offscreen render targets |

**关键改动区域：**
- `src/render.ts` — `render.batchGtCameras` 批量 offscreen 渲染 + PNG 下载
- `src/ui/view-panel.ts` — GT Camera 区域导出按钮 + 分辨率/偏移/圆心 UI
- `src/sw.ts` — Service Worker 改为 network-first 策略
- `src/ui/localization.ts` — 支持 i18next `{{var}}` 模板插值
- `src/ui/scss/view-panel.scss` — `.view-panel-row-input` 样式
- `static/locales/` — 14 个新增中英文字符串

### 2. Python 管线（`tills/`）

6 步自动化管线，将 COLMAP 重建结果 + SuperSplat 渲染插入实拍序列，输出混合视频。

| 脚本 | 功能 |
|------|------|
| `paths.py` | 共享路径常量 `project(name)` → `CameraData/<name>/` |
| `run_pipeline.py` | **主控脚本**，`--project` + `--force` 串联全部步骤 |
| `colmap_bin_to_json.py` | Step 1: COLMAP bin → cameras.json |
| `extract_camera.py` | Step 2: 从多机位帧提取指定相机 |
| `interpolate_cameras_circle.py` | Step 3: 圆拟合 → 300 环绕位姿插值 |
| `interpolate_cameras.py` | 旧版圆插值（高斯平滑，保留） |
| `interpolate_cameras_arc.py` | 弃用（弧线非闭环） |
| `blend_frames.py` | Step 5: 实拍 + 渲染混合编排（支持 v2 segments 模式） |
| `pngs_to_mp4.py` | Step 6: ffmpeg 编码 MP4 |

**项目数据结构**（`CameraData/<project>/`）：
```
raw_frames/        # 原始多机位帧（初始）
colmap_bins/       # COLMAP bin（初始）
cameras.json       # [Step 1] 63 台相机内外参
anchor_frames/     # [Step 2] 锚点相机连续帧
cameras_align.json # [Step 3] 300 环绕位姿
renders/           # [Step 4] SuperSplat 渲染 PNG（手动放入）
blended/           # [Step 5] 混合序列
output/            # [Step 6] MP4
```

## 推荐 Skills

编写代码、分析逻辑、修改管线时使用以下 skill：

| Skill | 适用场景 |
|-------|----------|
| **`codegen`** | 修改 SuperSplat TypeScript 源码或 Python 管线脚本时。遵循项目现有模式，最小化新增文件/函数，复用已有 helpers |
| **`neat-freak`** | 会话结束后同步文档（PIPELINE.md, Docs/, tills/*.md），确保 docs 和代码一致 |
| **`complex-runtime-chain-notes`** | 解释 SuperSplat 渲染管线的执行链（offscreen render → copyRt → read → compress → download），或 Python 圆插值的数学原理 |
| **`renderdoc-mcp`** | 如果 SuperSplat 渲染结果不对，用 RenderDoc 抓帧调试 WebGL |
| **`nsight-reader`** | GPU 性能分析（本项目的 3DGS 渲染路径） |

## 编码约定

### SuperSplat (TypeScript)
- 事件总线驱动：`events.fire()` / `events.on()` / `events.function()` / `events.invoke()`
- UI 组件：PCUI Container/Label/Button/SelectInput/NumericInput
- 渲染：`startOffscreenMode → forceRender → postRender → copyRt → read`
- 每次都先构建：`npm run build`，改 dist 后需刷新浏览器（SW 已改 network-first，通常刷一次即可）

### Python 管线
- 所有脚本通过 `--project <name>` 定位项目目录
- 路径计算用 `tills/paths.py` 的 `project(name)` 函数
- 中间产物存在则跳过，`--force` 强制覆盖
- Step 4 是唯一手动步骤（SuperSplat 渲染），管线在此暂停等待

## 当前状态

- **批次导出功能**：[正常] SuperSplat 批量导出已在 View Panel 可用
- **Python 管线**：[正常] 全 6 步可运行，`--force` 覆盖已生效
- **blend_frames**：[正常] 支持 v2 多段模式 (`--segments`)
- **Service Worker**：[正常] network-first 策略，不再缓存旧版

## 文档索引

| 文档 | 位置 | 内容 |
|------|------|------|
| 交接文档 | `Docs/HANDOFF_2026-06-05.md` | 全部功能、实现路径、文件清单 |
| 管线说明 | `PIPELINE.md` | 6 步流程、参数说明、快速启动 |
| 自动化设计 | `tills/WorkFlow_1.md` | 自动化方案的初始设计文档 |
| 旧工作流 | `tills/WORKFLOW.md` | CameraData/02 特化记录 |

*最后更新: 2026-06-05*
