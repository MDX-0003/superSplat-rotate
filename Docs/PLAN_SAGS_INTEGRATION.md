# SAGS 后处理融入 v8 Train Daemon — 实施计划

> 状态: **Phase 1/2 已完成（代码级验证通过）** · Phase 3 仅剩真实环境端到端（GPU 实跑） · 2026-08
> 相关讨论: LiteGSWin / supersplat / SAGS 三项目关系梳理，决策点已全部确认。
> 进度：Phase 1（状态机/派发/回收/最小 UI）✅ 已实现并通过 py_compile + 页面渲染 + action 逻辑 +
> daemon 冒烟测试（启动/页面/API/入队→探测→失败链路）；Phase 2 ✅（模板同步、sags 参数节、文档）；
> Phase 3 待真实环境验证（见第 5 节验证方案 2/3/4/6）。

---

## 0. 背景与目标

- LiteGSWin 负责训练：`raw_images` 帧 → 每帧 `{MMDD}-{HHMMSS}.ply` + 每帧 COLMAP `sparse/0`；
- train_daemon(8080) 派发训练到各 worker（host/remote），回收 PLY 到 `CameraData/<proj>/`；
- fuse_server(8081) 做 interpolate→fuse→clip→render（**本次零改动**，`-sags.ply` 会被 `scan_all` 自动发现）；
- SAGS（每台机器都部署、路径一致）是帧级后处理：`{frame}.ply + sparse/0 → {frame}-sags.ply`（演员高斯）。

目标：**SAGS 并入 train_daemon 的派发状态机**，与 liteGS 共用"每台机器同时只跑一个 GPU 任务"的互斥约束；训练中可标记"这帧需要 SAGS"，帧训练完自动在同一机器串行跑；结果自动回收 `CameraData/<proj>/{frame}-sags.ply`，与原 PLY 并存。

## 1. 已确认的决策（不变量）

| # | 决策 |
|---|---|
| D1 | SAGS 触发入口**只在 train_daemon 页面**（帧表格每行 🎭 按钮）；fuse_server 不改代码 |
| D2 | **SAGS 绑定该帧的训练机**（持有 `data/<sub>/<frame>/sparse/0` 与 results ply 的机器），不跨设备搬中间文件；liteGS 是唯一可自由找机器派发的任务 |
| D3 | 一台机器同时只跑一个 GPU 任务（liteGS 帧 或 SAGS）；SAGS 占用某机器时，liteGS 派发自动绕开该机器（"去其他机器找空闲"） |
| D4 | SAGS 结果 `{frame}-sags.ply` 回收进 `CameraData/<proj>/`，**保留原 PLY，两者并存** |
| D5 | 失败/不含人（`report.status != "ok"`）→ 不回收、明确报错；帧数据已清理 → 报错（不跨设备搬数据）；清理帧时自动取消其排队项；允许重跑覆盖 |
| D6 | SAGS 固定默认参数：`--view-count 12 --max-actors 2 --speed balanced`（约 90s/帧），可经 pipeline.json `sags` 节覆盖 |
| D7 | `workers.json` 每个 worker 加 `sags_path`（各机路径一致，如 `C:/SAGS`），与 `litegs_path` 并列 |
| D8 | 命名：`-sags` 后缀自然进入合成命名（fuse_ply.py 的 `_output_prefix_and_labels` 已验证），如 `0805-combine-140403-150732-sags.ply`；fuse→clip→render 全兼容 |

## 2. 架构与数据流

```
FrameState 新增: sags_status(none/queued/running/done/failed) + sags_error

帧表格新列 SAGS:
  none    → [🎭 排队]         done    → [✓] + [🎭 重跑]
  queued  → [排队中] + [取消]  failed  → [✗] + [🎭 重跑] (title=错误)
  running → [SAGS运行中]

主循环每周期（5s）:
  ① liteGS 派发:
     worker 可用 = 在线 ∧ 训练帧数<max_per_worker ∧ 该机无 queued/running 的 SAGS
     → SAGS 占用的机器自动被 liteGS 绕开
  ② SAGS 派发:
     遍历 sags_status=="queued" 的帧:
       目标机 = frame.worker_id(在线则用, 否则探测)   ← 探测: ssh if exist data/<sub>/<frame>/sparse/0
       目标机空闲(无训练帧 ∧ 无其他 SAGS) → 启动 SAGS, 标记 running
       目标机忙 → 本周期跳过(继续排队); 目标机数据不存在 → failed("该帧训练数据已清理/不在线")
  ③ SAGS 监控:
     进程 exit 0 → ssh 读 <sags_path>\result\<sub>\report.json → status=="ok"
        → 回收 {frame}-sags.ply → done
     否则 → failed + sags_error(读 report/reason)
```

