---
name: interpolate-direction
description: "interpolate_cameras_circle.py 圆形轨迹旋转方向的决定机制 + 2026-08 新增的 --direction 参数（auto/same/opposite）"
metadata:
  node_type: memory
  type: project
  originSessionId: 92430b36c357
---

# 插值圆轨迹旋转方向（CW/CCW）机制

**定位**: 解释 `tills_ply/interpolate_cameras_circle.py`（fuse_server Step 0 / ply_pipeline interpolate 步）生成的 `cameras_align.json` 环绕方向由什么决定，以及 2026-08 新增的方向控制参数。

## 方向是怎么决定的（原代码，无显式参数）

1. **圆拟合** `fit_circle()`：对关键帧位置 SVD → `u1, u2`（面内两轴）、`normal`（面法线）。角度定义为 `atan2(y, x)`，`(x,y)` 是位置在 `(u1,u2)` 基下的投影。
2. **采样方向写死**：`sample_angles = np.linspace(ang_a, ang_a + 2π, N)` —— 永远从锚点相机角度**单调递增**走满 360°，即永远沿 `(u1,u2)` 帧的 **+angle（u1→u2）** 方向。没有参数可翻转。
3. **normal 翻转**（`5de528d` 提交加入）：把法线翻到与相机平均 down 轴（`R[:,1]`）一致——只决定圆环上下朝向与 look-at 姿态，**不影响面内运动方向**。
4. **结论**：世界坐标里顺时针还是逆时针（以及是否与拍摄顺序同向），完全由 SVD 基 `u1/u2` 的朝向决定；SVD 对近圆点集面内轴符号任意、两奇异值近退化 → **方向与拍摄一致与否是数据巧合**。

## 实测（10 个数据集，max_index=89）

- 与拍摄方向**一致**（5）：03、0719、0720、0722、合并测试
- 与拍摄方向**相反**（5）：0721、0803、0805、0807、90-0803
- 拍摄方向在 `(u1,u2)` 帧内的平均角步长：全部为 ±4°/帧（90 帧转台），符号正负各半。
- 同一个 03 数据集，`max_index=44` 与 `max_index=89` 的 SVD 朝向甚至相反。

## 方向判据（坐标帧无关，已验证）

判据 A（推荐，全序稳健）——相邻关键帧在 `(u1,u2)` 平面的平均有符号角步长：

```python
proj = np.column_stack([(positions - center) @ u1, (positions - center) @ u2])
ang_steps = np.arctan2(proj[:-1,0]*proj[1:,1] - proj[:-1,1]*proj[1:,0],
                       proj[:-1,0]*proj[1:,0] + proj[:-1,1]*proj[1:,1])
mean_step = np.mean(ang_steps)   # >0 拍摄逆时针, <0 顺时针
```

前提：关键帧沿圆周**单调有序**；若来回摆动，需退回锚点局部切线判据（`t_cap=(p_{a+1}-p_{a-1})` 投影 vs 插值切线 `(-sin a_a, cos a_a)` 的点积符号）。

## 2026-08 新增 `--direction` 参数

`tills_ply/interpolate_cameras_circle.py` 新增 `--direction auto|same|opposite`（默认 `auto`）：
- `same`/`auto` → 沿拍摄方向（`dir_sign = sign(mean_step)`）
- `opposite` → 反向
- 实现：`dir_sign<0` 时 `sample_angles = linspace(ang_a, ang_a - 2π, N)`，半径/旋转/内参的插值函数按物理角度归一化，两种方向逐帧几何一致，仅遍历顺序相反（已验证 `same[i] == opposite[(N-i)%N]` 位置+旋转精确成立）。

**陷阱（已踩坑）**：反向遍历时锚点 B 的原始角度必须是
`ang_b_signed = ang_a - (2π - span)`（其物理角度是 `ang_a + span`），
写成 `ang_a - span` 会把 B 钉偏（span≠180° 时误差 ~2×|180°-span|）。

**透传链路**：`fuse_server.py run_fuse_clip`（Step 0 args）+ `ply_pipeline.py build_interpolate_args` 都加了 `--direction`；`presets.json` 全部 6 个 preset 与 `CameraData/_template/presets.json` 的 `interpolate` 段都加了 `"direction": "auto"`。

**行为变化提醒**：auto 现在跟随拍摄方向 → 之前 5 个"反向"数据集（0803/0805/0807/0721/90-0803）重跑 interpolate 后方向与旧版相反；想保持旧行为设 `opposite`。

## fuse 前端 UI 现状（2026-08 核对）

fuse_server 只有**两处** preset 编辑 UI（无第三处）：
1. 主页面「点击编辑Presets」→ `preset-modal` 弹窗（`pm-*` id 前缀）→ interpolate 区含 `pm-i-direction` 下拉
2. 独立 `/presets` 页面（`i-*` id 前缀）→ `i-direction` 下拉

页面是**服务端 f-string 渲染**：改 Python 后必须重启 fuse_server 进程（无热更新），浏览器再硬刷新（Ctrl+F5，虽然已全局 `Cache-Control: no-cache`）。改 JS 后按 CLAUDE.md 校验花括号平衡。

## 相关文件关系

- `tills_ply/interpolate_cameras_circle.py` —— 当前主用版本（fuse_server / ply_pipeline 都调它）
- `tills/interpolate_cameras_circle.py` —— **旧副本**，v1 管线 `run_pipeline.py` 还在用，**未同步** `--direction`
- `tills/bridge_interpolate.py` —— v2 UE 桥接，自带显式 `--direction cw|ccw`（与圆插值无关，别混）
- `tills_ply/ply_utils.py::fit_circle` —— fuse/clip 用的同款圆拟合（方向与插值共享同一 SVD 机制）
