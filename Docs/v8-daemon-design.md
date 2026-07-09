# Pipeline v8 — Daemon 架构设计

> **状态：** 设计已确认，待开发实现
> **日期：** 2026-07-09

---

## 1. 设计目标

将 v7 的一次性 CLI 训练流程改造为**两个持续运行的后台进程**，适配"实时帧到达→即时分发训练→训练完成后用户可选 fuse+render"的工作流。

### 核心原则

- **能和现有代码整合就不新开文件。** v6/v7 保持不变，新逻辑通过 import 复用，不重写已工作的代码。
- **不做过度设计。** 不做回退机制里的"降级策略"、不做根本用不上的 fallback 代码、不做身份认证、不做数据库。
- **一次只做一件事。** train-daemon 只管训练分发回收，fuse-server 只管 fuse/clip/render 调度。两者通过文件系统（PLY 存在性）解耦。

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                    Pipeline v8                                   │
│                                                                  │
│  ┌──────────────────────┐        ┌──────────────────────┐       │
│  │   train_daemon.py     │        │   fuse_server.py      │       │
│  │   (端口 8080)          │        │   (端口 8081)          │       │
│  │                       │        │                       │       │
│  │  轮询 raw_images/      │  PLY   │  轮询 *.ply            │       │
│  │  → 就绪检测            │ ────→ │  → 展示列表            │       │
│  │  → 分发到 Worker       │ 文件   │  → 用户选择 fuse+clip  │       │
│  │  → SSH 并行训练        │ 系统   │  → 用户触发 render     │       │
│  │  → 回收 PLY            │       │                       │       │
│  │                       │        │                       │       │
│  │  Web UI:              │        │  Web UI:              │       │
│  │  状态面板 + Worker日志  │        │  PLY列表 + 操作面板    │       │
│  └──────────────────────┘        └──────────────────────┘       │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  server/_server.py  ← 共享极简 HTTP/SSE 框架                │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  复用 (不动):                                              │   │
│  │  _distributed.py   WorkerNode / SSH / SCP / status / PID  │   │
│  │  _shared.py        ROOT / preset / parse / Playwright      │   │
│  │  run_pipeline_v6.py  fuse+clip / render 函数              │   │
│  │  run_pipeline_v7.py  保留向后兼容                          │   │
│  │  tills_ply/          fuse_ply.py / clip_ply.py (不改)     │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 进程职责边界

| | train_daemon | fuse_server |
|------|:---:|:---:|
| 扫描 raw_images | ✅ | ❌ |
| 帧就绪检测 | ✅ | ❌ |
| 分发到 Worker + SSH 训练 | ✅ | ❌ |
| 监控训练进度 + 日志流 | ✅ | ❌ |
| 回收 PLY | ✅ | ❌ |
| 停止训练 + 清理（soft/hard） | ✅ | ❌ |
| 扫描可用 PLY | ❌ | ✅ |
| fuse + clip | ❌ | ✅ |
| Playwright render | ❌ | ✅ |
| 独立启动/停止 | ✅ | ✅ |

---

## 3. 文件结构

```
tills/
├── _shared.py                    # 不动
├── _distributed.py               # 小改：+check_frame_ready(), +kill_worker_process(), +cleanup_frame()
├── run_pipeline_v6.py            # 不动
├── run_pipeline_v7.py            # 不动
└── server/                       # 新增
    ├── _server.py                # 共享极简 HTTP/SSE server（~150 行）
    ├── train_daemon.py           # train daemon 入口（~300 行）
    └── fuse_server.py            # fuse server 入口（~200 行）
```

### _distributed.py 新增函数

| 函数 | 职责 |
|------|------|
| `check_frame_ready(frame_dir, expected_count)` | 双采样稳定性检测：两次 `os.listdir` + 文件数/列表/size 对比 |
| `kill_worker_process(worker, status_path)` | 从 status JSON 读取 PID → `taskkill /f /pid`（远程 SSH）或 `os.kill`（本地） |
| `cleanup_frame(worker, sub_dir, frame_id, level)` | 按 soft/hard 级别清理指定帧的训练产物（best-effort，文件不存在静默跳过） |

### batch_run.py 改造（LiteGSWin 仓库）