worker 上执行的命令（**复用 `ssh_run_async(worker, cmd)`，host 走 `shell=True`，remote 走 `_build_ssh_cmd`**）：

```
cd /d "<sags_path>" && "<sags_path>\.venv\Scripts\python.exe" scripts\segment_humans_sags.py
  "<litegs>\results\<sub>\{frame}.ply"
  --colmap-sparse "<litegs>\data\<sub>\<frame_dirname>\sparse\0"
  --output-dir "<sags_path>\result\<sub>"
  --view-count 12 --max-actors 2 --speed balanced
```

## 3. 逐文件改动清单

### 3.1 `tills/_distributed.py`（约 5 行）
- `WorkerNode` dataclass 增加字段 `sags_path: str = ""`；
- `load_workers()` 读取 `w.get("sags_path", "")`。

### 3.2 `tills/server/train_daemon.py`

**a) `FrameState`（L50 附近）**
- 新增字段：`sags_status: str = "none"`、`sags_error: str = ""`。
- `to_dict()` 输出这两个字段（SSE 用）。

**b) `handle_action`（L415 附近）**
- 新增 `action == "sags_enqueue"`：需要 `key`；帧必须存在；状态在 `none/done/failed` 时允许入队（重跑覆盖，D5）；`running` 拒绝（提示先等完成）；置 `sags_status="queued"`，广播状态。
- 新增 `action == "sags_cancel"`：仅 `queued` 可取消（置回 `none`）；`running` 拒绝（约 90s，等完成）。
- 清理（`clean`）交互（D5）：若该帧 `queued` → 先自动取消并记日志；若 `running` → 拒绝清理并提示。

**c) `main_loop` —— liteGS 派发段（L965 附近）**
- 计算 `available` worker 时，排除"该机存在 `queued`/`running` 的 SAGS 帧"的机器（D3）。约 5 行。

**d) `main_loop` —— 新增 SAGS 派发段（插在 liteGS 派发之后）**
- 遍历 `state.frames` 中 `sags_status=="queued"` 的帧：
  1. 解析 `sub_dir/frame_id/dirname`（frame 上已有）；
  2. 目标机定位：`frame.worker_id` 非空且该 worker 在线 → 用之；否则探测在线 worker（优先 host）：`ssh_run(w, 'if exist "<litegs>\data\<sub>\<dirname>\sparse\0" (echo OK)')`；
  3. 目标机忙（有 `training` 帧 或 有 `running` 的 SAGS）→ 跳过本周期；
  4. 启动：`ssh_run_async(worker, sags_cmd)`；`sags_status="running"`；stdout 用独立 reader 线程逐行 `_emit_log(worker.id, line)`（与训练日志同机制，落到该 worker 日志面板）；
  5. 记录到独立字典 `state.sags_processes[key] = (worker, proc)`（**不复用** `running_processes`，避免与训练监控混淆）。

**e) `main_loop` —— 新增 SAGS 监控段（与训练监控并列）**
- 每周期 `proc.poll()`：
  - 仍运行 → 跳过；
  - exit 0 → `ssh_run` 读 `<sags_path>\result\<sub>\report.json`（`if exist ... type ...`，参考 `read_worker_status`）→ 解析 `status`：
    - `"ok"` → 回收：host `shutil.copy2`（tmp+rename 原子写，避免 fuse 扫描半成品）；remote `scp_recv_multi` 到临时名再 `os.replace` → `CameraData/<proj>/{frame}-sags.ply` → `sags_status="done"`，日志"回收 {frame}-sags.ply (xx MB)"；
    - 其他（如 `no_people_detected`）→ `sags_status="failed"`，`sags_error=status`，不回收；
  - exit ≠ 0 → `sags_status="failed"`，`sags_error=f"exit {rc}"`；不重试（SAGS 短任务，用户可手动重跑）。
- 结束后 `state.sags_processes.pop(key)`。

**f) `build_page`（L326）与 `_JS_SSE`（L182）**
- 表格加 "SAGS" 列：按 `sags_status` 渲染按钮/徽标（D1，见第 2 节表格）；
- `doAction` 复用（`/action` 已支持任意 action 名）；
- SSE 处理：`sags_status` 变化时沿用现有"reload"模式（与 `new/checking/ready` 切换一致）。

### 3.3 `CameraData/<proj>/workers.json`
- 每个 worker 增加 `"sags_path": "C:/SAGS"`（各机一致；`_template/workers.json` 同步）。

