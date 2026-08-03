#!/usr/bin/env python3
"""
完整模拟 daemon 分发标定时对 worker 执行的所有 SSH / SCP / PowerShell 命令。

用法:
    uv run python -m tills.tests.test_scp_cali                          # 默认 project=0719, worker=worker1
    uv run python -m tills.tests.test_scp_cali --project 0719 --worker worker1
    uv run python -m tills.tests.test_scp_cali --dry-run                # 只打印命令不执行
    uv run python -m tills.tests.test_scp_cali --speed-test             # 先发小文件测速, 再发标定目录
    uv run python -m tills.tests.test_scp_cali --speed-only             # 只测速, 不传标定
    uv run python -m tills.tests.test_scp_cali --verbose                # SCP -v 详细日志
    uv run python -m tills.tests.test_scp_cali --timeout 1800

依赖:
    - 本机需在 CameraData/<project>/ 下有 pipeline.json + workers.json
    - 本机需能 SSH 到目标 worker（key 已配置）
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── 路径 ──────────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent.parent  # supersplat/


def load_config(project: str) -> dict:
    cfg_path = ROOT / "CameraData" / project / "pipeline.json"
    if not cfg_path.exists():
        sys.exit(f"ERROR: config not found: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_workers(project: str) -> list[dict]:
    cfg = load_config(project)
    proj_dir = ROOT / "CameraData" / project
    dist_cfg = cfg.get("distributed", {})
    workers_config = dist_cfg.get("workers_config", "workers.json")
    workers_path = proj_dir / workers_config
    if not workers_path.exists():
        sys.exit(f"ERROR: workers config not found: {workers_path}")
    with open(workers_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("workers", [])


def find_worker(workers: list[dict], worker_id: str) -> dict:
    for w in workers:
        if w["id"] == worker_id:
            return w
    sys.exit(f"ERROR: worker '{worker_id}' not found in workers.json. "
             f"Available: {[w['id'] for w in workers]}")


# ── SSH / SCP 命令构建（与 _distributed.py 完全一致）─────────────────────────────

def build_ssh_cmd(worker: dict, remote_command: str) -> list[str]:
    ssh_user = worker.get("ssh_user")
    ssh_target = f"{ssh_user}@{worker['ip']}" if ssh_user else worker["ip"]
    cmd = ["ssh"]
    if worker.get("ssh_key_path"):
        cmd.extend(["-i", worker["ssh_key_path"]])
    if worker.get("ssh_port", 22) != 22:
        cmd.extend(["-p", str(worker["ssh_port"])])
    cmd.extend(["-o", "StrictHostKeyChecking=accept-new"])
    cmd.extend(["-o", "ConnectTimeout=10"])
    cmd.extend(["-o", "BatchMode=yes"])
    cmd.append(ssh_target)
    cmd.append(remote_command)
    return cmd


def build_scp_send_cmd(worker: dict, local_paths: list[str], remote_dst: str,
                        verbose: bool = False) -> list[str]:
    ssh_user = worker.get("ssh_user")
    ssh_target = f"{ssh_user}@{worker['ip']}" if ssh_user else worker["ip"]
    remote_target = f"{ssh_target}:{remote_dst}"
    scp_args = ["scp", "-r"]
    if verbose:
        scp_args.append("-v")
    if worker.get("ssh_key_path"):
        scp_args.extend(["-i", worker["ssh_key_path"]])
    if worker.get("ssh_port", 22) != 22:
        scp_args.extend(["-P", str(worker["ssh_port"])])
    scp_args.extend(["-o", "StrictHostKeyChecking=accept-new"])
    scp_args.extend(["-o", "ConnectTimeout=10"])
    scp_args.extend(["-o", "BatchMode=yes"])
    scp_args.extend(local_paths)
    scp_args.append(remote_target)
    return scp_args


def build_backup_cmd(cali_dir: str, backup_root: str, sub_dir: str) -> str:
    return (
        f'powershell -Command "'
        f"$src='{cali_dir}'; $dstRoot='{backup_root}'; $name='{sub_dir}'; "
        f"if (-not (Test-Path $src)) {{ exit 0 }}; "
        f"New-Item -ItemType Directory -Force -Path $dstRoot | Out-Null; "
        f'$n=1; while (Test-Path \\"$dstRoot\\$name-$n\\") {{ $n++ }}; '
        f'Move-Item -Path $src -Destination \\"$dstRoot\\$name-$n\\"'
        f'"'
    )


# ── 执行辅助 ─────────────────────────────────────────────────────────────────────

class Colors:
    HEADER = "\033[1;36m"
    OK = "\033[32m"
    WARN = "\033[33m"
    ERROR = "\033[31m"
    DIM = "\033[2m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def run_cmd(cmd: list[str], timeout: int = 600, dry_run: bool = False,
            show_stdout: bool = True) -> subprocess.CompletedProcess:
    """运行命令，打印命令和结果。返回 CompletedProcess，附赠 .elapsed 属性。"""
    cmd_str = " ".join(cmd)
    print(f"\n{Colors.DIM}── 命令 ──────────────────────────────────────────────{Colors.RESET}")
    print(f"  {cmd_str}")

    if dry_run:
        print(f"  {Colors.WARN}(dry-run, 跳过执行){Colors.RESET}")
        r = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        r.elapsed = 0.0
        return r

    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=timeout)
        result.elapsed = time.time() - t0  # type: ignore[attr-defined]

        if show_stdout:
            if result.stdout.strip():
                print(f"{Colors.DIM}── stdout ────────────────────────────────────────────{Colors.RESET}")
                for line in result.stdout.strip().split("\n"):
                    print(f"  {Colors.OK}│{Colors.RESET} {line}")
            else:
                print(f"  {Colors.DIM}(无 stdout){Colors.RESET}")

        if result.stderr.strip():
            print(f"{Colors.DIM}── stderr ────────────────────────────────────────────{Colors.RESET}")
            for line in result.stderr.strip().split("\n"):
                print(f"  {Colors.WARN}│{Colors.RESET} {line}")

        print(f"{Colors.DIM}── 结果 ──────────────────────────────────────────────{Colors.RESET}")
        status = (
            f"{Colors.OK}OK (exit 0)" if result.returncode == 0
            else f"{Colors.ERROR}FAIL (exit {result.returncode})"
        )
        print(f"  {status}{Colors.RESET}  ⏱ {result.elapsed:.1f}s")
        return result

    except subprocess.TimeoutExpired:
        result = subprocess.CompletedProcess(cmd, -1, stdout="", stderr="")
        result.elapsed = time.time() - t0  # type: ignore[attr-defined]
        print(f"  {Colors.ERROR}⏱ TIMEOUT after {result.elapsed:.0f}s (limit {timeout}s){Colors.RESET}")
        raise
    except Exception as e:
        result = subprocess.CompletedProcess(cmd, -2, stdout="", stderr=str(e))
        result.elapsed = time.time() - t0  # type: ignore[attr-defined]
        print(f"  {Colors.ERROR}💥 EXCEPTION: {e}{Colors.RESET}")
        raise


# ── 目录大小 / 格式化 ────────────────────────────────────────────────────────────

def get_dir_size(path: Path) -> tuple[int, int]:
    """返回 (字节数, 文件数)。"""
    total = 0
    count = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
            count += 1
    return total, count


def fmt_size(byte_count: int) -> str:
    if byte_count > 1024 * 1024 * 1024:
        return f"{byte_count / (1024**3):.1f} GB"
    elif byte_count > 1024 * 1024:
        return f"{byte_count / (1024**2):.1f} MB"
    else:
        return f"{byte_count / 1024:.1f} KB"


def fmt_speed(bytes_transferred: int, elapsed: float) -> str:
    if elapsed <= 0:
        return "—"
    bps = bytes_transferred / elapsed
    if bps > 1024 * 1024:
        return f"{bps / (1024**2):.1f} MB/s"
    elif bps > 1024:
        return f"{bps / 1024:.1f} KB/s"
    else:
        return f"{bps:.0f} B/s"


# ── 测速：先传一个小文件测基线 ───────────────────────────────────────────────────

def run_speed_test(worker: dict, worker_litegs: Path,
                   speed_size_mb: int = 10,
                   dry_run: bool = False,
                   verbose: bool = False) -> dict | None:
    """SCP 一个指定大小的临时文件到 worker，测量传输速度。

    Returns:
        dict with keys: elapsed, size_bytes, speed_str, overhead_estimate
        None on failure or dry_run.
    """
    print(f"\n{Colors.BOLD}[Speed Test] 小文件测速 ({speed_size_mb} MB){Colors.RESET}")

    # 创建临时文件
    if dry_run:
        print(f"  {Colors.WARN}(dry-run, 跳过){Colors.RESET}")
        return None

    tmp_path = None
    try:
        tmp_path = Path(tempfile.mkstemp(suffix=".bin", prefix="scp_speed_test_")[1])
        size_bytes = speed_size_mb * 1024 * 1024

        # 用 seek + write 创建稀疏文件友好的方式
        print(f"  创建测试文件: {tmp_path} ({fmt_size(size_bytes)})")
        t0 = time.time()
        with open(tmp_path, "wb") as f:
            f.seek(size_bytes - 1)
            f.write(b"\0")
        print(f"  {Colors.DIM}创建耗时: {time.time() - t0:.1f}s{Colors.RESET}")

        # 确保远程临时目录存在
        remote_tmp = f"{worker_litegs.as_posix()}/data/_scp_test"
        mkdir_cmd = f'if not exist "{remote_tmp}" mkdir "{remote_tmp}"'
        r = subprocess.run(build_ssh_cmd(worker, mkdir_cmd),
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        if r.returncode != 0:
            print(f"  {Colors.ERROR}✗ 无法创建远程临时目录{Colors.RESET}")
            return None

        # SCP 上传测试文件
        print(f"  SCP 上传 {tmp_path} → {worker['id']}:{remote_tmp}/")
        scp_cmd = build_scp_send_cmd(worker, [str(tmp_path)],
                                      remote_tmp, verbose=verbose)
        result = run_cmd(scp_cmd, timeout=120, show_stdout=False)

        if result.returncode != 0:
            print(f"  {Colors.ERROR}✗ 测速 SCP 失败{Colors.RESET}")
            return None

        elapsed = result.elapsed
        speed = fmt_speed(size_bytes, elapsed)

        # 估算连接开销：如果同样的文件再传一次，对比时间差
        # SSH 往返延迟测试
        t1 = time.time()
        ping_result = subprocess.run(
            build_ssh_cmd(worker, "echo ping"),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10)
        ssh_rtt = time.time() - t1

        # 清理远程文件
        cleanup_cmd = f'del /q "{remote_tmp}\\{tmp_path.name}" 2>nul & rmdir "{remote_tmp}" 2>nul'
        subprocess.run(build_ssh_cmd(worker, cleanup_cmd),
                       capture_output=True, timeout=30)

        # 打印报告
        print(f"\n{Colors.BOLD}  ══ 测速报告 ══{Colors.RESET}")
        print(f"  测试文件大小:   {fmt_size(size_bytes)}")
        print(f"  传输耗时:       {elapsed:.1f}s")
        print(f"  {Colors.BOLD}实际传输速度:   {speed}{Colors.RESET}")
        print(f"  SSH 往返延迟:   {ssh_rtt * 1000:.0f} ms")
        if elapsed > 0 and ssh_rtt > 0:
            # 理论最大速度 = size / (elapsed - ssh_rtt)（粗略扣掉连接建立）
            pure_transfer = max(elapsed - ssh_rtt, 0.1)
            pure_speed = fmt_speed(size_bytes, pure_transfer)
            print(f"  纯传输速度:     {pure_speed} (扣除 SSH 握手)")
        print()

        return {
            "elapsed": elapsed,
            "size_bytes": size_bytes,
            "speed_str": speed,
            "ssh_rtt_ms": ssh_rtt * 1000,
        }

    finally:
        if tmp_path and tmp_path.exists():
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ── 主流程（与 run_distribute_cali 完全一致）──────────────────────────────────────

def test_distribute_cali(project: str, worker_id: str,
                         dry_run: bool = False,
                         speed_test: bool = False,
                         speed_only: bool = False,
                         speed_size_mb: int = 10,
                         timeout: int = 600,
                         verbose: bool = False):
    cfg = load_config(project)
    workers = load_workers(project)
    worker = find_worker(workers, worker_id)

    litegs_path = Path(cfg.get("litegs_path", ""))
    cali_root = litegs_path / "data" / "calibration"
    host_cali = cali_root / project
    sub_dir = project  # 默认 sub_dir == project name

    worker_litegs = Path(worker["litegs_path"])
    worker_cali = worker_litegs / "data" / "calibration" / sub_dir
    worker_old = worker_litegs / "data" / "old-cali"

    print(f"\n{Colors.HEADER}{'═' * 62}{Colors.RESET}")
    print(f"{Colors.BOLD}  SCP 标定分发 — 完整模拟{Colors.RESET}")
    print(f"{Colors.HEADER}{'═' * 62}{Colors.RESET}")
    print(f"  项目:           {project}")
    print(f"  目标 Worker:    {worker_id} ({worker['ip']})")
    print(f"  SSH 用户:       {worker.get('ssh_user', '(默认)')}")
    print(f"  SSH Key:        {worker.get('ssh_key_path', '(默认)')}")
    print(f"  主机 cali 目录: {host_cali}")
    print(f"  副机 cali 目录: {worker_cali}")
    print(f"  副机备份目录:   {worker_old}")

    # ── Step 0: 验证主机标定数据 ──
    print(f"\n{Colors.BOLD}[Step 0] 验证主机标定数据{Colors.RESET}")
    sparse_txt = host_cali / "sparse" / "cameras.txt"
    sparse_bin = host_cali / "sparse_bin"
    if not sparse_txt.exists():
        sys.exit(f"  {Colors.ERROR}✗ sparse/cameras.txt 不存在: {sparse_txt}{Colors.RESET}")
    if not sparse_bin.is_dir() or not any(sparse_bin.iterdir()):
        sys.exit(f"  {Colors.ERROR}✗ sparse_bin/ 为空: {sparse_bin}{Colors.RESET}")
    cali_size, cali_files = get_dir_size(host_cali)
    print(f"  {Colors.OK}✓ 主机标定数据完整 — {fmt_size(cali_size)}, {cali_files} 个文件{Colors.RESET}")

    # ── Step 1: SSH 连通性测试 ──
    print(f"\n{Colors.BOLD}[Step 1] SSH 连通性测试{Colors.RESET}")
    t0 = time.time()
    ssh_test_cmd = build_ssh_cmd(worker, "echo OK")
    result = run_cmd(ssh_test_cmd, timeout=15, dry_run=dry_run)
    ssh_connect_time = time.time() - t0
    if not dry_run and (result.returncode != 0 or "OK" not in (result.stdout or "")):
        print(f"  {Colors.ERROR}✗ SSH 连通性测试失败！请检查 key/网络/防火墙{Colors.RESET}")
        sys.exit(1)
    if not dry_run:
        print(f"  {Colors.OK}✓ SSH OK (连接耗时 {ssh_connect_time:.1f}s){Colors.RESET}")

    # ── Speed Test（可选）──
    speed_info = None
    if speed_test or speed_only:
        speed_info = run_speed_test(worker, worker_litegs,
                                     speed_size_mb=speed_size_mb,
                                     dry_run=dry_run, verbose=verbose)

    if speed_only:
        print(f"{Colors.HEADER}{'═' * 62}{Colors.RESET}")
        print(f"{Colors.BOLD}  测速完成。{Colors.RESET}")
        print(f"{Colors.HEADER}{'═' * 62}{Colors.RESET}")
        return

    # ── Step 2: 检查副机已有标定 ──
    print(f"\n{Colors.BOLD}[Step 2] 检查副机是否已有标定{Colors.RESET}")
    check_cmd = f'if exist "{worker_cali}" (echo EXISTS) else (echo NOT_FOUND)'
    ssh_check_cmd = build_ssh_cmd(worker, check_cmd)
    result = run_cmd(ssh_check_cmd, timeout=30, dry_run=dry_run)
    has_existing = "EXISTS" in (result.stdout or "")

    # ── Step 3: 备份副机已有标定 ──
    if has_existing:
        print(f"\n{Colors.BOLD}[Step 3] 备份副机已有标定{Colors.RESET}")
        move_cmd = build_backup_cmd(str(worker_cali), str(worker_old), sub_dir)
        ssh_move_cmd = build_ssh_cmd(worker, move_cmd)
        result = run_cmd(ssh_move_cmd, timeout=60, dry_run=dry_run)
        if not dry_run and result.returncode != 0:
            print(f"  {Colors.WARN}⚠ 备份失败 (exit {result.returncode})，继续尝试分发...{Colors.RESET}")
        else:
            print(f"  {Colors.OK}✓ 已备份{Colors.RESET}")
    else:
        print(f"\n{Colors.BOLD}[Step 3] 备份 — 跳过（副机无已有标定）{Colors.RESET}")

    # ── Step 4: 确保副机目标父目录存在 ──
    print(f"\n{Colors.BOLD}[Step 4] 确保副机目标父目录存在{Colors.RESET}")
    mkdir_cmd = f'if not exist "{worker_cali.parent}" mkdir "{worker_cali.parent}"'
    ssh_mkdir_cmd = build_ssh_cmd(worker, mkdir_cmd)
    run_cmd(ssh_mkdir_cmd, timeout=30, dry_run=dry_run)

    # ── Step 5: SCP 分发标定 ──
    print(f"\n{Colors.BOLD}[Step 5] SCP 分发标定{Colors.RESET}")
    print(f"  {Colors.DIM}源:   {host_cali}{Colors.RESET}")
    print(f"  {Colors.DIM}大小: {fmt_size(cali_size)}, {cali_files} 个文件{Colors.RESET}")

    # 用测速结果预估耗时
    if speed_info:
        estimated_s = cali_size / (speed_info["size_bytes"] / speed_info["elapsed"]) if speed_info["elapsed"] > 0 else 0
        print(f"  {Colors.DIM}预估耗时: {estimated_s:.0f}s (基于 {speed_info['speed_str']}){Colors.RESET}")
    parent = str(worker_cali.parent).replace("\\", "/")
    print(f"  {Colors.DIM}目标: {worker_id}:{parent}/{Colors.RESET}")

    scp_cmd = build_scp_send_cmd(worker, [str(host_cali)], parent, verbose=verbose)
    try:
        result = run_cmd(scp_cmd, timeout=timeout, dry_run=dry_run,
                         show_stdout=not verbose)
        if dry_run:
            pass
        elif result.returncode == 0:
            elapsed = result.elapsed
            speed = fmt_speed(cali_size, elapsed)
            print(f"\n{Colors.OK}{'═' * 62}{Colors.RESET}")
            print(f"{Colors.OK}  ✓ SCP 分发成功！{Colors.RESET}")
            print(f"  {Colors.DIM}总耗时: {elapsed:.1f}s | 有效速度: {speed}{Colors.RESET}")
            print(f"{Colors.OK}{'═' * 62}{Colors.RESET}")
        else:
            print(f"\n{Colors.ERROR}{'═' * 62}{Colors.RESET}")
            print(f"{Colors.ERROR}  ✗ SCP 失败 (exit {result.returncode}){Colors.RESET}")
            print(f"{Colors.ERROR}{'═' * 62}{Colors.RESET}")
            print(f"\n{Colors.WARN}常见原因排查：{Colors.RESET}")
            print(f"  1. 远程路径中的盘符冒号被 SCP 误解析为 host:path 分隔符")
            print(f"  2. 远程 OpenSSH 版本过旧，不支持 Windows 盘符路径")
            print(f"  3. 远程磁盘空间不足")
            print(f"  4. 远程目录权限问题")
            print(f"  5. 防火墙/网络超时（大目录传输）")
            print(f"\n  尝试手动运行：")
            print(f"  {Colors.DIM}{' '.join(scp_cmd)}{Colors.RESET}")
    except subprocess.TimeoutExpired:
        print(f"\n{Colors.ERROR}  ✗ SCP 超时 ({timeout}s){Colors.RESET}")
        print(f"  传输量: {fmt_size(cali_size)}, 最低所需速度: {fmt_speed(cali_size, timeout)}")

    # ── Step 6: 验证远程文件 ──
    print(f"\n{Colors.BOLD}[Step 6] 验证远程文件{Colors.RESET}")
    verify_cmd = f'if exist "{worker_cali}\\sparse\\cameras.txt" (echo VERIFIED) else (echo MISSING)'
    ssh_verify_cmd = build_ssh_cmd(worker, verify_cmd)
    result = run_cmd(ssh_verify_cmd, timeout=30, dry_run=dry_run)
    if not dry_run and "VERIFIED" in (result.stdout or ""):
        print(f"  {Colors.OK}✓ 远程文件验证通过{Colors.RESET}")
    elif not dry_run:
        print(f"  {Colors.WARN}⚠ 远程文件验证失败 — 文件可能未传输完整{Colors.RESET}")


# ── CLI ──────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="完整模拟 daemon 标定分发：SSH + SCP + PowerShell",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  uv run python -m tills.tests.test_scp_cali
  uv run python -m tills.tests.test_scp_cali --project 0719 --worker worker1
  uv run python -m tills.tests.test_scp_cali --dry-run
  uv run python -m tills.tests.test_scp_cali --speed-test           # 先测速再传
  uv run python -m tills.tests.test_scp_cali --speed-only           # 只测速不传
  uv run python -m tills.tests.test_scp_cali --verbose              # SCP -v 调试
  uv run python -m tills.tests.test_scp_cali --timeout 1800
        """,
    )
    parser.add_argument("--project", default="0719", help="项目名 (默认 0719)")
    parser.add_argument("--worker", default="worker1", help="目标 Worker ID (默认 worker1)")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    parser.add_argument("--timeout", type=int, default=600, help="SCP 超时秒数 (默认 600)")
    parser.add_argument("--speed-test", action="store_true",
                        help="先传一个小文件测速，再传标定目录")
    parser.add_argument("--speed-only", action="store_true",
                        help="只测速，不传标定目录")
    parser.add_argument("--speed-size", type=int, default=10,
                        help="测速文件大小 MB (默认 10)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="SCP 加 -v 打印协议级调试信息")
    args = parser.parse_args()

    test_distribute_cali(
        args.project, args.worker,
        dry_run=args.dry_run,
        speed_test=args.speed_test,
        speed_only=args.speed_only,
        speed_size_mb=args.speed_size,
        timeout=args.timeout,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
