# Pipeline v8 — Daemon 使用指南

> 最后更新: 2026-07-09

---

## 1. 快速启动

```powershell
# 终端 1 — 训练守护进程（自动分发 + 监控 + 回收 PLY）
uv run python -m tills.server.train_daemon --config 06

# 终端 2 — Fuse 服务（PLY 浏览 + fuse/clip + render）
uv run python -m tills.server.fuse_server --config 06
```

| 服务 | 默认端口 | 浏览器 |
|------|:---:|------|
| Train Daemon | 8080 | http://localhost:8080 |
| Fuse Server | 8081 | http://localhost:8081 |

---

## 2. pipeline.json 配置

```jsonc
{
  "project": "05",
  "preset": "xxx",

  // 帧图片来源目录（可选，默认 CameraData/<project>/raw_images/）
  "raw_images_path": "E:/work/26.7_SKNJ/0630_test_imgs",

  // 每帧期望的图片数量（可选，不填则从目录名前缀解析）
  // 例：目录名 "120-2026-06-30-120849" → 前缀 120 = 期望 120 张
  "img_num": 120,

  // 扫描间隔（秒），默认 5
  "poll_interval": 5,

  // 分布式配置（同 v7）
  "distributed": {
    "enabled": true,
    "workers_config": "workers.json",
    "training": {
      "iterations": 25000
    }
  },

  // fuse/render 配置
  "fps": 25,
  "litegs_path": "E:/work/26.7_SKNJ/LiteGSWin"
}
```

---

## 3. 帧状态机（完整流转图）

```
帧目录在 raw_images/ 下出现
  │
  ▼
┌──────────┐  数=预期?    ┌───────────┐  连续两轮快照一致?   ┌───────┐
│ checking │──────────────│ checking   │────────────────────│ ready │
│ (首轮)   │   snapshot1  │ (等待稳定) │  snapshot1==snap2  │       │
└──────────┘              └───────────┘                    └───┬───┘
     │ 数≠预期                                                   │
     │ 或首轮快照无对比基准                                       │
     │                                                           ▼
     │                                                    ┌──────────┐
     │                                                    │ copying  │ 拷贝到 worker
     │                                                    └────┬─────┘
     │                                                         │
     │                                                         ▼
     │                                                    ┌──────────┐
     │                                                    │ training │ 并行训练
     │                                                    └────┬─────┘
     │                                     exit 0 + PLY 回收    │  exit≠0
     │                                     ────────────────────┼──────────
     │                                                         │
     │                                                        ▼
     │                                                    ┌───────┐
     │                                             重试1次 │ ready │
     │                                                    └───┬───┘
     │                                         exit≠0 again   │
     │                                                        ▼
     │                                                    ┌────────┐
     │                                                    │ failed │──── 用户点 [清理]
     │                                                    └────────┘     │
     │                                                                   ▼
     │                          ┌───────┐                          ┌─────┐
     └─ 目录消失(hard删除) ──→  │ 移除  │                          │ new │──→ checking ...
                                └───────┘                          └─────┘

┌──────┐  用户点[清理]  ┌─────┐
│ done │───────────────→│ new │──→ checking ...
└──────┘                └─────┘
     ↑
     │  exit 0 + PLY 回收成功
     │
  training
```

### 状态含义速查

| 状态 | 含义 | daemon 行为 |
|------|------|------------|
| `new` | 刚通过 Web UI 清理重置 | 下一轮进入双采样 |
| `checking` | 正在等待文件拷贝稳定 | 每轮对比快照 |
| `ready` | 文件已就绪，等待分发 | 下一轮分配给 worker |
| `copying` | 正在拷贝到 worker | — |
| `training` | 训练进行中 | 读 status JSON + 流式读 stdout |
| `done` | 训练完成，PLY 已回收 | PLY 存在 → 跳过；PLY 被删 → 重新检测 |
| `failed` | 训练失败 | 不自动重试，等待用户 [清理] |

---

## 4. 帧就绪判定逻辑（伪代码）

