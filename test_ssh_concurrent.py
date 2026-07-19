"""并发 SSH 压测 worker4 — 模拟 daemon 多连接场景"""
import subprocess
import time
import threading

WORKER1 = "10.10.4.101"
WORKER4 = "10.10.4.104"
KEY_PATH = "C:/Users/admin/.ssh/id_ed25519"
SSH_OPTS = [
    "-i", KEY_PATH,
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
]

def ssh_cmd(ip, remote_cmd):
    return ["ssh"] + SSH_OPTS + [f"admin@{ip}", remote_cmd]

results = []

def run_worker(label, ip, remote_cmd, timeout):
    t0 = time.time()
    try:
        r = subprocess.run(ssh_cmd(ip, remote_cmd),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=timeout)
        results.append(f"[{label}] rc={r.returncode} "
                       f"stdout={r.stdout.strip()!r} "
                       f"elapsed={time.time()-t0:.1f}s")
    except subprocess.TimeoutExpired:
        results.append(f"[{label}] TIMEOUT after {time.time()-t0:.1f}s")
    except Exception as e:
        results.append(f"[{label}] ERROR: {e}")

print("=" * 60)
print("场景 A: 先开长连接到 worker1，再连 worker4")
print("=" * 60)

# 1. 先启动到 worker1 的长连接（模拟训练中）
print("1. 启动 worker1 长连接 (ssh_run_async, 跑 15s)...")
proc_w1 = subprocess.Popen(
    ssh_cmd(WORKER1, "ping -n 15 127.0.0.1 > nul"),
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace",
)
time.sleep(1)  # 等连接建立

# 2. 在 worker1 连接存活期间，连 worker4（模拟 daemon dispatch mkdir）
print("2. worker1 连接存活中，尝试 worker4 mkdir ...")
t0 = time.time()
try:
    r = subprocess.run(ssh_cmd(WORKER4, r'mkdir "C:\code\LiteGSWin\data\0718\_concurrent_test"'),
                      capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=10)
    print(f"   worker4 mkdir → rc={r.returncode} elapsed={time.time()-t0:.1f}s")
    if r.stderr.strip():
        print(f"   stderr: {r.stderr.strip()!r}")
except subprocess.TimeoutExpired:
    print(f"   worker4 mkdir → TIMEOUT after {time.time()-t0:.1f}s")

proc_w1.wait(timeout=20)
print(f"   worker1 长连接结束 rc={proc_w1.returncode}\n")

print("=" * 60)
print("场景 B: worker1 + worker4 同时各开 1 条长连接")
print("=" * 60)

t1 = threading.Thread(target=run_worker,
                      args=("w1-long", WORKER1, "ping -n 12 127.0.0.1 > nul", 20))
t4 = threading.Thread(target=run_worker,
                      args=("w4-long", WORKER4, "ping -n 8 127.0.0.1 > nul", 15))
t1.start(); time.sleep(0.5); t4.start()
t1.join(); t4.join()
for r in results[-2:]:
    print(f"   {r}")

print("\n=" * 60)
print("场景 C: 快速连续 5 次 SSH 到 worker4（模拟 retry loop）")
print("=" * 60)
for i in range(5):
    t0 = time.time()
    try:
        r = subprocess.run(ssh_cmd(WORKER4, "echo OK"),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=10)
        print(f"   [{i+1}] rc={r.returncode} elapsed={time.time()-t0:.1f}s")
    except subprocess.TimeoutExpired:
        print(f"   [{i+1}] TIMEOUT after {time.time()-t0:.1f}s")
    time.sleep(0.2)

print("\n=" * 60)
print("场景 D: 同时 3 条 Popen 到 worker4（最大并发压力）")
print("=" * 60)
procs = []
for i in range(3):
    p = subprocess.Popen(
        ssh_cmd(WORKER4, "ping -n 5 127.0.0.1 > nul"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    procs.append(p)
    print(f"   [{i+1}] Popen started")

time.sleep(3)
for i, p in enumerate(procs):
    rc = p.poll()
    status = f"rc={rc}" if rc is not None else "still running"
    print(f"   [{i+1}] {status}")
    if rc is None:
        try:
            p.wait(timeout=8)
            print(f"   [{i+1}] finished rc={p.returncode}")
        except subprocess.TimeoutExpired:
            p.kill()
            print(f"   [{i+1}] TIMEOUT killed")
