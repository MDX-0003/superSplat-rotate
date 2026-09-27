---
name: interpolate-sampling-residual
description: "interpolate_cameras_circle.py 的两处平滑性缺陷与 2026-09-27 修复：采样角被 pin 到远锚点造成一帧顿挫；残差混合的 t%1.0 造成锯齿/2 倍速率/远锚点残差从未生效"
metadata:
  node_type: memory
  type: project
---

# circle 插值轨迹的两处平滑性缺陷（2026-09-27 修复）

**定位**: `tills_ply/interpolate_cameras_circle.py` —— fuse_server Step 0 与 ply_pipeline interpolate 步共用的脚本，产出 `cameras_align.json`（转一圈）。两处缺陷都会让成片在**固定方位**出现抖动/顿挫，与参数无关（`radius_scale` / `height_offset` / `pitch_offset` / `fov_x` 全都帮不上）。

**为什么特别难查**: 脚本没有任何"平滑"步骤可供调节。位置/半径/残差/内参**全部是角度的解析函数**，看起来天然光滑；缺陷藏在"角度序列怎么排"和"混合权重怎么算"这两处，而日志只打印半径和 span，看不出采样间距或权重曲线。用户从 UI 上能看到的所有 interpolate 参数都不影响这两处。

## 背景：先理解模型，否则看不懂 bug

一次 `cameras_align.json` 的生成分四步（`main()`）：

1. **只拟合一个圆**：对所有关键帧位置做 SVD 求平面 + 最小二乘求圆心 → `center / normal / u1 / u2 / span`（`fit_circle`）。这是**整条链路唯一的平均步骤**。
2. **只留两个锚点**：anchor A = `--anchor-camera` 指定的机位；anchor B = 角向离它最远的那个关键帧（`best_j`）。其余关键帧姿态**全部丢弃**。
3. **位置 = 角度的纯函数**：`pos = angle_to_3d(θ, r(θ), center, u1, u2)`，半径 `r(θ)` 在 `r_a`（A 的半径）与 `r_b`（B 的半径）之间按 `_wrap_progress` 过渡。
4. **朝向 = look-at 圆心 × 残差 slerp**：`look-at` 是"看向圆心"的标准姿态；再乘上两个锚点各自的 SfM 残差（`residual = look_at⁻¹ · R_anchor`，即真实解算姿态相对纯 look-at 的偏差）之间的 slerp，把 SfM 解出的真实朝向找回来。

**关键不变量（两处 bug 都违反了它）**：既然姿态是角度的函数，**半径 / 残差 / 内参三个混合必须共用同一个"进度映射" `_wrap_progress(θ)`**，它在一圈里是 `0 →(远锚点)→ 1 →(接缝)→ 0` 的单峰三角形。任何一个用了别的映射，就会在锚点处与另外两个错位、或在接缝处不闭合。修复后代码里这个不变量是显式的：三个混合都调同一个 `_wrap_progress`。

---

## 缺陷 1：采样角被 pin 到远锚点 → 一圈固定方位的一帧顿挫

### 旧代码（`build output angles` 段）

```python
sample_angles = np.linspace(ang_a, ang_a + dir_sign * 2 * np.pi, N, endpoint=False)
# ensure anchors land exactly at their positions
sample_angles[0] = ang_a
# find the closest sample to ang_b and pin it
idx_b = np.argmin(np.abs(sample_angles - ang_b_signed))
sample_angles[idx_b] = ang_b_signed      # ← 元凶
```

### 为什么错

均匀采样每步 `2π/N`，把**离远锚点最近的那一个采样**硬拽到 `ang_b` 上，位移量最多半个步长。于是：

- 进入该帧的步长缩短，走出该帧的步长拉长，**各最多半个步长**；
- 极端情况下前后两步变成 `0.5×` 和 `1.5×` 标称步长 → 该帧"爬行"、下一帧"冲刺"，肉眼看就是一帧顿挫；
- 因为"离起始机位角向最远的那个关键帧"相对起始机位是**固定方位**，所以抖动永远出现在同一个位置；
- `total` 越小（步长越大）越明显。

### 实测（项目 06，`total=160`，标称 `2.25°/帧`）