启动时内联写入 PID 到 worker status JSON（3 行代码，不引入跨仓库 import）：
```python
# 原子覆盖写入，每次启动清空旧状态
status = {"pid": os.getpid(), "status": "running", ...}
tmp = status_path.with_suffix(".tmp")
with open(tmp, "w") as f: json.dump(status, f)
tmp.replace(status_path)
```

---

## 4. pipeline.json 扩展

```jsonc
{
  "project": "05",
  "preset": "xxx",

  // 新增：可配置的 raw_images 路径（不再硬绑 CameraData/<project>/raw_images/）
  // 如果未设置，fallback 到 CameraData/<project>/raw_images/
  "raw_images_path": "E:/work/26.7_SKNJ/supersplat/CameraData/05/raw_images",

  // 新增：帧目录下期望的图片数量（与目录名前缀取交集验证）
  // 如果帧目录前缀是 "120-..."，要求 图片数量 ∈ {预期数量, 前缀N}
  "img_num": 120,

  // 分布式配置（同 v7）
  "distributed": {
    "enabled": true,
    "workers_config": "workers.json",
    "training": {
      "iterations": 30000,
      "target_primitives": null,
      "frame_stride": null
    }
  },

  // fuse/render 配置（同 v6）
  "fps": 25,
  "resolution": [3840, 2160]
}
```

---

## 5. train_daemon 核心流程

### 5.1 主循环

```
while True:
    for each project in tracked_projects:
        1. 扫描 raw_images/ 下所有帧目录
        2. 对每个帧目录:
           - 已就绪 + PLY 不存在 → 加入分发队列
           - 未就绪 → 执行双采样检测，通过则标记"就绪"
           - PLY 已存在 → 跳过
        3. 分发队列非空 → round-robin 分配给在线 Worker
        4. 对每个正在训练的帧:
           - 读取 worker status JSON → 更新进度
           - Popen.poll() 检查进程是否存活
           - 训练完成 → 回收 PLY → 标记完成
           - 进程意外退出 → 重试分发一次 → 仍失败则标记 FAILED
        5. 处理用户操作队列（停止/清理）
    sleep(5)
```

### 5.2 帧就绪检测算法

```
输入: frame_dir (Path), expected_count (int 或 None)
输出: is_ready (bool)

// 第 1 次采样
files_1 = list(frame_dir.iterdir())
count_1 = len(files_1)
sizes_1 = {f.name: f.stat().st_size for f in files_1}

// 等待稳定窗口
sleep(5)

// 第 2 次采样
files_2 = list(frame_dir.iterdir())
count_2 = len(files_2)
sizes_2 = {f.name: f.stat().st_size for f in files_2}

// 三重校验
return (
    count_1 == count_2                          // 文件数稳定
    and set(sizes_1.keys()) == set(sizes_2.keys())  // 文件列表未变
    and sizes_1 == sizes_2                      // 每个文件 size 未变
    and (expected_count is None or count_1 == expected_count  // 满足预期数量
         or count_1 == 目录名前缀N)
)
```

### 5.3 训练启动

- 复用 v7 的 `ssh_run_async()` + `batch_run.py` 调用方式
- 状态文件写到 worker 的 `results/<sub_dir>/_worker_status.json`
- 日志通过 `Popen.stdout` 逐行读取，写入内存环形缓冲区（每 worker ~500 行）

### 5.4 PLY 回收

- `Popen.poll()` 返回 0 → 训练完成 → `scp_recv_multi()` 回收 PLY
- 返回非 0 → 训练失败 → 自动重试一次（重新分发到其他 Worker）
- 回收完成后用文件系统通知 fuse-server（无需主动通知——fuse-server 自己轮询）

### 5.5 停止 + 清理流程

```
用户点击 [停止] + 选择 soft/hard:
  1. 读取 worker status JSON → 获取 PID
  2. SSH "taskkill /f /pid <pid>"（远程）或 os.kill(pid)（本地）
     - 进程不存在 → 静默跳过
  3. 清理文件（best-effort）:
     soft:
       - supersplat CameraData/<proj>/<sub_dir>-<frame_id>.ply
       - worker results/<sub_dir>/
       - worker data/<sub_dir>/<frame_dir>/
     hard:
       - soft 的全部
       - supersplat raw_images/<frame_dir>/
  4. 清理时文件不存在 → 静默跳过，继续下一个
```

