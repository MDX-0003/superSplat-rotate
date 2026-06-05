# 3DGS 相机轨迹渲染管线

## 前提

本管线用于：给定一个场景的多机位多帧实拍数据 + COLMAP 重建结果，将相机 360° 环绕轨迹的 SuperSplat 渲染画面插入实拍序列，输出混合视频。

## 项目初始化

在 `CameraData/` 下创建项目目录，必须包含以下内容：

```
CameraData/<project>/
├── raw_frames/                    # 原始多机位帧 (必须)
│   ├── 0001/                      #   每帧一个子目录
│   │   ├── 001.jpg                #     相机1
│   │   ├── 002.jpg                #     相机2
│   │   └── ...                    #     (共63个相机)
│   ├── 0002/
│   └── ...                        #   (共168帧)
│
├── colmap_bins/                   # COLMAP bin 文件 (必须)
│   ├── cameras.bin
│   ├── images.bin
│   └── points3D.bin
│
└── *.ply                          # 3DGS 点云模型 (建议放项目根)
```

> **关键要求**：所有PLY的重建必须使用同一组相机内外参（COLMAP 对齐流程），确保坐标系统一。

## 一键运行

```bash
python tills/run_pipeline.py --project <project> --camera 6 --head 64 --tail 161
```

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `--project` | (必须) | CameraData 下的项目名 |
| `--camera` | `6` | 锚点相机编号（轨迹起点/终点） |
| `--head` | `64` | 实拍保留区间上界（1~head 保留） |
| `--tail` | `161` | 实拍保留区间下界（tail~末尾保留） |
| `--total` | `300` | SuperSplat 渲染帧数 |
| `--radius-scale` | `1.0` | 轨迹圆半径缩放比 |
| `--fps` | `60` | 输出视频帧率 |
| `--crf` | `6` | 视频质量（0=无损, 6=近无损） |

## 分步说明

管道共 6 步。Step 4 需要手动操作，其余全部自动。
每步如果输出已存在则自动跳过，支持断点续跑。

---

### Step 1 — COLMAP bin → cameras.json

| 项目 | 内容 |
|------|------|
| **脚本** | `tills/colmap_bin_to_json.py` |
| **输入** | `colmap_bins/cameras.bin` + `colmap_bins/images.bin` |
| **输出** | `cameras.json` — 63 台相机的内外参，同一坐标系 |
| **意义** | COLMAP 二进制格式转为管线可读的 JSON，每台相机包含 position / rotation / fx / fy / width / height |

```bash
python tills/colmap_bin_to_json.py --project <project>
```

---

### Step 2 — 提取锚点相机全部帧

| 项目 | 内容 |
|------|------|
| **脚本** | `tills/extract_camera.py` |
| **输入** | `raw_frames/0001~0168/00X.jpg` |
| **输出** | `anchor_frames/0001~0168.jpg` — 单相机（如 6 号）的全部时间序列 |
| **意义** | 从 63 台相机中抽出指定一台的连续帧，用于后续与渲染画面混合 |

```bash
python tills/extract_camera.py --project <project> --camera 6
```

---

### Step 3 — 圆插值生成 300 个环绕位姿

| 项目 | 内容 |
|------|------|
| **脚本** | `tills/interpolate_cameras_circle.py` |
| **输入** | `cameras.json`（63 台相机） |
| **输出** | `cameras_align.json` — 300 个位姿，360° 闭环 |
| **意义** | 在 63 台相机拟合出的空间圆上均匀采样 300 个位姿，起点=终点=锚点相机位置，旋转用 look-at 圆心 + 残差 slerp，画面已验证正确 |

```bash
python tills/interpolate_cameras_circle.py --project <project> --anchor-camera 006 --total 300 --radius-scale 0.85
```

**参数说明**：
- `--anchor-camera`：相机 img_name（3 位补零），如 `006` 表示 6 号相机
- `--radius-scale`：`<1` 靠近圆心（拍摄更紧凑），`>1` 远离圆心（视野更广）

