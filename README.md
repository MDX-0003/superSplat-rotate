# SuperSplat Editor

[![Github Release](https://img.shields.io/github/v/release/playcanvas/supersplat)](https://github.com/playcanvas/supersplat/releases)
[![License](https://img.shields.io/github/license/playcanvas/supersplat)](https://github.com/playcanvas/supersplat/blob/main/LICENSE)
[![Discord](https://img.shields.io/badge/Discord-5865F2?style=flat&logo=discord&logoColor=white&color=black)](https://discord.gg/RSaMRzg)
[![Reddit](https://img.shields.io/badge/Reddit-FF4500?style=flat&logo=reddit&logoColor=white&color=black)](https://www.reddit.com/r/PlayCanvas)
[![X](https://img.shields.io/badge/X-000000?style=flat&logo=x&logoColor=white&color=black)](https://x.com/intent/follow?screen_name=playcanvas)

| [SuperSplat Editor](https://superspl.at/editor) | [User Guide](https://developer.playcanvas.com/user-manual/gaussian-splatting/editing/supersplat/) | [Blog](https://blog.playcanvas.com) | [Forum](https://forum.playcanvas.com) |

The SuperSplat Editor is a free and open source tool for inspecting, editing, optimizing and publishing 3D Gaussian Splats. It is built on web technologies and runs in the browser, so there's nothing to download or install.

A live version of this tool is available at: https://superspl.at/editor

![image](https://github.com/user-attachments/assets/b6cbb5cc-d3cc-4385-8c71-ab2807fd4fba)

To learn more about using SuperSplat, please refer to the [User Guide](https://developer.playcanvas.com/user-manual/gaussian-splatting/editing/supersplat/).

## Local Development

To initialize a local development environment for SuperSplat, ensure you have [Node.js](https://nodejs.org/) 20.19.0 or later installed. Follow these steps:

1. Clone the repository:

   ```sh
   git clone https://github.com/playcanvas/supersplat.git
   cd supersplat
   ```

2. Install dependencies:

   ```sh
   npm install
   ```

3. Build SuperSplat and start a local web server:

   ```sh
   npm run develop
   ```

   If your default `node` is older but a newer Node installation exists elsewhere, set `SUPERSPLAT_NODE_HOME` to that installation directory before running the command.

4. Open a web browser tab and make sure network caching is disabled on the network tab and the other application caches are clear:

   - On Safari you can use `Cmd+Option+e` or Develop->Empty Caches.
   - On Chrome ensure the options "Update on reload" and "Bypass for network" are enabled in the Application->Service workers tab:

   <img width="846" alt="Screenshot 2025-04-25 at 16 53 37" src="https://github.com/user-attachments/assets/888bac6c-25c1-4813-b5b6-4beecf437ac9" />

5. Navigate to `http://localhost:3000`

When changes to the source are detected, SuperSplat is rebuilt automatically. Simply refresh your browser to see your changes.

## Python 管线（v8 Daemon）

Python 自动化管线，包含分布式 3DGS 训练调度、PLY 融合/裁剪、SuperSplat 网页渲染。所有 Python 依赖通过 [uv](https://docs.astral.sh/uv/) 管理，声明在 [pyproject.toml](pyproject.toml)。

### 从头配置环境

```powershell
# 1. 安装 uv（如未安装，需重启终端生效）
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. 进入仓库，创建虚拟环境并安装 Python 依赖
cd E:\work\26.7_SKNJ\supersplat
uv sync

# 3. 安装 Playwright 无头浏览器（视频渲染步骤需要）
uv run playwright install chromium
```

之后所有 Python 命令统一使用 `uv run python` 前缀，无需手动激活 venv。

### 新建项目

```powershell
# 一键创建 CameraData/<project>/ 并拷贝模板文件
uv run python -m tills.server.train_daemon init 06
```

自动从 `CameraData/_template/` 拷贝 `pipeline.json` 和 `workers.json`，并将 `pipeline.json` 中的 `project` 改为 `06`。模板文件说明见 [CameraData/_template/](CameraData/_template/)。

> `init` 命令在两个 daemon 中均可使用（`train_daemon` / `fuse_server`）。

### 启动服务

两个后台进程，浏览器实时监控：

```powershell
# 终端 1 — 训练守护进程（自动扫描 raw_images → 分发训练 → 回收 PLY）
uv run python -m tills.server.train_daemon --config CameraData/06/pipeline.json

# 终端 2 — Fuse 服务（PLY 浏览 + interpolate/fuse/clip + Playwright 渲染）
uv run python -m tills.server.fuse_server --config CameraData/06/pipeline.json
```

| 服务 | 端口 | 浏览器 | 功能 |
|------|:---:|------|------|
| Train Daemon | 8080 | `http://localhost:8080` | 训练状态监控、worker 管理 |
| Fuse Server | 8081 | `http://localhost:8081` | PLY 选择 → fuse+clip → 视频渲染 |
| Preset Editor | 8081 | `http://localhost:8081/presets` | 参数预设编辑 |

### 前置条件

- **SuperSplat 前端**：渲染步骤需要本地 SuperSplat 开发服务器运行（`npm run serve`，默认 `http://localhost:3000`）。Fuse Server 面板会显示 npm 状态。
- **SSH 免密登录**：分布式训练需要主机到副机的 SSH 免密配置。
- **workers.json**：需根据实际机器填写 hostname、ip、litegs_path 等。模板中已预填 1 主机 + 4 副机的示例配置。

### 相关文档

| 文档 | 内容 |
|------|------|
| [Docs/HANDOFF_2026_07_10_V8.md](Docs/HANDOFF_2026_07_10_V8.md) | v8 开发交接文档（架构、调试、设计决策） |
| [Docs/v8-daemon-usage.md](Docs/v8-daemon-usage.md) | v8 使用手册（状态机、FAQ） |
| [Docs/v8-daemon-design.md](Docs/v8-daemon-design.md) | v8 架构设计文档 |

---

## Localizing the SuperSplat Editor

The currently supported languages are available here:

https://github.com/playcanvas/supersplat/tree/main/static/locales

### Adding a New Language

1. Add a new `<locale>.json` file in the `static/locales` directory.

2. Add the locale to the list here:

   https://github.com/playcanvas/supersplat/blob/main/src/ui/localization.ts

### Testing Translations

To test your translations:

1. Run the development server:

   ```sh
   npm run develop
   ```

2. Open your browser and navigate to:

   ```
   http://localhost:3000/?lng=<locale>
   ```

   Replace `<locale>` with your language code (e.g., `fr`, `de`, `es`).

## Contributors

SuperSplat is made possible by our amazing open source community:

<a href="https://github.com/playcanvas/supersplat/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=playcanvas/supersplat" />
</a>
