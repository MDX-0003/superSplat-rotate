# Win11 主副机 SSH 免密配置指南

> v7 分布式管线前置步骤 · 2 台全新 Win11 机器

---

## 原理

```
主机 (私钥)  ──SSH──→  副机 (公钥)
   🔑                     🔒
   签名                   验证签名
```

- 主机手里有一把**私钥**（秘密，不传出去）
- 副机 `authorized_keys` 里存了主机那把私钥对应的**公钥**（可以公开）
- 连接时副机用公钥出一道题，只有持有私钥的主机能解开 → 身份通过 → 免密

Win11 有两个坑：
1. 副机需先安装 OpenSSH Server（`Add-WindowsCapability`）
2. Administrator 用户需把公钥写入 `C:\ProgramData\ssh\administrators_authorized_keys` 而非 `~/.ssh/authorized_keys`

---

## 操作步骤

> 以下命令如未标注"副机"，均在**主机**上执行。
> `<副机IP>` 替换为副机局域网 IP（`ipconfig` 查看 IPv4 地址）。

### ① 副机：安装并启动 SSH Server

在副机上用**管理员 PowerShell** 执行：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'
```

**原理**：Win11 不预装 SSH Server，需手动添加。`StartupType Automatic` 保证重启后自动运行。

### ② 主机：生成密钥对（如已有可跳过）

```powershell
mkdir "$env:USERPROFILE\.ssh" -Force
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\id_ed25519" -N '""'
```

**原理**：`ed25519` 是一种现代、短小、安全的密钥算法。`-N '""'` 表示密钥本身不设密码（否则每次 SSH 还要输密钥密码，失去免密意义）。-f用于指定密钥生成位置。

这条命令一定会生成id_ed25519（私钥，保留本机）和id_ed25519.pub（公钥，传给副机），持有公钥的副机才能被主机通过ssh访问。


### ③ 主机：修复私钥权限

```powershell
cmd /c 'icacls "%USERPROFILE%\.ssh\id_ed25519" /inheritance:r /grant:r "%USERNAME%:R"'
```

**原理**：Windows 默认让所有用户能读私钥文件，SSH 认为这太不安全所以拒绝使用。此命令移除其他用户的读取权限，仅保留你自己。

### ④ 把公钥传到副机

```powershell
ssh <副机IP> "mkdir C:\temp"
scp "$env:USERPROFILE\.ssh\id_ed25519.pub" Administrator@<副机IP>:C:/temp/host_key.pub
```

第一次连接会提示 `fingerprint`，输入 `yes` 确认。`scp` 需要输一次副机密码（最后一次）。

### ⑤ 副机：把公钥存入系统级授权文件

SSH 进副机（输密码）：

```powershell
ssh Administrator@<副机IP>
```

在副机 shell 中执行：

```cmd
type C:\temp\host_key.pub >> C:\ProgramData\ssh\administrators_authorized_keys
exit
```

回到主机，收紧副机上 `administrators_authorized_keys` 的权限并重启 sshd（SSH 要求该文件只能被 SYSTEM 和 Administrators 读取，否则拒绝使用）：

```powershell
ssh Administrator@<副机IP> "cmd /c 'icacls \"C:\ProgramData\ssh\administrators_authorized_keys\" /inheritance:r /grant:r \"SYSTEM:R\" /grant:r \"BUILTIN\Administrators:R\"'"
ssh Administrator@<副机IP> "powershell Restart-Service sshd"
```

**原理**：

- Administrator 属于 Administrators 组，Windows OpenSSH 会忽略 `%USERPROFILE%\.ssh\authorized_keys`，转而去读 `C:\ProgramData\ssh\administrators_authorized_keys`。该路径在步骤①安装 OpenSSH Server 时自动生成。
- `type` 是 cmd 里等价于 Unix `cat` 的命令，`>>` 是追加重定向（`>` 则覆盖）。注意必须用 cmd 的 `type` 来写，PowerShell 的 `>>` 会输出 UTF-16 编码，sshd 不认。
- **权限收紧是必须的**：如果 `administrators_authorized_keys` 允许 SYSTEM/Administrators 以外的人读取（如 `Authenticated Users`），sshd 认为不安全，直接拒绝使用该文件。
### ⑥ 验证

```powershell
ssh Administrator@<副机IP> "echo SUCCESS"
```

输出 `SUCCESS` 且未提示密码 → 完成。

---

## 故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| `ssh: connect to host ... port 22: Connection refused` | 副机 sshd 未启动 | 副机执行 `Start-Service sshd` |
| `Permission denied` 仍然要密码 | 公钥未写入或路径错误 | 确认是 `C:\ProgramData\ssh\administrators_authorized_keys` 而非 `~/.ssh/authorized_keys`；全网格场景下主机自身也需写入 |
| `Permission denied`，但账户无密码，直接回车被拒 | Administrator 空密码 + Windows 安全策略禁止空密码网络登录 | 给 Administrator 设密码：`net user Administrator "密码"` |
| `Bad permissions` / `UNPROTECTED PRIVATE KEY FILE` | 私钥权限太宽 | 重新执行步骤 ③ |
| `ssh -vvv` 日志中完全没有 `Offering public key` | 客户端 `.ssh` 目录或私钥权限太宽 | 检查 `icacls "$env:USERPROFILE\.ssh"` 和 `icacls "$env:USERPROFILE\.ssh\id_ed25519"`，如有 `BUILTIN\Users` 则收紧 |
| `ssh -vvv` 日志显示 `Offering public key` 后立刻 `receive packet: type 51`（拒绝） | 服务端 `administrators_authorized_keys` 权限太宽（含 `Authenticated Users`） | `icacls "C:\ProgramData\ssh\administrators_authorized_keys"` 检查，执行权限收紧 + `Restart-Service sshd` |
| `ssh-keygen -lf administ*...` 报 "not a public key file" | 文件被 PowerShell 的 `>>` 写成了 UTF-16 编码 | 用 `cmd /c 'type ...pub > ...'` 重建文件 |
| `Permission denied` 各种排查都正常 | 用户名拼写错误 | 确认是 `Administrator`（非 `Adminstrator`） |
| 重启后副机连不上 | sshd 未设开机自启 | 副机执行 `Set-Service -Name sshd -StartupType 'Automatic'` |
| 副机 IP 变了（DHCP） | 路由器重新分配 IP | `workers.json` 里改用 `hostname` 字段或固定副机 IP |

**排查技巧**：副机连主机失败时，用详细日志定位具体环节：

```powershell
ssh -vvv Administrator@<主机IP> 2>&1 | Select-String "Offering|pubkey|Authentication|denied|refused|identity"
```

---

## ⑦ 多机扩展

### 场景一：只需主机发起连接（单向）

第 3/4/5 台副机：对每台新机器重复 **① → ④ → ⑤ → ⑥**，无需重新生成密钥。**所有副机使用同一把主机的公钥**。

最终效果：主机 → 任意副机免密。

---

### 场景二：每台机器都需要互相 SSH（全网格）

v7 分布式管线中，调度器可能动态分配任务，**任何一台机器都可能是"主机"，去 SSH 另一台**。

核心思路：**共用同一对密钥，公钥写入所有机器的 authorized_keys，私钥拷贝到所有机器。**

#### 阶段 A：建立公钥基础设施（不涉及私钥扩散）

**首先**，在机器 1（首次生成密钥的那台**主机**）上，把公钥写入自己的 `authorized_keys` 并收紧权限：

```powershell
cmd /c 'type "%USERPROFILE%\.ssh\id_ed25519.pub" >> "C:\ProgramData\ssh\administrators_authorized_keys"'
cmd /c 'icacls "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant:r "SYSTEM:R" /grant:r "BUILTIN\Administrators:R"'
Restart-Service sshd
```

> 主机自身也需要配，否则全网格中其他机器 SSH 进来时会退回到密码验证。

然后，对机器 2/3/4/5 逐一：

1. 副机执行**步骤①**（安装 SSH Server）
2. 主机把**已有的** `.pub` 传到副机：**步骤④**
3. 副机写入 `administrators_authorized_keys`：**步骤⑤**
4. 验证主机→副机免密：**步骤⑥**

> ⚠️ **此时私钥还没有扩散。** 先确保公钥基础设施 100% 正确，再动下一步。

#### 阶段 B：扩散私钥（在公钥已验证的前提下）

对每台**副机**（机器 2/3/4/5），从主机拷入私钥并修权限：

```powershell
# 在主机上执行，把私钥传到副机
scp "$env:USERPROFILE\.ssh\id_ed25519" Administrator@<副机IP>:C:/temp/id_ed25519

# SSH 进副机
ssh Administrator@<副机IP>
```

在副机上执行：

> ⚠️ SSH 进 Windows 默认打开的是 **cmd**，而下面命令用的是 PowerShell 语法。在副机上先输入 `powershell` 回车，切到 PowerShell 后再执行。

```powershell
# 把私钥挪到正确位置
mkdir "$env:USERPROFILE\.ssh" -Force
move C:\temp\id_ed25519 "$env:USERPROFILE\.ssh\id_ed25519"

# 修复私钥权限（同步骤③，关键！）
cmd /c 'icacls "%USERPROFILE%\.ssh\id_ed25519" /inheritance:r /grant:r "%USERNAME%:R"'
```

#### 阶段 C：验证全网格

从每台机器，选另一台测试：

```powershell
ssh Administrator@<另一台IP> "echo SUCCESS"
```

全部返回 `SUCCESS` → 任意两台之间免密互通 ✅

---

> 💡 **为什么私钥放在阶段 B？** 如果先扩散私钥再验证公钥，一旦 authorized_keys 配置有错，排查范围从 1 台变成 N 台。先让公钥链路跑通，再把私钥拷贝出去，出错时只需怀疑"私钥拷贝/权限"这一个环节。