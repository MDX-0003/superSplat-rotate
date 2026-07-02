# v7 分布式训练管线 — 设计方案

> 状态：已审批 | 日期：2026-07-01

---

## 1. Context

### 问题
v6 流水线的 train 阶段是单机串行的：`batch_run.py` 在本地逐个帧训练。当帧数量多时，训练耗时线性增长（每帧约 5-15 分钟），5-10 帧就需要 1-2 小时。

### 目标
利用 5 台相同配置的 Win11 主机（均含 RTX 5090D + 相同路径的 supersplat 仓库和 LiteGSWin 环境），将训练阶段的帧分发到多台机器并行执行，训练完成后汇总结果回到主机，再复用现有 v6 的 fuse → clip → render 流程。

### 约束
- 同一个 `CameraData/<project>/` 下使用**唯一的 `pipeline.json`**，所有参数（训练、fuse、clip）均从此文件读取，不再引入额外的参数配置
- 同一 MMDD（即同一个 `sub_dir`）的 `raw_images/` 是**增量追加**的：Day1 放 3 帧训练完，Day2 可能再放 2 帧继续训练，不能每次全量扫描重分发
- 主机既做调度也参与训练（1 主 + 4 副，共 5 台 Worker）
- 每帧图片 < 50MB，PLY ~70MB，总计数据量在 GB 级别，局域网传输完全够用
- 副机认证方式待定（等硬件配齐后实测决定 SSH 密码 vs 密钥）

### 预期效果
- N 台机器并行训练 N 个帧 → 理论加速比 ≈ N×
- 增量场景下，仅需训练新增帧，已训练的 PLY 自动跳过
- 副机故障不影响其他机器，跳过失败帧继续汇总

---

## 2. 架构总览

```
主机 (Coordinator + Worker)
│
├── CameraData/<project>/
│   ├── pipeline.json          ← 唯一参数来源 (不变)
│   ├── workers.json           ← 新增: Worker 节点配置
│   ├── raw_images/            ← 帧素材 (用户增量放入)
│   │   ├── 114-2026-.../
│   │   ├── 115-2026-.../
│   │   └── ...
│   └── *.ply                  ← 训练产出堆积 (旧+新)
│
├── tills/run_pipeline_v7.py   ← 新增: v7 主控
└── tills/_distributed.py      ← 新增: 分布式工具库

副机 × 4 (Worker 1-4)
│
└── LiteGSWin/                 ← 相同路径
    ├── data/<MMDD>/           ← 主机分发的帧目录
    │   ├── 114-2026-.../
    │   └── 115-2026-.../
    ├── data/calibration/<MMDD>/ ← 需事先部署 (所有机器一致)
    └── results/<MMDD>/        ← 训练产物 (之后被主机拉取)
        └── *.ply
```

### 流程

```
Phase 1  差分检测    →  扫描 raw_images/，对比已有 PLY，得到 new_frames
Phase 2  分发帧数据  →  SCP/本地拷贝 new_frames 到各 Worker 的 LiteGSWin
Phase 3  并行训练    →  SSH 触发 batch_run.py，实时监控进度面板
Phase 4  回收结果    →  SCP 拉取 *.ply + cameras.json 到主机 CameraData/
Phase 5  fuse+clip+render → 完全复用 v6 现有逻辑 (tills/run_pipeline_v6.py 的对应函数)
```

---

## 3. 关键设计决策

### 3.1 远程执行：SSH
- Win11 内置 OpenSSH Server，仅需一行 PowerShell 开启
- Python `subprocess.Popen` 直接调用 `ssh <worker> "<command>"`，stdout/stderr 天然流式回传
- 未来如需跨机房，在 SSH 层之上加 Tailscale 即可，上层代码不变
- **备选方案**：WinRM (PowerShell Remoting) — 配置复杂，不推荐

### 3.2 增量分发（不是全量轮询）
```
raw_images/ 帧目录  ──→  筛选 PLY 不存在的新帧  ──→  均分给 N 台 Worker
已训练帧 (PLY 已存在) →  直接跳过，不参与分发
```
- 新增 1-3 帧 → 只用 1-3 台 Worker，其余闲置（可接受）
- 新增 5+ 帧 → 5 台全用满
- 分发时优先填满有 GPU 的 Worker，避免某台过载

### 3.3 统一参数来源
所有参数从 `CameraData/<project>/pipeline.json` 读取，新增字段：
```json
{
  "project": "03",
  "preset": "03-xxx",
  "litegs_path": "E:/path/to/LiteGSWin",
  "fps": 25,
  "resolution": "3840x2160",
  "distributed": {
    "enabled": true,
    "workers_config": "workers.json",
    "training": {
      "iterations": 10000,
      "target_primitives": 500000,
      "frame_stride": 1
    }
  }
}
```
- `distributed.enabled`：false 时退化为本地 v6 行为
- `distributed.training`：传递给 `batch_run.py` 的额外参数
- `distributed.workers_config`：指向 workers.json 的相对路径（相对于 pipeline.json 所在目录）

