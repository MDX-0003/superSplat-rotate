# CLAUDE.md — SuperSplat 项目指南

## 项目概述

本项目包含两套代码体系。**Python 环境统一使用 uv 管理**：

```powershell
uv sync                          # 首次：创建 .venv + 安装依赖
uv run playwright install chromium  # 首次：安装 Playwright 浏览器
uv run python -m tills.server.train_daemon --config CameraData/05/pipeline.json
```

### 1. SuperSplat Web 编辑器（`src/`）

PlayCanvas 3DGS 编辑器，开源。我们在此之上添加了**批量导出 GT 相机渲染**功能。

| 层级 | 技术栈 |
|------|--------|
| 框架 | TypeScript + PlayCanvas 引擎 + PCUI 组件库 |
| 构建 | Rollup + SCSS (Dart Sass) |
| 运行 | `npm run build` → `npm run serve` (node serve-dist.mjs) 或 `npm run develop` (watch + serve) |
| 渲染 | WebGL 2.0, splat rendering via gsplat, offscreen render targets |

**关键改动区域：**
- `src/file-handler.ts:163` — `cameraImportSessionMode` 默认改为 `'both'`，消除 JSON 导入弹窗
- `src/render.ts` — `render.batchGtCameras` 批量 offscreen 渲染 + PNG 下载
- `src/ui/view-panel.ts` — GT Camera 区域导出按钮 + 分辨率/偏移/圆心 UI
- `src/sw.ts` — Service Worker 改为 network-first 策略

### 2. Python 管线（`tills/`）

自动化管线，包含 3DGS 训练调度 + SuperSplat 网页渲染自动化 + 视频拼接。

| 脚本 | 功能 |
|------|------|
| `run_pipeline_v5.py` | **v5 主控**：LiteGS 训练 + clip + Playwright 自动渲染 + 实拍混剪 |
| `run_pipeline_v6.py` | **v6 主控**：多帧 LiteGS 训练 + 交互式 fuse + Playwright 纯渲染 |
| `_shared.py` | v5 / v6 共享函数库（Playwright 自动化、preset 加载、文件选择、工具函数） |
| `paths.py` | 共享路径常量 `project(name)` → `CameraData/<name>/` |
| `run_pipeline.py` | v1: 6 步圆形插值管线（v4 为最终版，含 blend + concat） |
| `run_pipeline_v2.py` | v2: UE 轨迹 + 弧形桥接 |
| `run_pipeline_v3.py` | v3: JSON timeline 驱动 |
| `run_pipeline_v4.py` | v4: 扁平序列图片，real→render→real concat |

### 3. PLY 处理（`tills_ply/`）

| 脚本 | 功能 |
|------|------|
| `ply_pipeline.py` | Preset 驱动的全流程工具：interpolate → fuse → clip |
| `fuse_ply.py` | 多 PLY 融合（圆拟合 + 圆柱体过滤 + bias 修正）。单 PLY 自动跳过 |
| `clip_ply.py` | PLY 裁剪/去噪/环形删除。支持 `--files` 选择性处理 + 同名跳过 |
| `interpolate_cameras_circle.py` | 圆形环绕相机位姿插值 → `cameras_align.json`（转一圈） |
| `interpolate_cameras_swing.py` | 圆弧摆动相机位姿插值 → `cameras_spin.json`（从锚点相机摆 ±X 度后回到起点，首尾姿态逐比特相同，可无缝循环） |
| `ply_utils.py` | PLY 二进制读写 + 圆拟合 |
| `presets.json` | 命名参数预设：path / max_index / interpolate / fuse / clip |

### 4. LiteGSWin（`LiteGSWin/`，独立仓库）

3DGS 训练环境。通过 `uv run python batch_run.py --sub_dir <MMDD>` 批量训练。
配置文件中用 `litegs_path` 指向其路径。

## 项目数据结构

