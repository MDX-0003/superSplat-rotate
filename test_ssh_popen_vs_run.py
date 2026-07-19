"""精确定位：Popen SSH 期间 subprocess.run SSH 是否被阻塞"""
import subprocess
import time

WORKER4 = "10.10.4.104"
KEY_PATH = "C:/Users/admin/.ssh/id_ed25519"
SSH_BASE = ["ssh", "-i", KEY_PATH,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes"]

def ssh_popen(ip, cmd, label):
    return subprocess.Popen(
        SSH_BASE + [f"admin@{ip}", cmd],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

print("=== 测试 1: Popen worker4 → subprocess.run worker1 ===")
p = ssh_popen(WORKER4, "ping -n 15 127.0.0.1 > nul", "w4")
time.sleep(1)
t0 = time.time()
try:
    r = subprocess.run(
        SSH_BASE + ["admin@10.10.4.101", "echo OK"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10)
    print(f"   worker1 echo → rc={r.returncode} elapsed={time.time()-t0:.1f}s")
except subprocess.TimeoutExpired:
    print(f"   worker1 echo → TIMEOUT after {time.time()-t0:.1f}s")
p.wait(timeout=20)
print(f"   worker4 Popen done rc={p.returncode}\n")

print("=== 测试 1B: Popen worker1 → subprocess.run worker4 ===")
p1b = ssh_popen("10.10.4.101", "ping -n 15 127.0.0.1 > nul", "w1")
time.sleep(1)
t0 = time.time()
try:
    r = subprocess.run(
        SSH_BASE + [f"admin@{WORKER4}", "echo OK"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10)
    print(f"   worker4 echo → rc={r.returncode} elapsed={time.time()-t0:.1f}s")
except subprocess.TimeoutExpired:
    print(f"   worker4 echo → TIMEOUT after {time.time()-t0:.1f}s")
p1b.wait(timeout=20)
print(f"   worker1 Popen done rc={p1b.returncode}\n")

print("=== 测试 2: Popen worker4 → subprocess.run worker4 自己 ===")
p2 = ssh_popen(WORKER4, "ping -n 15 127.0.0.1 > nul", "w4")
time.sleep(1)
t0 = time.time()
try:
    r = subprocess.run(
        SSH_BASE + ["admin@10.10.4.104", "echo OK"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10)
    print(f"   worker4 self → rc={r.returncode} elapsed={time.time()-t0:.1f}s")
except subprocess.TimeoutExpired:
    print(f"   worker4 self → TIMEOUT after {time.time()-t0:.1f}s")
p2.wait(timeout=20)
print(f"   worker4 Popen done rc={p2.returncode}\n")

print("=== 测试 3: Popen host (本地) → subprocess.run worker4 ===")
import sys
# 本地跑 ping 不涉及 SSH
p3 = subprocess.Popen(["ping", "-n", "15", "127.0.0.1"],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, encoding="utf-8", errors="replace")
time.sleep(1)
t0 = time.time()
try:
    r = subprocess.run(
        SSH_BASE + [f"admin@{WORKER4}", "echo OK"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10)
    print(f"   worker4 echo → rc={r.returncode} elapsed={time.time()-t0:.1f}s")
except subprocess.TimeoutExpired:
    print(f"   worker4 echo → TIMEOUT after {time.time()-t0:.1f}s")
p3.wait(timeout=20)
print(f"   本地 Popen done rc={p3.returncode}\n")

print("=== 测试 4: 两个 subprocess.run 同时（两个线程）===")
import threading
def run_w1():
    r = subprocess.run(
        SSH_BASE + ["admin@10.10.4.101", "ping -n 5 127.0.0.1 > nul"],
        capture_output=True, timeout=15)
    print(f"   worker1 ping → rc={r.returncode}")
def run_w4():
    r = subprocess.run(
        SSH_BASE + [f"admin@{WORKER4}", "echo OK"],
        capture_output=True, timeout=10)
    print(f"   worker4 echo → rc={r.returncode}")
t1 = threading.Thread(target=run_w1); t4 = threading.Thread(target=run_w4)
t1.start(); t4.start()
t1.join(); t4.join()