### 3.4 Workers 配置

```json
{
  "workers": [
    {
      "id": "host",
      "hostname": "localhost",
      "ip": "127.0.0.1",
      "ssh_user": null,
      "ssh_port": 22,
      "litegs_path": "E:/work/26.7_SKNJ/LiteGSWin",
      "supersplat_path": "E:/work/26.7_SKNJ/supersplat",
      "is_host": true
    },
    {
      "id": "worker1",
      "hostname": "DESKTOP-W1",
      "ip": "192.168.1.101",
      "ssh_user": "Administrator",
      "ssh_port": 22,
      "litegs_path": "E:/work/26.7_SKNJ/LiteGSWin",
      "supersplat_path": "E:/work/26.7_SKNJ/supersplat",
      "is_host": false
    }
  ]
}
```

**认证 (TBD)**：`ssh_user` / `ssh_key_path` / `ssh_password` 字段预留，具体方案等硬件到位后决定。

### 3.5 进度监控：状态文件 + 终端面板

不解析 tqdm 流（5 路并发 tqdm 会乱），改为每个 Worker 训练时定期写入状态文件：

```json
{
  "worker": "worker1",
  "status": "running",
  "current_frame": "116-2026-06-25-162734",
  "current_stage": "training",
  "iteration": 6000,
  "total_iterations": 10000,
  "total_frames": 6,
  "completed_frames": 2,
  "elapsed_seconds": 754.2
}
```

主机面板每 3 秒通过 SSH 读取各 Worker 状态文件，刷新终端显示：

```
═══════════════════════════════════════════════════════════════════
  v7 分布式训练 — project: 03  sub_dir: 0625  新增: 3 帧
═══════════════════════════════════════════════════════════════════
  Worker     分配  状态       当前帧           耗时    阶段
  ─────────────────────────────────────────────────────────────────
  host        1帧   training  116-...162734    08:23   iter 4000/10000
  worker1     1帧   training  117-...162812    09:01   iter 5000/10000
  worker2     1帧   training  118-...162905    07:45   colmap matching
  worker3     —     闲置
  worker4     —     闲置
  ─────────────────────────────────────────────────────────────────
  整体: 0/3 完成 | 已有 PLY: 12 → fuse 可选: 1-15
═══════════════════════════════════════════════════════════════════
```

### 3.6 错误处理策略
- 单帧失败 → 该 Worker 跳过此帧，继续下一帧（batch_run 本身已有此行为）
- 某 Worker 整机离线 → 该 Worker 的所有帧标记为失败，其他 Worker 继续
- 网络中断 → SSH 命令自动重试 3 次（5s 间隔），仍失败则标记该 Worker 离线
- 最终汇总时：丢失的帧不出现在项目目录的 PLY 列表中
- **不做**：自动重试、故障转移（保持简单）

---

## 4. 文件改动清单

### 4.1 新增文件

| 文件 | 说明 |
|------|------|
| `tills/run_pipeline_v7.py` | v7 主控脚本，约 400-500 行 |
| `tills/_distributed.py` | 分布式工具库，约 300-400 行 |
| `Docs/PLAN/v7-distributed-training.md` | 本设计文档的存档副本 |

### 4.2 修改文件

| 文件 | 改动 | 行数 |
|------|------|------|
| `LiteGSWin/batch_run.py` | 新增 `--frames` 参数 + 新增 `--worker-status` 参数 | ~20 行 |
| `LiteGSWin/run_LiteGS_pipeline.py` | (可选) 新增状态文件写入逻辑 | ~30 行 |

### 4.3 不变文件

`tills/_shared.py`、`tills_ply/*`、`tills/run_pipeline_v6.py`、`src/*` — 完全不改动。

---

## 5. 详细模块设计

### 5.1 `tills/_distributed.py` — 分布式工具库