```
CameraData/<project>/
├── pipeline.json          # 管线配置（v5/v6 必读）
├── cameras.json           # 相机参数（LiteGS 训练产出）
├── raw_images/            # 原始素材
│   ├── *.jpg              # v5: 扁平序号 JPG
│   └── <frame_dir>/       # v6: 每帧一个子文件夹
├── Train_imgs/            # 训练素材（自动提取）
├── renders/               # SuperSplat 渲染输出 MP4
├── *.ply                  # 训练产出 PLY
├── *-clip/                # clip 处理后的 PLY
└── output/                # 最终拼接视频（仅 v5）
```

## v5 vs v6

| | v5 | v6 |
|------|-----|------|
| 适用场景 | 连续 JPG → 按范围抽训练帧 → fuse+clip → 实拍混剪 | 预抽帧文件夹 → 批量训练 → 交互 fuse → 纯渲染 |
| Config 核心字段 | `project`, `preset`, `litegs_path`, `jsons_path?`, `output.segments`, `output.fps/crf/resolution/source` | `project`, `preset`, `litegs_path`, `jsons_path?`, `fps`, `resolution` |
| 流程 | train → clip → render | train → fuse → render |
| 视频输出 | `output/output.mp4`（实拍+渲染混合） | `renders/<project>.mp4`（纯渲染） |
| docs | `Docs/V5_V6_USAGE.md` | 同 |
| 技术文档 | `Docs/V5_AUTOMATION.md` | 同 |

## 自动化技术栈

- **Playwright** (Python)：浏览器启动/连接、`page.evaluate()` 调用 SuperSplat events API、文件上传（零拷贝 `set_input_files`）
- **OPFS**：视频渲染流式输出，避免 Chrome OOM（4K 视频用 `StreamTarget` 替代 `BufferTarget`）
- **SuperSplat events 总线**：`events.invoke('import')`, `events.invoke('render.video', settings, stream)`

详细原理见 `Docs/V5_AUTOMATION.md`。

## 编码约定

### SuperSplat (TypeScript)
- 事件总线驱动：`events.fire()` / `events.on()` / `events.function()` / `events.invoke()`
- UI 组件：PCUI Container/Label/Button/SelectInput/NumericInput
- 每次改 `src/` 后需 `npm run build`

### Python 管线
- v5/v6 共享代码在 `tills/_shared.py`
- 所有路径通过 `cfg["project"]` 推导（不是 preset 的 path）
- 中间产物存在则跳过，`--force` 强制覆盖
- 断开重跑不加 `--steps` 即可从断开处继续（全幂等）

### 相机索引约定（`tills_ply/interpolate_cameras_*.py`）⚠️
- **`cameras.json` 的 `id` 字段只能当编号显示，绝不能当下标用。** 数组下标（位置）才是唯一可靠的索引：拟合圆的 `positions`、`all_angles`、锚点 B 的 `best_j`、`data[:max_index+1]` 切片全部按位置算。
- 取锚点必须用 `enumerate` 拿位置，**不要**写 `anchor_idx = d["id"]`：
  ```python
  for i, d in enumerate(data):
      if d["img_name"] == anchor_img:
          anchor_idx = i                  # 位置，用于 all_angles[anchor_idx] / data[anchor_idx]
          anchor_file_id = d.get("id")    # 仅用于日志
          break
  ```
- 历史 bug（2026-09-13 修复）：`interpolate_cameras_circle.py` 曾用 `anchor_idx = d["id"]`。当 id 是 1-based 时**静默错位一台相机**（日志看起来完全正常，但 index 0 是 007 而不是 006）；id 稀疏时直接 `IndexError`。当前 LiteGS 产出的 `cameras.json` 恰好 `id == 数组下标`（21 个项目实测全部成立），所以修复前后输出**逐字节相同**。
- 新脚本 `interpolate_cameras_swing.py` 从一开始就用 `enumerate`。日志同时打印 `array_index=` 和 `(file id=)` 便于对账。

