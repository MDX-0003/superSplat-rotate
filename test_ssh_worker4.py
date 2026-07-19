"""对比 subprocess SSH vs 交互式 SSH 到 worker4"""
import subprocess
import time

WORKER4_IP = "10.10.4.104"
KEY_PATH = "C:/Users/admin/.ssh/id_ed25519"

# ── daemon 发的 mkdir 命令（完全一致的参数列表，不是字符串）──
mkdir_cmd = [
    "ssh",
    "-i", KEY_PATH,
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    f"admin@{WORKER4_IP}",
    r'mkdir "C:\code\LiteGSWin\data\0718\test_subprocess"',
]
mkdir_cmd_str = [
    "ssh",
    "-i", KEY_PATH,
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    f"admin@{WORKER4_IP}",
    'mkdir "C:\\code\\LiteGSWin\\data\\0718\\test_subprocess"',
]

# ── 简单 echo 命令 ──
echo_cmd = [
    "ssh",
    "-i", KEY_PATH,
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    f"admin@{WORKER4_IP}",
    "echo OK",
]

# ── scp 命令（模拟 daemon 的 scp_send_multi）──
scp_cmd = [
    "scp",
    "-r",
    "-i", KEY_PATH,
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    r"C:\Windows\System32\calc.exe",
    f"admin@{WORKER4_IP}:C:/Users/admin/Desktop/test_scp_calc.exe",
]

print("=" * 60)
print("1. subprocess.run echo (timeout=10)")
print(f"   cmd: {' '.join(echo_cmd)}")
t0 = time.time()
try:
    r = subprocess.run(echo_cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=10)
    print(f"   rc={r.returncode}  stdout={r.stdout.strip()!r}  "
          f"stderr={r.stderr.strip()!r}  elapsed={time.time()-t0:.1f}s")
except subprocess.TimeoutExpired:
    print(f"   TIMEOUT after {time.time()-t0:.1f}s")
except Exception as e:
    print(f"   ERROR: {e}")

print()
print("=" * 60)
print("2. subprocess.run mkdir (list 参数) (timeout=10)")
print(f"   cmd: {' '.join(mkdir_cmd)}")
t0 = time.time()
try:
    r = subprocess.run(mkdir_cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=10)
    print(f"   rc={r.returncode}  stdout={r.stdout.strip()!r}  "
          f"stderr={r.stderr.strip()!r}  elapsed={time.time()-t0:.1f}s")
except subprocess.TimeoutExpired:
    print(f"   TIMEOUT after {time.time()-t0:.1f}s")
except Exception as e:
    print(f"   ERROR: {e}")

print()
print("=" * 60)
print("3. subprocess.run mkdir (str 参数，反斜杠) (timeout=10)")
print(f"   cmd: {' '.join(mkdir_cmd_str)}")
t0 = time.time()
try:
    r = subprocess.run(mkdir_cmd_str, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=10)
    print(f"   rc={r.returncode}  stdout={r.stdout.strip()!r}  "
          f"stderr={r.stderr.strip()!r}  elapsed={time.time()-t0:.1f}s")
except subprocess.TimeoutExpired:
    print(f"   TIMEOUT after {time.time()-t0:.1f}s")
except Exception as e:
    print(f"   ERROR: {e}")

print()
print("=" * 60)
print("4. subprocess.run SCP (timeout=30)")
print(f"   cmd: {' '.join(scp_cmd)}")
t0 = time.time()
try:
    r = subprocess.run(scp_cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    print(f"   rc={r.returncode}  stdout={r.stdout.strip()!r}  "
          f"elapsed={time.time()-t0:.1f}s")
    if r.stderr.strip():
        print(f"   stderr={r.stderr.strip()!r}")
except subprocess.TimeoutExpired:
    print(f"   TIMEOUT after {time.time()-t0:.1f}s")
except Exception as e:
    print(f"   ERROR: {e}")

print()
print("=" * 60)
print("5. subprocess.Popen (模拟 ssh_run_async) (timeout=5)")
popen_cmd = [
    "ssh",
    "-i", KEY_PATH,
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    f"admin@{WORKER4_IP}",
    "ping -n 5 127.0.0.1 > nul",
]
print(f"   cmd: {' '.join(popen_cmd)}")
t0 = time.time()
proc = subprocess.Popen(popen_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace")
time.sleep(2)
rc = proc.poll()
if rc is None:
    print(f"   still running after 2s — waiting max 8s more...")
    try:
        proc.wait(timeout=8)
        print(f"   finished, rc={proc.returncode}  elapsed={time.time()-t0:.1f}s")
    except subprocess.TimeoutExpired:
        proc.kill()
        print(f"   TIMEOUT, killed  elapsed={time.time()-t0:.1f}s")
else:
    print(f"   already exited rc={rc}  elapsed={time.time()-t0:.1f}s")