```python
# 核心函数

class WorkerNode:
    """单台 Worker 的配置和连接状态"""
    id: str
    hostname: str
    ip: str
    ssh_user: str | None
    ssh_port: int = 22
    is_host: bool = False
    litegs_path: Path
    supersplat_path: Path

def load_workers(config_path: Path) -> list[WorkerNode]:
    """从 workers.json 加载 Worker 列表"""

def ssh_run(worker: WorkerNode, command: str, timeout: int = 3600) -> subprocess.CompletedProcess:
    """在远程 Worker 上执行命令，返回结果。主机直接 subprocess.run"""

def ssh_run_async(worker: WorkerNode, command: str) -> subprocess.Popen:
    """异步启动远程命令，返回 Popen 对象（用于并行训练）"""

def scp_send(worker: WorkerNode, local_path: Path, remote_path: str) -> bool:
    """SCP 发送文件/目录到远程 Worker。主机用 shutil.copy"""

def scp_recv(worker: WorkerNode, remote_path: str, local_path: Path) -> bool:
    """SCP 从远程 Worker 拉取文件/目录。主机用 shutil.copy"""

def read_worker_status(worker: WorkerNode, sub_dir: str) -> dict | None:
    """SSH 读取远程 status JSON 文件，解析返回"""

class ProgressDisplay:
    """终端进度面板，每 3 秒刷新"""
    def __init__(self, workers: list[WorkerNode]):
    def update(self, statuses: list[dict]):
    def render(self):
    def close(self):
```

### 5.2 `tills/run_pipeline_v7.py` — 主控脚本

```python
# 流程

def main():
    args = parse_args()
    cfg = load_config(args.config)  # pipeline.json

    if not cfg.get("distributed", {}).get("enabled", False):
        # 退化为 v6 行为
        run_v6_pipeline(cfg, args)
        return

    workers = load_workers(cfg)
    preset = load_preset(cfg["preset"])

    if should("train"):
        run_v7_train(cfg, workers)

    if should("fuse"):
        run_v6_fuse_interactive(cfg, preset, args.force)  # 完全复用

    if should("render"):
        asyncio.run(async_main_v6(args, cfg))  # 完全复用


def run_v7_train(cfg, workers):
    """
    Phase 1: 差分检测
    Phase 2: 分发帧数据到各 Worker
    Phase 3: 并行训练 + 监控面板
    Phase 4: 回收结果
    """
    # 1. 扫描 raw_images/ → 帧列表
    all_frames = scan_raw_images(cfg)
    
    # 2. 差分：过滤已有 PLY
    new_frames = filter_untrained(all_frames, cfg)
    if not new_frames:
        print("所有帧均已训练。")
        return
    
    # 3. 均分给 Worker (round-robin 或按顺序均分)
    chunks = distribute(new_frames, workers)
    
    # 4. 分发帧数据到各 Worker
    for worker, chunk in zip(workers, chunks):
        for frame in chunk:
            copy_frame_to_worker(worker, frame, cfg)  # 主机用 shutil, 副机用 SCP
    
    # 5. 并行启动训练
    processes = []
    for worker, chunk in zip(workers, chunks):
        if not chunk:
            continue
        cmd = build_batch_run_cmd(worker, chunk, cfg)
        proc = ssh_run_async(worker, cmd)
        processes.append((worker, chunk, proc))
    
    # 6. 监控面板
    display = ProgressDisplay(workers)
    try:
        while any(p.poll() is None for _, _, p in processes):
            statuses = [read_worker_status(w, cfg) for w in workers]
            display.update(statuses)
            display.render()
            time.sleep(3)
    finally:
        display.close()
    
    # 7. 回收 PLY + cameras.json
    for worker, chunk, proc in processes:
        rc = proc.wait()
        collect_results(worker, chunk, cfg)
    
    print("训练完成。")


def distribute(frames: list, workers: list) -> list[list]:
    """将帧均分给 Worker。只分给活跃的 Worker，多余的闲置。"""
    active = [w for w in workers if w.is_active]  # 可通过 ping 检测
    chunks = [[] for _ in active]
    for i, frame in enumerate(frames):
        chunks[i % len(active)].append(frame)
    return chunks
```

### 5.3 LiteGSWin 侧改动

**batch_run.py 新增参数：**

```python
parser.add_argument("--frames", nargs="*", default=None,
                    help="只处理指定的帧目录名 (空格分隔)")
parser.add_argument("--worker-status", type=str, default=None,
                    help="训练过程中写入状态文件的路径")
```

**discover_frames 修改：**

```python
def discover_frames(sub_dir, start_from=None, frames=None):
    all_frames = sorted(p for p in (DATA_ROOT / sub_dir).iterdir() if p.is_dir())
    if start_from:
        all_frames = [f for f in all_frames if f.name >= start_from]
    if frames is not None:
        frame_set = set(frames)
        all_frames = [f for f in all_frames if f.name in frame_set]
    return all_frames
```

**训练循环中写入状态文件：**
在每帧开始/结束时更新 `_worker_status.json`（如果 `--worker-status` 指定了路径）。这是增量修改，不影响现有逻辑。

---

## 6. 命令行接口