---

### Step 4 — SuperSplat 渲染（手动）

| 项目 | 内容 |
|------|------|
| **输入** | `cameras_align.json` + `.ply` 点云模型 |
| **输出** | `renders/circle_0001~0300.png` — 300 张渲染图 |
| **意义** | 唯一手动步骤。主控脚本在此暂停并打印提示 |

**操作流程**：
1. 浏览器打开 SuperSplat
2. 拖入 `.ply` 模型
3. 菜单 → Import → 选择 `cameras_align.json`
4. 右侧工具栏 → 相机图标 → View Panel → GT Camera 区域
5. 设置分辨率（默认 HD 1920×1080）、Radius Scale 等
6. 点击 **Export All**
7. 浏览器自动下载 300 张 PNG 到 Downloads
8. 将 `circle_0*.png` 全部移入 `renders/` 目录
9. 回到终端按 **Enter** 继续

---

### Step 5 — 混合编排

| 项目 | 内容 |
|------|------|
| **脚本** | `tills/blend_frames.py` |
| **输入** | `anchor_frames/`（实拍）+ `renders/`（渲染） |
| **输出** | `blended/frame_0001~0372.{jpg,png}` |
| **意义** | 实拍帧 1~64 + 渲染 300 帧 + 实拍帧 161~168 → 统一编号序列 |

```bash
python tills/blend_frames.py --project <project> --head 64 --tail 161 --render-count 300
```

**输出结构**：
```
frame_0001~0064.jpg   ← 实拍（保留）
frame_0065~0364.png   ← SuperSplat 渲染（替换原 65~160 区间）
frame_0365~0372.jpg   ← 实拍（保留，原始序号 161~168 重编号）
```

---

### Step 6 — 编码 MP4

| 项目 | 内容 |
|------|------|
| **脚本** | `tills/pngs_to_mp4.py` |
| **输入** | `blended/frame_*.jpg` |
| **输出** | `output/frame.mp4` |
| **意义** | 372 帧图像序列 → H.264 视频，默认 60fps 近无损 |

```bash
python tills/pngs_to_mp4.py --project <project> --fps 60 --crf 6
```

---

## 项目完整目录结构（运行后）

```
CameraData/<project>/
├── config.json              # 自动生成，记录本次参数
├── raw_frames/              # [初始] 原始多机位帧
├── colmap_bins/             # [初始] COLMAP bin 文件
├── *.ply                    # [初始] 3DGS 点云
│
├── cameras.json             # [Step 1] 63 台相机内外参
├── anchor_frames/           # [Step 2] 锚点相机连续帧 (0001~0168.jpg)
├── cameras_align.json       # [Step 3] 300 个插值环绕位姿
├── renders/                 # [Step 4] SuperSplat 渲染 PNG (circle_0001~0300.png)
├── blended/                 # [Step 5] 混合序列 (frame_0001~0372)
└── output/                  # [Step 6] 最终视频 (frame.mp4)
```

## 脚本速查

| 脚本 | 功能 |
|------|------|
| `tills/paths.py` | 共享路径常量 |
| `tills/run_pipeline.py` | 主控脚本（一键跑全部） |
| `tills/colmap_bin_to_json.py` | COLMAP bin → cameras.json |
| `tills/extract_camera.py` | 提取指定相机的全部帧 |
| `tills/interpolate_cameras_circle.py` | 圆插值生成 300 环绕位姿 |
| `tills/interpolate_cameras.py` | 圆插值（旧版，角度域高斯平滑） |
| `tills/interpolate_cameras_arc.py` | 弧线插值（两帧间，非闭环，已弃用） |
| `tills/blend_frames.py` | 实拍+渲染混合编排 |
| `tills/pngs_to_mp4.py` | 图像序列 → MP4 |
| `tills/WorkFlow_1.md` | 自动化方案设计文档 |
| `tills/WORKFLOW.md` | 旧版工作流记录（CameraData/02 特化） |