| | 位移步长 | 转角步长 |
|---|---|---|
| 旧（带 pin） | 0.0519 ~ **0.1486** m（中位 0.1001） | 1.177 ~ **3.299** deg |
| 新（去掉 pin） | 0.099888 ~ 0.100237 m | 2.2256 ~ 2.2745 deg |

抖动**只在一处**：数组下标 80→81（`circle_0081` → `circle_0082`；SuperSplat 的时间轴帧号 = 数组下标），实际是 **1.167° 然后 3.341°**，即 −48.1% / +48.5%。其余 158 帧是平滑缓变的弧（±0.17%）。

### 新代码

```python
sample_angles = np.linspace(ang_a, ang_a + dir_sign * 2 * np.pi, N, endpoint=False)
idx_b = int(np.argmin(np.abs(sample_angles - ang_b_signed)))   # 仅用于日志
```

**这一步是外科手术式的**：只删两行 pin（不动残差）时，项目 06 上新旧 JSON **只有 1 帧不同** —— 正是原来顿挫的那一帧（下标 81 / `circle_0082`），它移动了 0.0483 m / 1.0733°（= 被拽掉的 1.083° 角位移对应的弧长），其余 159 帧**含第 0 帧逐字节相同**。因为 `sample_angles` 只有那一个元素被改过，而每帧姿态只由自己的角度决定，所以影响范围必然是单帧。

### 为什么两个锚点都不需要 pin

- **锚点 A**：`np.linspace` 的第一个元素**本来就精确等于** `ang_a`（numpy 显式赋值 `y[0] = start`），`sample_angles[0] = ang_a` 是空操作。
- **锚点 B**：半径 / 残差 / 内参三个混合**都是连续角度的函数**，没有任何一处依赖"有采样正好落在 `ang_b` 上"。均匀采样天然"扫过" `ang_b`，`_wrap_progress` 在那一侧自动接近 1（项目 06 实测最近采样达 **99.4%**，角向偏差 1.083° ≈ 0.48 个步长）。
- 代价：输出里不再有哪一帧逐比特等于原始远锚点姿态。但 `radius_scale=1` 时第 0 帧仍精确复现起始机位姿态（角度精确、残差取 `residual_a`），`radius_scale≠1` 时半径本来就按比例缩放，谈不上"精确"。

---

## 缺陷 2：残差混合的 `t % 1.0` → 锯齿 + 2 倍速率 + 远锚点残差从未生效

这是本次一并修掉的第二处（用户授权"一起修改"）。

### 旧代码（旋转循环内）

```python
a_mod = (a - ang_a) % (2 * np.pi) + ang_a
if a_mod <= ang_b:
    t = (a_mod - ang_a) / span                 # 0 → 1  (跨 span 段)
else:
    t = 1.0 + (a_mod - ang_b) / (2 * np.pi - span)   # 1 → 2  (回程段)
t = t % 1.0                                    # ← 元凶

if t <= 0.5:
    frac = t / 0.5                             # 0→1: anchor_a → anchor_b
    residual = Slerp(["a","b"])(frac)
else:
    frac = (t - 0.5) / 0.5                     # 0→1: anchor_b → anchor_a
    residual = Slerp(["b","a"])(frac)
```

### 错在哪：`t` 的定义自相矛盾

代码注释里写的意图是"**t 在整圈上是 0→1 的均匀进度，t=0.5 对应远锚点**"：前半段 a→b，后半段 b→a。两个分支和 slerp 方向**都是按这个模型写的，而且是对的**。

但 `t` 的实际计算与这个模型不符：跨 span 段用 `t = (a_mod-ang_a)/span`，在 `ang_b` 处已经**到 1.0**（不是 0.5）。作者发现这一点后，用 `t = t % 1.0` 去"补"，结果是把回程段的 1→2 折回 0→1，于是：