```
每 5 秒（poll_interval）扫描 raw_images/ 下的所有子目录:

for each fd in raw_dirs:
    key = parse(fd.name)          // "120-2026-06-30-120849" → key="0630-120849"
    
    // 第一层：跳过已知完成的
    if status == "training":       跳过
    if status == "done":
        if PLY 存在:               跳过（真正的完成）
        if PLY 不存在:             重置为 "checking"（用户删了 PLY）
    
    // 第二层：PLY 已存在 → 直接完成
    if CameraData/<proj>/<key>.ply 存在 and not --force:
        标记 "done"; 跳过
    
    // 第三层：双采样稳定性检测
    cur = snapshot(fd)             // (文件数, 文件名集合, 每个文件的字节数)
    prev = 上一轮存的快照
    
    期望数量 = img_num or 目录名前缀N
    
    if 首次发现:
        记录快照; 状态="checking"
    elif 状态 in ("new", "checking"):
        if cur.count != 期望数量:
            不训（还在拷贝中）
        elif prev == None:
            不训（等下一轮才有对比基准）
        elif cur != prev:
            不训（文件还在写入/变化）
        else:
            状态="ready"  ← 就绪！下一轮分发
```

**关键时间线**：帧目录拷贝完成 → 第一轮扫描（记录快照）→ 等 5s → 第二轮扫描（对比）→ 如果一致 → READY。**所以最快需要 2 个扫描周期（~10 秒）才能被检测为就绪。**

---

## 5. Web UI 操作

### Train 面板 (port 8080)

| 操作 | 按钮 | 效果 | 适用状态 |
|------|------|------|:---:|
| 停止训练 | `[停止]` | 立即 kill 进程树（含子进程），移除 Popen | training |
| 清理 soft | `[清理 soft]` | 删除 PLY + worker results/data，**保留** raw_images | training / done / failed |
| 清理 hard | `[清理 hard]` | 清理 soft 的全部 + **删除** raw_images 对应帧目录 | training / done / failed |

**清理后的行为**：
- **soft 删除**：PLY 已消失，下一轮扫描重新检测 → checking → ready → 重新分发训练
- **hard 删除**：raw_images 目录也消失，帧从列表移除（需 F5 刷新网页）

### Fuse 面板 (port 8081)

| 操作 | 按钮 | 效果 |
|------|------|------|
| fuse + clip | `[fuse + clip 选中]` | 对选中的 PLY 执行 fuse_ply.py → 自动接 clip_ply.py |
| render | `[render 选中]` | 对第一个选中的 PLY 执行 Playwright 渲染 |

**默认全不勾选**，长程任务下通常只需手动选最新的 2-3 个 PLY。

---

## 6. 常见困惑点

### 6.1 "为什么新帧放进去很久还没开始训练？"

```
帧拷贝  →  第一轮扫描（首轮快照）→  等 5s  →  第二轮扫描（对比）→  READY  →  分发
├─ t0 ──┼────── t0+5s ──────────┼── t0+10s ───────────────────┼── t0+10s ─────────┤
```

**最少 10 秒**（两个扫描周期）。如果拷贝还在进行中（文件数不够或文件大小在变），会一直等。

**排查**：看终端输出——
- `[scan] NEW 0630-120856 — 80 files, expect 120` → 文件数不够，还在拷贝
- `[scan] 0630-120856 — 120/120 files, not yet stable` → 文件数够了但大小在变
- `[scan] READY 0630-120856 — 120 files stable` → 已就绪

### 6.2 "点了停止，日志还在继续输出"

```
stop 按钮 → kill PID（远程进程）→ terminate Popen（本地壳进程）→ stdout 管道 EOF

如果管道中还有缓冲数据，reader 线程会读完再退出——可能需要几秒。
如果日志持续输出超过 5-10 秒，说明进程树没有被完全杀死（检查 taskkill /t 是否生效）。
```

### 6.3 "清理后状态没变 / 卡在 done"

```
旧行为（已修复）:
  清理 → 文件删了 → TrainState 状态没变 → 扫描跳过 done 帧 → 永远不变

当前行为:
  清理 → 文件删了 + status 重置为 "new" → 扫描检测到 done + PLY 不存在 → 重置
  删除后状态变更会在下一个扫描周期（5s 内）生效。网页需 F5 刷新才显示新状态。
```

### 6.4 "hard 删除后网页还显示该帧"

```
hard 删除 → raw_images 目录被删 → state.frames 中移除该帧

但网页是服务端渲染的——SSE 只更新已有行，不删行。
需要按 F5 刷新网页，行才会消失。
```