### 3.4 `CameraData/<proj>/pipeline.json`（可选）
- 增加 `"sags": {"view_count": 12, "max_actors": 2, "speed": "balanced"}` 覆盖默认值；缺省用代码内默认（D6）。

### 3.5 不动的文件
- `fuse_server.py`、`_shared.py`、`fuse_ply.py`、`clip_ply.py`、`_server.py` —— 零改动。

## 4. 关键实现约束（踩坑记录）

1. **SSH 派发必须复用 `ssh_run` / `ssh_run_async`**（`_distributed.py`），禁止自定义 subprocess/ssh 调用：
   - remote：`["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", user@ip, command]`；
   - host：`subprocess.Popen(command, shell=True, stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, text=True, encoding="utf-8", errors="replace")` —— **必须字符串直传**，不能 list（见 `Docs/PLAN/subprocess-list2cmdline-bug.md`，list 会被 list2cmdline 二次转义双引号导致 `The filename...syntax is incorrect`）。
2. **管道缓冲死锁**：`stdout=PIPE` 必须配独立 reader 线程逐行消费（训练段已有此模式，SAGS 照抄），否则远程进程写满管道缓冲会卡死。
3. **venv python 直用**：沿用训练段 `".venv/Scripts/python.exe"` 的写法（train_daemon L1046）；若 SSH 会话出现 os error 448（uv trampoline 挂载点问题），回退 `uv python find 3.11`（`resolve_worker_python` 模式），届时再处理。
4. **report.json 校验**：回收前必须读 worker 上 `<sags_path>\result\<sub>\report.json` 的 `status`，仅 `"ok"` 才回收（`no_people_detected`/`dependency_error` 不产出有效结果）。
5. **原子回收**：`-sags.ply` 先写临时名再 `os.replace`，避免 fuse_server 5s 轮询扫到半成品文件。
6. **SAGS 队列是内存态**：daemon 重启后 `queued` 丢失（与现有帧状态一致，可接受；文档注明）。

## 5. 验证方案

1. **环境**：启动 `uv run python -m tills.server.train_daemon --config CameraData/<proj>/pipeline.json`（建议先用"帧数据在主机上"的项目，如 0719：`LiteGSWin/data/0719/` 有主机本地帧目录）。
2. **单帧端到端**：对一台 `done` 帧点 🎭 → 日志出现 SAGS 输出（views/report）→ 约 90s 后 `CameraData/<proj>/{frame}-sags.ply` 出现 → fuse_server 列表自动可见 → 混选 `{frame}.ply + {other}-sags.ply` fuse，确认命名 `xxx-combine-...-sags.ply` → clip → render。
3. **互斥**：SAGS 运行中观察 liteGS 派发日志——主机不被派发新帧（若主机是 SAGS 目标机）；remote 机器正常派发。
4. **串行**：训练中标记某帧 🎭 → 该帧训练完成(rc==0)后，同一机器下一周期自动开始 SAGS，不再接新 liteGS 帧。
5. **失败路径**：清理某帧数据后再点 🎭 → 报错"该帧训练数据已清理"；构造 `no_people_detected`（如对无人场景帧跑）→ 不回收、状态 failed。
6. **重跑/取消**：done 后再次 🎭 重跑覆盖；queued 时点取消 → 回到 none。
7. **清理交互**：queued 帧点清理 → 自动取消排队并删除数据；running 帧点清理 → 被拒绝。

## 6. 分阶段里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| **Phase 1 核心闭环** | WorkerNode.sags_path；FrameState 字段；enqueue/cancel action；SAGS 派发/监控/回收；liteGS 派发排除 SAGS 机器；日志进 worker 面板；页面最小 UI（按钮+状态文字，沿用 reload 机制） | 验证 2/3/5 通过 |
| **Phase 2 打磨** | 原子回收；清理交互（取消排队/拒绝清理）；取消按钮；错误 tooltip；`sags` 参数节读取；`_template/workers.json` 同步 | 验证 4/6/7 通过 |
| **Phase 3 收尾** | 端到端回归（fuse/clip/render 全链）、并发场景、README/docs 更新 | 全部验证通过 |

## 7. 风险与注意

- SAGS 每次运行约 90s（12 视图）；排队帧多时按机器串行，耗时线性增长——UI 文案提示"串行执行，多帧请耐心"。
- `-sags.ply` 与 `{frame}.ply` 并存会让 fuse 列表行数翻倍，属预期行为（D4）。
- daemon 重启丢失 SAGS 排队态（内存态）；Phase 3 可选加固：入队时写 `logs/sags_queue.json` 持久化。
- 若某帧训练失败但已排队 SAGS：保持 queued，待该帧重训成功后再执行（文档注明）。