1. **远锚点处 `t` 归零** → 在远锚点处 `frac = 0` → **远锚点自己的 SfM 残差从未被应用**（实测第 81 帧只走到 `res_b` 的 **1.1%**），残差反而在两个"腿的中点"（第 40、120 帧）达到满值 —— 语义完全反了；
2. **一圈里残差走满 2 个来回**（`t` 被折两次：一次被 `t<=0.5` 分支折，一次被 `t%1.0` 折）→ 残差**变化速率是设计值的 2 倍**（实测 0.028°/帧，应为 0.014°/帧）；
3. **一圈里出现 3 次方向反转**（实测第 40、81、120 帧）而不是设计意图的 2 次（远锚点 + 接缝）→ 三处角速度突变。

### 新代码

```python
# 一圈里一个 0→1→0 单峰：锚点处 0（SfM 姿态精确复现）、远锚点处 1（同样精确）、
# 接缝处回到 0。只由角度驱动。
frac = _wrap_progress(a, ang_a, ang_b, span)
residual = _slerp(residual_a, residual_b, frac)
```

**为什么不需要第二个分支、也不需要 wrap**：`_slerp(a→b, frac)` 里的 `frac` 在回程段是**下降**的，slerp 自动从 b 回到 a。方向反转不需要用"换一对端点"来表达 —— 那正是旧代码复杂化并写错的根源。同理不需要 `max/min` 截断，`_wrap_progress` 天然落在 `[0,1]`。

顺带把半径和内参也统一到同一个 `_wrap_progress`：

- `radius_at_angle` / 内参循环原先各自**复制粘贴**了一遍同样的映射（且那一份是对的，用了 `1.0 - ...` 而不是 `t % 1.0`）。三份重复实现 + 其中一份写错，正是这个 bug 的成因，所以合并成单一定义。
- 合并后**位置与内参逐字节不变**（已验证），只有旋转改变。

### 实测对比（项目 06）

| | 残差方向反转帧 | 每帧残差变化 | 远锚点处应用了多少 `res_b` |
|---|---|---|---|
| 旧 | 40, 81, 120（3 次） | 0.028°/帧 | **1.1%** |
| 新 | 81（1 次，即远锚点） | 0.014°/帧 | **100.0%** |

转角步长极差从 2.20% 降到 1.09%。

---

## 两处缺陷的量级对照（为什么只有第 1 处值得用户报"抖动"）

| 缺陷 | 单帧异常幅度 | 视觉感受 |
|---|---|---|
| 采样角 pin | **±48%**（约 18 mm/帧 @2.68 m 半径） | 明显的一帧顿挫 |
| 残差锯齿 | **±1.1%**（约 0.4 mm/帧） | 肉眼不可见（但语义错误，必须修） |

**残留（有意不改）**：`_wrap_progress` 是**线性三角形，C0**，所以一圈里仍有 2 个"角"（远锚点、接缝），每次角速度方向反转 ≈ `2 × 0.014 = 0.028°/帧`，占基准 `2.24°/帧` 的 **1.1%** —— 低于本设计的噪声水平，故意不动。若将来真的看到它，一行即可改成 C1（两端导数为 0）：

```python
frac = 0.5 * (1.0 - math.cos(math.pi * _wrap_progress(a, ang_a, ang_b, span)))
```

**不要做**：不要为了"统一风格"把 swing 的 `2.0 * _triangular(p)` 搬到 circle 上 —— `tri(p)` 会把单峰折成**双峰**（因为 `p` 在一圈里本来就是 0→1→0），远锚点处 `frac` 又会退回 0，等于把缺陷 2 换个写法重新引入。

---

## 与 swing 脚本的异同（别盲目"统一"）

| | circle | swing |
|---|---|---|
| 角度 profile | `linspace` 均匀铺满 360° | 半隐式升余弦，**首尾逐比特闭合** |
| 循环播放 | **不是闭环**：接缝正好差 1 个步长（06 实测 0.099888 m / 2.2622°） | 闭环，且有硬门禁 `d_pos>1e-5` 直接 `sys.exit(1)` |
| 残差斜率 | `frac = p`（斜率 1） | `frac = 2·min(p,1−p)`（斜率 2） |
| 端点 | 远锚点处 `frac = 1`（必须） | 永远到不了远锚点（摆幅 ±30° 时 `p` 只到 ~0.17） |