### 6.5 "worker 的训练 log 迟迟不出现"

```
训练启动 → stdout reader 线程开始读 → 但 SSH 连接可能需几秒建立
         → Python/batch_run.py 启动可能需几秒
         → COLMAP 阶段可能无明显输出

这种情况稍等即可。如果 1 分钟后仍无输出，用 SSH 手动登录 worker 检查。
```

### 6.6 "failed 后为什么不自动重试？"

```
failed 意味着训练本身出了问题（脚本崩溃、显存不足、数据损坏等）。
自动重试不会修复根本原因，反而造成无限循环。

用户应:
  1. 查看 worker 的 .log 文件定位失败原因
  2. 修复问题后 → 点 [清理 soft] → 帧重置为 new → 重新分发训练
```

### 6.7 "网页刷新和 SSE 实时推送各负责什么？"

```
首次打开 / F5 刷新:
  → GET /  → build_page() → 完整 HTML 重新渲染
  → 新建 EventSource('/events') → SSE 实时推送

SSE 推送期间:
  → status 事件: 更新已有行的状态/Worker/迭代字段（不增删行）
  → log 事件:    追加文本到对应 Worker 的日志面板

SSE 不会做的事:
  → 增删表格行（hard 删除后需 F5）
  → 改变操作按钮（状态变更后按钮文本不会自动更新）
```

### 6.8 "日志文件在哪里？"

```
每次启动 daemon 自动创建:
  CameraData/<proj>/logs/daemon-<YYYYMMDD-HHMMSS>/
  ├── daemon.log    # 扫描结果、分发/回收/失败事件、心跳
  ├── host.log      # 本机 worker 训练 stdout
  └── worker1.log   # 远程 worker 训练 stdout（按 workers.json 中的 id 命名）
```

### 6.9 "PLY 回收失败 / 回收后 PLY 大小为 0"

```
回收流程:
  batch_run.py exit 0
  → daemon 读取 results/<sub_dir>/<sub_dir>-<frame_id>.ply
  → 拷贝到 CameraData/<proj>/<key>.ply
  → 标记 done

可能的失败原因:
  - batch_run.py 说 "output already exists"（残留产物）
    → daemon 自动检查 PLY，不存在则日志显示 "回收失败: PLY 不存在"
  - 训练真的失败了但 batch_run.py exit 0（罕见）
    → PLY 可能存在但为空文件
```

### 6.10 "daemon 启动后 Worker 全部离线怎么办？"

```
daemon 不会退出。持续等待直到至少一个 Worker 上线。
终端和日志会显示 "无可用 Worker"。
Worker 上线后会自动开始扫描分发。
```

---

## 7. 终端输出速查

| 终端输出 | 含义 |
|---------|------|
| `[scan] NEW 0630-120849 — 80 files, expect 120` | 首次发现，文件数不足 |
| `[scan] 0630-120849 — 120/120 files, not yet stable` | 数量够但快照未稳定 |
| `[scan] READY 0630-120849 — 120 files stable` | 就绪，等待分发 |
| `[dispatch] 0630-120849 → host` | 即将分发到 host worker |
| `[scan #N] 6 dirs \| ready=1 training=2 done=3 failed=0` | 周期心跳 |
| `RE-CHECK 0630-120849 — PLY deleted, will re-detect` | PLY 被删除，重新检测 |
| `REMOVED 0630-120849 — raw_images directory gone` | hard 删除，帧移除 |

---

## 8. 典型工作流

```
场景: 共享盘新来了 3 帧图片，需要训练 → fuse → render

1. 确保两个 daemon 在运行
2. 在浏览器打开两个面板
3. 等待 Train 面板显示 "done"（观察终端或网页上的扫描心跳）
4. 在 Fuse 面板勾选最新的 2-3 个 PLY → [fuse + clip 选中]
5. 等待 fuse+clip 完成（看日志面板）
6. 勾选生成的 combine_*.ply → [render 选中]
7. 等待渲染完成 → renders/ 目录下拿到 MP4
```

**如果某帧训练失败**：
1. 点击该帧的 `host.log` / `worker1.log` 查看错误
2. 修复问题
3. 点击 `[清理 soft]` 重置
4. 下一轮自动重新分发训练
