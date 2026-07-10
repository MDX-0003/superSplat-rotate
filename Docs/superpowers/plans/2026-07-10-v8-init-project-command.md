# v8 Daemon — init 项目初始化命令

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持 `python -m tills.server.train_daemon init 06` 一键创建 `CameraData/06/` 并填充模板文件。

**Architecture:** 核心逻辑放在 `_server.py`（两个 daemon 共用），两个 daemon 的 `main()` 在 argparse 之前检测 `init` 子命令。

**Tech Stack:** Python 3 stdlib（`sys.argv`, `json`, `shutil`, `Path`）

---

## 需求确认

| 决策 | 选项 |
|------|------|
| 目录已存在 | 报错退出 |
| 修改字段 | `project` + `raw_images_path`（自动生成 `CameraData/06/raw_images`） |
| 入口位置 | `train_daemon` + `fuse_server` 通用（逻辑放 `_server.py`） |
| 批量初始化 | 只支持单个项目 |

---

### Task 1: 在 `_server.py` 中添加 `init_project()` 函数

**Files:**
- Modify: `tills/server/_server.py`（末尾追加函数）

- [ ] **Step 1.1: 添加 `init_project()` 函数**

在 `_server.py` 文件末尾追加：

```python
# ── Project init helper (shared by train_daemon and fuse_server) ──

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "CameraData" / "_template"


def init_project(project_name: str) -> None:
    """Create CameraData/<project_name>/ from _template files.

    Copies pipeline.json and workers.json from the template directory,
    modifying project and raw_images_path in pipeline.json.
    Errors out if the target directory already exists.
    """
    proj_dir = Path(__file__).resolve().parent.parent.parent / "CameraData" / project_name

    if proj_dir.exists():
        print(f"ERROR: {proj_dir} 已存在，如需重新初始化请先删除该目录")
        sys.exit(1)

    proj_dir.mkdir(parents=True)
    print(f"创建目录: {proj_dir}")

    # ── pipeline.json (modify project + raw_images_path) ──
    src_pipeline = _TEMPLATE_DIR / "pipeline.json"
    if src_pipeline.exists():
        with open(src_pipeline, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["project"] = project_name
        cfg["raw_images_path"] = f"CameraData/{project_name}/raw_images"
        dst = proj_dir / "pipeline.json"
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"  复制并修改: pipeline.json (project={project_name})")
    else:
        print(f"  WARNING: 模板文件不存在: {src_pipeline}")

    # ── workers.json (copy as-is) ──
    src_workers = _TEMPLATE_DIR / "workers.json"
    if src_workers.exists():
        dst = proj_dir / "workers.json"
        shutil.copy2(src_workers, dst)
        print(f"  复制: workers.json")
    else:
        print(f"  WARNING: 模板文件不存在: {src_workers}")

    print(f"\n初始化完成: {proj_dir}")
    print(f"  下一步: 编辑 {proj_dir / 'pipeline.json'} 中的 preset、litegs_path 等字段")
    print(f"  启动: python -m tills.server.train_daemon --config {proj_dir / 'pipeline.json'}")
```

> `_server.py` 已经 `import json`、`from pathlib import Path`，需要补充 `import sys` 和 `import shutil`。

- [ ] **Step 1.2: 补充 `_server.py` 缺少的 import**

检查 `_server.py` 顶部，确认是否有 `import sys` 和 `import shutil`。如果没有则添加。

当前 `_server.py` imports（line 1-15）:
```python
import json
import queue
import threading
import time as _time
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
```

缺少 `sys` 和 `shutil`。在 `import json` 之后添加:
```python
import shutil
import sys
```

---

### Task 2: 在 `train_daemon.py` 中添加 `init` 子命令检测

**Files:**
- Modify: `tills/server/train_daemon.py:815-825`（`main()` 开头，argparse 之前）

- [ ] **Step 2.1: 在 argparse 之前检测 `init` 子命令**

在 `main()` 函数中，`parser = argparse.ArgumentParser(...)` 之前插入:

```python
def main():
    # ── init subcommand (before argparse) ──
    if len(sys.argv) >= 2 and sys.argv[1] == "init":
        if len(sys.argv) < 3:
            print("ERROR: init 需要项目名参数")
            print("Usage: python -m tills.server.train_daemon init <project>")
            print("Example: python -m tills.server.train_daemon init 06")
            sys.exit(1)
        from tills.server._server import init_project
        init_project(sys.argv[2])
        return

    parser = argparse.ArgumentParser(description="v8 Train Daemon")
    ...
```

