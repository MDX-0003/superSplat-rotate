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

## Python 管线（v7 分布式训练）

本项目包含 Python 自动化管线（`tills/`），支持单机和多机分布式 3DGS 训练。Python 依赖通过 [uv](https://docs.astral.sh/uv/) 管理。

### 环境配置

```powershell
# 1. 安装 uv（如未安装）
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. 创建虚拟环境并安装 Python 依赖
cd E:\work\26.7_SKNJ\supersplat
uv sync

# 3. 安装 Playwright 浏览器（渲染步骤需要）
uv run playwright install chromium
```

依赖声明在 [pyproject.toml](pyproject.toml)，无需手动 `pip install`。

### 运行 v7 分布式训练

```powershell
# 全流程（差分检测 → 分发 → 训练 → fuse → render）
uv run python tills/run_pipeline_v7.py --config CameraData/05/pipeline.json

# 仅训练，指定帧号（支持 frame_id 或完整目录名）
uv run python tills/run_pipeline_v7.py --config CameraData/05/pipeline.json --steps train --frames 122221 151131

# 本地模拟（无需副机，在本机启动 5 个进程模拟分布式）
uv run python tills/run_pipeline_v7.py --config CameraData/05/pipeline.json --steps train --simulate-local
```

### 新建项目

```powershell
# 从模板创建配置
cp CameraData\_template\pipeline.json CameraData\<新项目>\pipeline.json
cp CameraData\_template\workers.json  CameraData\<新项目>\workers.json

# 编辑 pipeline.json   → 修改 project、preset、litegs_path
# 编辑 workers.json    → 修改每台机器的 hostname / ip
```

配置模板和字段说明见 [CameraData/_template/](CameraData/_template/)。

### 相关文档

| 文档 | 内容 |
|------|------|
| [Docs/PLAN/v7-distributed-training.md](Docs/PLAN/v7-distributed-training.md) | v7 分布式训练设计方案 |
| [Docs/PLAN/ssh-setup-guide.md](Docs/PLAN/ssh-setup-guide.md) | SSH 免密配置（前置步骤） |
| [Docs/V5_V6_USAGE.md](Docs/V5_V6_USAGE.md) | v5/v6 管线使用手册 |

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