**为什么残差斜率故意不同**：circle 会一路走到远锚点，`frac` 必须在 `p=1` 处精确等于 1；swing 只在小弧内摆动，用 2× 斜率让它多带一点残差。**不要为了"一致"把两者调成同一个公式** —— 那必然错一头。

另外 swing 的 `--residual-blend none|auto|full` 是**用户可调**且被 fuse_server 透传的（`fuse_server.py` 只把它拼进 swing 那一组参数）；circle 没有这个 CLI，其残差行为是硬编码的单一 `auto`。想给 circle 加 knob 时注意这点。

---

## 可复现的验证方法（下次改动请照做）

```bash
# 1) HEAD 版本必须能逐字节复现用户交付的文件 —— 先证明"我的理解就是他的出片路径"
git show HEAD:tills_ply/interpolate_cameras_circle.py > /tmp/_orig.py
python /tmp/_orig.py --path CameraData/06 --max-index 89 --total 160 \
    --anchor-camera 001 --radius-scale 0.95 --fov-x 70 --direction auto \
    --output /tmp/_orig_out.json
python -c "import json;print(json.load(open('/tmp/_orig_out.json'))==json.load(open('CameraData/06/cameras_align_old.json')))"
# True

# 2) 隔离验证：从 HEAD 版本只删掉两行 pin 生成基线，再和"两处都修"的版本对比，
#    位置/内参/fov 必须逐字节相同（只有 rotation 允许变）。
```

其他判据（不依赖视觉）：

- **一帧尖峰检测**：`任一帧步长偏离左右邻居均值 > 5%` → 修好后为 `False`。
- **步长极差**：`max/min - 1`。位置应 ~0.35%（那是 `r_a→r_b` 的单峰半径过渡，正常），转角应 ~1.1%。
- **残差反转帧**：从输出反算 `residual = look_at(pos)⁻¹ · R`，数方向反转次数 → circle 应为 1 次（远锚点）。

---

## 改动清单与"不要做的事"（防回归）

改动（2026-09-27，仅 `tills_ply/interpolate_cameras_circle.py`）：

1. 删除 `sample_angles[0] = ang_a` 与 `sample_angles[idx_b] = ang_b_signed` 两行 pin，`idx_b` 降级为日志用途；日志文案同步改成"far anchor angle … not pinned; nearest sample …"。
2. 新增 `_wrap_progress()` / `_slerp()` 两个共享函数（与 swing 同名同实现），半径 / 残差 / 内参**三处混合统一调用**，删掉 `interp_linear()`。
3. 残差混合的分支 + `t % 1.0` 全部删除，改为单行 `_slerp(residual_a, residual_b, _wrap_progress(...))`。

**不要做的事**：

- ❌ 不要为了"让锚点精确落位"把 pin 加回来（两行）。远锚点不需要有采样落在它上面。
- ❌ 不要把 `interp_linear` 那类映射再复制一遍到第四处 —— 三份重复实现就是这次 bug 的温床。
- ❌ 不要给残差混合再引入 `% 1.0` / `t<=0.5` 之类的分段；`frac` 的升降已经表达了方向。
- ❌ 不要照搬 swing 的 `2·tri(p)`（见上文，会变双峰）。
- ❌ 不要给 circle 脚本补一个"闭环"自检就以为等价于 swing —— circle 设计上就不是闭环（转一圈），接缝差 1 个步长是正常的；但如果把 circle 视频**循环播放**，接缝会跳一个步长，需要闭环请用 swing。

## 相关文件

- `tills_ply/interpolate_cameras_circle.py` —— 本次修复对象（fuse_server / ply_pipeline 都调它）
- `tills_ply/interpolate_cameras_swing.py` —— 兄弟脚本，`_wrap_progress` 的来源；**未改动**（无此两处缺陷）
- `tills/interpolate_cameras_circle.py` —— **旧副本**，v1 管线 `run_pipeline.py` 仍在用，**未同步本次修复**（也没有 `--direction`，见 [[interpolate-direction]]）
- `tills/server/fuse_server.py` —— Step 0 拼参数调上述脚本；`residual_blend` 只透传给 swing
- 方向机制 / `--direction` 参数 → [[interpolate-direction]]
- preset 与三步流程 → [[ply-pipeline]]