> `sys` 已在 train_daemon.py 中 import（line 17 `import sys`），无需额外添加。

- [ ] **Step 2.2: 更新 docstring 中的 Usage 说明**

修改 train_daemon.py 顶部 docstring（lines 1-10）:

```python
"""
Train Daemon — continuous polling, frame dispatch, training monitor.

Usage:
  # 项目初始化（首次使用）
  python -m tills.server.train_daemon init 06

  # 启动守护进程
  python -m tills.server.train_daemon --config CameraData/06/pipeline.json
  python -m tills.server.train_daemon --config CameraData/06/pipeline.json --port 8080

Start this and leave it running.  Open http://localhost:8080 to monitor.
"""
```

---

### Task 3: 在 `fuse_server.py` 中添加 `init` 子命令检测

**Files:**
- Modify: `tills/server/fuse_server.py`（`main()` 中 argparse 之前）

- [ ] **Step 3.1: 在 argparse 之前检测 `init` 子命令**

在 `fuse_server.py` 的 `main()` 函数中做相同处理。先找到 `main()` 定义:

```python
def main():
    # ── init subcommand (before argparse) ──
    if len(sys.argv) >= 2 and sys.argv[1] == "init":
        if len(sys.argv) < 3:
            print("ERROR: init 需要项目名参数")
            print("Usage: python -m tills.server.fuse_server init <project>")
            print("Example: python -m tills.server.fuse_server init 06")
            sys.exit(1)
        from tills.server._server import init_project
        init_project(sys.argv[2])
        return

    parser = argparse.ArgumentParser(description="v8 Fuse Server")
    ...
```

- [ ] **Step 3.2: 更新 fuse_server.py 顶部 docstring 中的 Usage**

```python
"""
Fuse Server — browser-based PLY selection → fuse+clip → render.

Usage:
  # 项目初始化（首次使用）
  python -m tills.server.fuse_server init 06

  # 启动服务
  python -m tills.server.fuse_server --config CameraData/06/pipeline.json
  python -m tills.server.fuse_server --config CameraData/06/pipeline.json --port 8081
"""
```

---

### Task 4: 验证

- [ ] **Step 4.1: Python 语法**

```powershell
cd e:\work\26.7_SKNJ\supersplat
python -c "import py_compile; py_compile.compile('tills/server/_server.py', doraise=True); print('_server.py OK')"
python -c "import py_compile; py_compile.compile('tills/server/train_daemon.py', doraise=True); print('train_daemon.py OK')"
python -c "import py_compile; py_compile.compile('tills/server/fuse_server.py', doraise=True); print('fuse_server.py OK')"
```

- [ ] **Step 4.2: 手动测试 init 命令**

```powershell
cd e:\work\26.7_SKNJ\supersplat

# 测试 init
python -m tills.server.train_daemon init 99

# 验证生成的文件
ls CameraData/99/
cat CameraData/99/pipeline.json

# 验证 project 字段和 raw_images_path
python -c "
import json
with open('CameraData/99/pipeline.json') as f:
    cfg = json.load(f)
assert cfg['project'] == '99', f'Expected project=99, got {cfg[\"project\"]}'
assert cfg['raw_images_path'] == 'CameraData/99/raw_images', f'Unexpected raw_images_path'
print('pipeline.json 字段验证通过')
"

# 验证重复 init 报错
python -m tills.server.train_daemon init 99
# 预期: ERROR: ...CameraData\99 已存在...

# 清理
rm -r CameraData/99
```

- [ ] **Step 4.3: 提交**

```bash
git add tills/server/_server.py tills/server/train_daemon.py tills/server/fuse_server.py
git commit -m "feat(v8): add 'init <project>' subcommand to both daemons

python -m tills.server.train_daemon init 06
→ creates CameraData/06/ with pipeline.json (project+raw_images_path set)
  and workers.json copied from _template/

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

1. **Spec coverage:** 覆盖所有需求 — init <project> 创建目录、复制模板、修改 project + raw_images_path、目录存在报错、两个 daemon 通用
2. **Placeholder scan:** 无 TBD/TODO
3. **Type consistency:** `init_project(project_name: str)` 参数类型一致，`sys.argv[2]` 是字符串直接传入
4. **共享代码安全:** `init_project` 放在 `_server.py` 末尾，不影响现有 `FileLogger`/`SSEBroadcaster`/`SSEHandler` 等类