---

## 6. fuse_server 核心流程

### 6.1 主循环

```
while True:
    1. 扫描 CameraData/<proj>/*.ply → 按时间排列
    2. 对比上次快照，有变化 → SSE 推送更新
    3. 处理用户操作队列:
       - POST /fuse   → {indices: [1,2,3]} → 执行 fuse_ply.py → 自动接 clip_ply.py
       - POST /render → {ply_name: "..."}  → 执行 Playwright 渲染
    4. 同一时间只允许一个任务（fuse+clip 或 render）
    sleep(5)
```

### 6.2 fuse+clip 流程

- 复用 v6 的 `fuse_ply.py` + `clip_ply.py` 参数拼接逻辑
- stdout 通过 SSE 实时推送
- fuse 成功后自动执行 clip（同 v6 行为）
- 新产生的 combine PLY 自动出现在列表中

### 6.3 render 流程

- 复用 v6 的 `async_main_v6()` Playwright 逻辑
- 用户选择 PLY（可以是原始 PLY 或 combine PLY）→ 触发渲染
- 渲染进度通过 SSE 推送

---

## 7. Web UI 设计

### 7.1 共享技术栈

- **渲染**：服务端生成 HTML（f-string 拼 HTML），零前端依赖
- **实时推送**：SSE（Server-Sent Events），Python 标准库手写 `text/event-stream`
- **操作触发**：HTTP POST 表单 + `fetch()`
- **样式**：内联 CSS，不引入 CSS 框架
- **_server.py**：两个 daemon 各实例化一个，配置不同路由，共享 SSE/静态文件逻辑

### 7.2 Train 面板 (localhost:8080)

```
┌─ v8 Train Daemon — project: 05 ──────────────────────────────────┐
│  Worker: 2/2 在线  |  轮询间隔: 5s                                │
├──────┬──────────┬────────┬──────────────┬────────────────────────┤
│ 帧号  │ 状态     │ Worker │ 迭代         │ 操作                   │
├──────┼──────────┼────────┼──────────────┼────────────────────────┤
│120849│ 训练中 ▸ │ host   │  5000/30000  │ [停止] [清理 soft▾]     │
│120850│ 就绪     │ —      │  —           │               [清理 ▾]  │
│120851│ ✓ 完成   │worker1 │  30000/30000 │               [清理 ▾]  │
│120852│ ❌ 失败  │worker1 │  12000/30000 │ [重试]        [清理 ▾]  │
├──────┴──────────┴────────┴──────────────┴────────────────────────┤
│                                                                    │
│  ▸ host 日志 (120849)                                    [自动滚动] │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ [14:22:01] Starting frame 120849...                          │ │
│  │ [14:22:05] iter 1000/30000  loss=0.0032  psnr=28.4          │ │
│  │ [14:22:10] iter 2000/30000  loss=0.0028  psnr=29.1          │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                    │
│  ▾ worker1 日志 (已完成)                                           │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ [14:20:00] Starting frame 120851...                          │ │
│  │ [14:35:00] Training complete.                                │ │
│  └──────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────┘
```

### 7.3 Fuse 面板 (localhost:8081)

```
┌─ v8 Fuse Server — project: 05 ────────────────────────────────────┐
│  可用 PLY: 5 个  |  状态: 空闲                                     │
├────┬──────────────────────────┬──────────┬────────────────────────┤
│ ☐  │ 0703-120849.ply          │ 245 MB   │ 07-03 14:22            │
│ ☐  │ 0703-120850.ply          │ 251 MB   │ 07-03 14:35            │
│ ☐  │ 0703-120851.ply          │ 238 MB   │ 07-03 14:40            │
│ ☐  │ combine_0703-120849.ply  │ 480 MB   │ 07-03 14:50 (fused)    │
│ ☐  │ 0628-102230.ply          │ 220 MB   │ 06-28 10:25            │
├────┴──────────────────────────┴──────────┴────────────────────────┤
│  默认全不勾选，手动选择最新 2-3 个。                                │
│  [fuse + clip 选中]  [render 选中]                                 │
├───────────────────────────────────────────────────────────────────┤
│  任务日志:                                                         │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ [14:50:00] fuse_ply.py --path CameraData/05 --indices 1 2    │ │
│  │ [14:50:05] Loading 0703-120849.ply (245 MB)...               │ │
│  │ [14:50:30] Fuse complete → combine_0703-120849.ply (480 MB)  │ │
│  │ [14:50:31] clip_ply.py --path CameraData/05 ...              │ │
│  │ [14:51:00] Clip complete → 05-clip/                          │ │
│  └──────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────┘
```