### 两条轨迹的起点锚点与帧数（`interpolate` preset 字段）
- `anchor_camera` = **circle**（转一圈）的起始机位；`swing_anchor_camera` = **swing**（摆动）的起始机位。
- `total` = **circle** 的帧数；`swing_total` = **swing** 的帧数（改大 = 摆动更慢更顺）。
- **两个 swing 专属字段留空 / 缺失 / 0 / 纯空白 → 回落到对应的 circle 字段**，因此老 preset 与"只填一个"的用法行为不变。服务器侧的回落写在 `fuse_server.py` 的 `run_fuse_clip`：
  ```python
  circle_anchor = str(ip.get("anchor_camera") or "006")
  swing_anchor  = str(ip.get("swing_anchor_camera") or "").strip() or circle_anchor
  circle_total  = int(ip.get("total") or 300)
  swing_total   = int(ip.get("swing_total") or 0) or circle_total
  ```
  注意 `or` 是必需的——空字符串/0 也要回落，否则会传 `--anchor-camera ""` 或 `--total 0`。
- `--anchor-camera` 与 `--total` **都不在公共参数列表里**：两个 job 各自拼自己的值，脚本 CLI 保持完全对称（两个脚本都只认 `--anchor-camera` / `--total`）。
- **帧数 = pose 数 = 时间轴长度**：`file-handler.ts` 导入时执行 `timeline.setFrames(len(json))`，所以每条 JSON 自带帧数、互不影响，视频时长 = `帧数 / fps`。渲染链路无需任何改动。
- `swing_total` 必须 **≥ 4**（脚本 `N < 4` 直接报错）。`run_fuse_clip` 里**提前校验**，避免跑完 circle 重写完 `cameras_align.json` 才在 swing 步失败、留下半更新的工程。
- ⚠️ `turn_frame` 与 `swing_total` **强耦合**：默认取 `(swing_total−1)/2`，且脚本强制居中（偏离 >0.5 帧就警告并拉回）。**建议 UI 里留空**；显式填写时改 `swing_total` 必须同步改，否则会被静默忽略。
- 锚点或帧数不同时，swing 的起始位置/旋转残差/半径曲线 `r_a`/**内参 fx,fy,width,height**（`--lock-intrinsics` 默认开）全部跟随 swing 锚点 → 同一个 PLY 的两条视频视角会不同，混剪需注意。
- 摆幅上限只有 **359°**（整圈退化）。峰值越过"角向对面"（`X > 2π − span`）时 `_wrap_progress` 会折返，但半径始终被限制在 `[r_a, r_b]` 内、效果仅毫米级（0913 实测 ±359° 时最大偏差 2.3mm / 4.33m），所以**只提示不 clamp**——不要为了这点效应去改用户填的 ±180°。

### v8 Daemon（`tills/server/`）
- `_server.py`：共享的 SSE/HTTP 微框架，两个 daemon 共同 import。**修改 `_server.py` 前确认 train + fuse 两个进程的行为都不会被影响。**
- `Cache-Control: no-cache` 已全局开启。由于页面是服务端渲染（f-string 拼 HTML），不加此 header 浏览器会缓存旧版本 HTML/JS，revert 代码后页面仍用缓存 → 看起来"没修好"。
- **修改 JS 必须一次只改一个区域**（modal / /presets 页 / 主页面逻辑），不要同时改多处。JS 语法错误或运行时异常会导致整个 `<script>` 块停止执行 → 所有事件处理器失效 → 页面"全部无法点击"。
- 改 JS 后先验证 brace 平衡：`python -c "from tills.server.fuse_server import build_fuse_page; page = build_fuse_page(...); 统计 { 和 } 数量"`。

## 文档索引

| 文档 | 内容 |
|------|------|
| `Docs/V5_V6_USAGE.md` | v5/v6 使用手册（pipeline.json 字段、流程、场景选择） |
| `Docs/V5_AUTOMATION.md` | Playwright/OPFS 技术原理、Bug 分析 |
| `Docs/HANDOFF_2026-06-05.md` | 早期交接文档 |
| `PIPELINE.md` | 旧 v1 管线说明 |

*最后更新: 2026-07-10*