```bash
# 全自动：差分检测 → 分发 → 训练 → 回收 → fuse → render
python tills/run_pipeline_v7.py --config CameraData/03/pipeline.json

# 分步
python tills/run_pipeline_v7.py --config CameraData/03/pipeline.json --steps train
python tills/run_pipeline_v7.py --config CameraData/03/pipeline.json --steps fuse
python tills/run_pipeline_v7.py --config CameraData/03/pipeline.json --steps render

# 强制重训所有帧 (忽略已有 PLY)
python tills/run_pipeline_v7.py --config CameraData/03/pipeline.json --force

# 仅本机模拟 (5 进程本地跑，不连 SSH)
python tills/run_pipeline_v7.py --config CameraData/03/pipeline.json --simulate-local
```

`--steps` 行为与 v6 完全一致：不指定 = 全流程，指定 = 仅跑指定步骤。

---

## 7. 测试策略

### 第 1 层：本地模拟 (`--simulate-local`)
- **场景**：在单机上启动 5 个本地 subprocess，各自指向独立临时 data 目录
- **验证**：帧分配、状态文件读写、面板刷新、结果回收逻辑
- **不依赖**：任何外部机器或 SSH
- **数据量**：用 5 帧小数据集（可以用现有项目复制出 5 份）

### 第 2 层：双机 SSH 链路
- **场景**：主机 + 1 台副机，2 帧数据
- **验证**：SSH 连接、SCP 文件传输、远程命令执行、进度状态读取
- **依赖**：1 台副机的 SSH Server 已开启

### 第 3 层：5 机全量
- **场景**：150 帧真实数据，5 台机器满负荷
- **验证**：并行训练正确性、总耗时对比（vs 单机串行）、面板性能
- **注入故障**：kill 一台副机 SSH 进程 → 验证跳过逻辑

### 第 4 层：增量场景
- **场景**：先训练 3 帧 → fuse → render 完成 → 再追加 2 帧 → 再训练 → 再 fuse
- **验证**：差分检测正确性、已训练帧不会重新分发、新旧 PLY 均可选 fuse

### 输出一致性验证
- 相同帧数据分别在单机串行和分布式并行下训练
- 对比 PLY 文件大小、点云数量、fuse 后渲染画面（目测即可）

---

## 8. 实现阶段建议

| 阶段 | 内容 | 估算 |
|------|------|------|
| **Phase A** | `_distributed.py`：SSH/SCP 基础工具 + WorkerNode + ProgressDisplay | 核心基础设施 |
| **Phase B** | LiteGSWin 改动：`--frames` + `--worker-status` | ~20 行改动 |
| **Phase C** | `run_pipeline_v7.py` 主控：差分检测 + 分发 + 并行训练 + 回收 | 主逻辑 |
| **Phase D** | `--simulate-local` 模式 + 第 1 层测试 | 最早可验证的里程碑 |
| **Phase E** | 第 2 层双机测试（需要 1 台副机可用） | 验证 SSH 链路 |
| **Phase F** | 第 3/4 层全量测试（需要 5 台机器到位） | 最终验证 |

Phase A-D 可以**在只有主机的情况下完成**（通过 `--simulate-local` 验证所有逻辑），不需要等副机到位。

---

## 9. 未决事项 (TBD)

| # | 事项 | 状态 |
|---|------|------|
| 1 | SSH 认证方式（密码 vs 密钥 vs Windows 集成认证） | 等硬件到位测试 |
| 2 | workers.json 中 auth 字段最终格式 | 取决于 #1 |
| 3 | 面板是否需要交互（切详细视图 / 中止） | 先实现基础刷新版 |
| 4 | 是否需要训练完成后自动发通知（钉钉/微信/邮件） | 暂不实现 |

---

## 10. 相关文件索引

| 文件 | 作用 |
|------|------|
| `tills/run_pipeline_v6.py` | v6 主控（train/fuse/render），v7 需复用其 fuse 和 render 逻辑 |
| `tills/_shared.py` | 共享函数库（Playwright、preset、clip）— v7 完全复用 |
| `tills/paths.py` | 路径常量（ROOT/DATA/project()）— v7 复用 |
| `LiteGSWin/batch_run.py` | 训练调度器 — v7 需加 `--frames` 和 `--worker-status` |
| `LiteGSWin/run_LiteGS_pipeline.py` | 单帧训练流程 |
| `LiteGSWin/utils/common.py` | `auto_detect_frame_id` / REPO_ROOT / DATA_ROOT |
| `tills_ply/presets.json` | PLY 处理预设 — v7 不变 |
| `tills_ply/fuse_ply.py` | 多 PLY 融合 — v7 不变 |
| `tills_ply/clip_ply.py` | PLY 裁剪 — v7 不变 |
| `Docs/V5_V6_USAGE.md` | v5/v6 使用文档 — v7 完成后需补充 |