---

## 8. 边界情况处理

| 场景 | 行为 |
|------|------|
| Daemon 启动时 Worker 全部离线 | 持续等待，UI 显示"无可用 Worker"，直到至少一台上线 |
| 训练中 Worker 离线 | SSH 连接断开 → 自动标记该帧 FAILED → 重新分发给其他在线 Worker（最多重试 1 次） |
| 分发阶段 Worker 离线 | 跳过该 Worker，帧分配给其他在线 Worker |
| Daemon 崩溃重启 | 无状态恢复：重新扫描，PLY 不存在 → 重新分发训练（已分发但未完成的帧可能被重训，后果可接受） |
| 同一帧拷贝到一半被扫描到 | 双采样稳定性检测拦截——两次采样间文件数/size 未稳定，等待下次循环 |
| PID 杀进程时进程已退出 | 静默跳过，不阻塞后续清理操作 |
| 清理时文件不存在 | 静默跳过（best-effort），继续清理下一个文件 |
| fuse-server 启动时已有 PLY | 正常扫描并展示，不影响 |
| train-daemon 和 fuse-server 同时操作 PLY | fuse-server 读 PLY 不修改原文件（产生新 combine PLY），train-daemon 只写新 PLY 不删除已存在的——不存在竞态 |
| Worker 上同时跑多个训练 | PID 写到独立 status 文件，kill 时按文件精准匹配 |

---

## 9. 设计决策记录

| 决策 | 选择 | 替代方案 | 理由 |
|------|------|---------|------|
| 进程模型 | 两个独立 daemon（train + fuse） | 单 daemon 内部分状态机 | 解耦、独立重启、各自无状态 |
| 进程间通信 | 文件系统（PLY 存在性） | TCP/pipe/消息队列 | 零依赖、天然持久、已有约定 |
| 帧就绪检测 | 双采样稳定性（count + size） | watchdog 文件事件 / touch 标记文件 | 拷贝方无配合能力，双采样是最可靠的被动检测 |
| Web 实时推送 | SSE | WebSocket / HTTP 轮询 | 标准库即可实现、浏览器自动重连、够用 |
| HTTP 框架 | 自建极简 (_server.py) | Starlette/FastAPI/Flask | 零依赖、总量 ~150 行、两个 daemon 共享 |
| 进程终止 | PID 精确杀 | 整机杀 / wmic 匹配 | 安全、无误杀、batch_run.py 只需 3 行改动 |
| Daemon 状态持久化 | 无状态（重启重评估） | daemon_state.json | PLY 存在性已是天然状态，无需额外文件 |
| 重试策略 | 原始分发 + 最多 1 次重试 | 无限重试 / 不重试 | 避免坏帧死循环，允许人工介入 |
| 日志存储 | 内存环形缓冲区（500 行/worker） | 磁盘文件 / 无保留 | 不写磁盘、不无限增长、够排查问题 |
| Fuse 默认勾选 | 全不勾选 | 全勾选 | 长程任务下 PLY 很多，通常只需最新 2-3 个 |
| Fuse 后自动接 clip | 是 | 否 | clip 是 fuse 的自然延续，v6 已有此行为 |
| Render 是否接在 fuse 后 | 否，单独触发 | 是 | render 耗时长，用户可能想先检查 fuse 结果 |
| v6/v7 改动 | 不动 | 原地重构 | 向后兼容，保留一次性 CLI 工具可用 |
| batch_run.py 改造 | 内联 3 行原子写入 | import supersplat | 避免跨仓库依赖 |
