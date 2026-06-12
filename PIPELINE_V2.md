# 3DGS 相机轨迹渲染管线 v2 — UE 自定义轨迹 + MP4 拼接

## 前提

本管线用于：给定一个场景的多机位多帧实拍数据 + COLMAP 重建的 GT 相机 + UE Sequence 导出的复杂相机轨迹，自动在实拍相机和 UE 轨迹之间生成圆弧桥接段，输出 MP4 混合视频。

v1（圆轨迹）见 [PIPELINE.md](PIPELINE.md)，由 `run_pipeline.py` 驱动。

## 项目初始化

在 `CameraData/` 下创建项目目录，必须包含：

```
CameraData/<project>/
├── raw_frames/                    # 原始多机位帧 (必须)
│   ├── 0001/
│   │   ├── 001.jpg
│   │   └── ...                    # (共 63 个相机)
│   ├── 0002/
│   └── ...
│
├── colmap_bins/                   # COLMAP bin 文件 (必须)
│   ├── cameras.bin
│   ├── images.bin
│   └── points3D.bin
│
└── *.ply                          # 3DGS 点云模型
```

> **关键要求**：所有 PLY 的重建使用同一组相机内外参（COLMAP 对齐流程），确保坐标系统一。

## 一键运行

```bash
# 首尾都有实拍
python tills/run_pipeline_v2.py --project 02 \
    --gt-camera 006 --ue-seq SequenceData/01/cameras.json \
    --tail-gt-camera 032 \
    --head-segments "cam006:1-64" \
    --tail-segments "cam032:161-168"

# 仅开头接实拍，结尾以 UE 镜头结束
python tills/run_pipeline_v2.py --project 02 \
    --gt-camera 006 --ue-seq SequenceData/01/cameras.json \
    --head-segments "cam006:1-64"
```

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `--project` | (必须) | CameraData 下的项目名 |
| `--gt-camera` | (必须) | GT 锚点相机 img_name（桥接起点），如 `006` |
| `--ue-seq` | (必须) | UE 导出的序列 JSON 路径 |
| `--tail-gt-camera` | 无 | 尾部 GT 锚点相机（可选） |
| `--head-segments` | 无 | 开头实拍段，如 `"cam006:1-64"` |
| `--tail-segments` | 无 | 结尾实拍段，如 `"cam032:161-168"` |
| `--bridge-min-frames` | `30` | 桥接段最少帧数 |
| `--fps` | `60` | 输出视频帧率 |
| `--crf` | `6` | JPG→MP4 编码质量（0=无损, 6=近无损） |
| `--resolution` | `3840x2160` | 输出分辨率（须与 SuperSplat 导出一致） |

## 分步说明

管道共 6 步。Step 4 需要手动操作，其余全部自动。每步输出已存在则自动跳过，支持断点续跑。

---

### Step 1 — COLMAP bin → cameras.json

| 项目 | 内容 |
|------|------|
| **脚本** | `tills/colmap_bin_to_json.py` |
| **输入** | `colmap_bins/cameras.bin` + `colmap_bins/images.bin` |
| **输出** | `cameras.json` — 63 台 GT 相机的内外参 |
| **意义** | 桥接锚点 + 圆心计算 |

---

### Step 2 — 提取实拍帧段

| 项目 | 内容 |
|------|------|
| **脚本** | 内联在 `run_pipeline_v2.py` |
| **输入** | `raw_frames/` + `--head-segments` / `--tail-segments` |
| **输出** | `anchor_frames/0001~NNNN.jpg`（顺序编号：先 head 后 tail） |
| **意义** | 提取 JPG 原图用于后续编码 |

---

### Step 3 — 桥接插值 + UE 序列 → cameras_align.json

| 项目 | 内容 |
|------|------|
| **脚本** | `tills/bridge_interpolate.py` + `tills/merge_trajectory.py` |
| **输入** | `cameras.json`（63 台 GT）+ UE 序列 JSON |
| **输出** | `cameras_align.json` — 完整合成轨迹 |

**子步骤**：

1. 加载 `cameras.json` → 取所有 GT 相机 position 的质心作为旋转圆心
2. 加载 UE 序列 → 检测开头/结尾的水平面旋转方向（CW / CCW）
3. 生成 **Bridge1**：GT 锚点相机 → UE 序列第一帧
4. 生成 **Bridge2**（可选）：UE 序列最后一帧 → GT 尾锚点相机
5. 合并：`bridge1 + UE序列 + bridge2` → `cameras_align.json`

**桥接算法要点**：
- 沿 GT 相机质心垂直轴做圆弧
- 旋转方向跟随 UE 序列开头方向（最短弧方向相反时绕长弧）
- 帧数按角度差比例，最少 30 帧
- radius / height / fx / fy 线性过渡，look-at 始终指向圆心

---

### Step 4 — SuperSplat 渲染视频（手动）

| 项目 | 内容 |
|------|------|
| **输入** | `cameras_align.json` + `.ply` 点云模型 |
| **输出** | `renders/render.mp4` |

**操作流程**：

1. 浏览器打开 SuperSplat
2. 拖入 `.ply` 模型
3. 拖入 `cameras.json` → 弹窗选 **"Add to GT Cameras"**
4. 拖入 `cameras_align.json` → 弹窗选 **"Replace Timeline"**
5. 菜单 → **Render → Video**
6. 设置参数：
   - Resolution: **3840×2160**（或 `--resolution` 指定的值）
   - Frame Rate: **60**（或 `--fps` 指定的值）
   - Format: **MP4**
   - Codec: **H.264**
   - Bitrate: **High** 或 **Ultra**
   - Frame Range: **0 到 N-1**（终端会打印帧数）
7. 保存为 `render.mp4`
8. 将 `render.mp4` 移入项目 `renders/` 目录
9. 回到终端按 **Enter** 继续

---

### Step 5 — JPG → MP4（head / tail）

| 项目 | 内容 |
|------|------|
| **工具** | ffmpeg |
| **输入** | `anchor_frames/` head/tail 段的 JPG |
| **输出** | `head.mp4` / `tail.mp4` |

JPG 序列编码为 H.264 MP4，参数与 SuperSplat 导出一致：
- 编码器 `libx264`，CRF 6（近无损）
- 像素格式 `yuv420p`
- CFR 帧率对齐

---

### Step 6 — TS concat → output.mp4

| 项目 | 内容 |
|------|------|
| **工具** | ffmpeg（流拷贝，零画质损失） |
| **输入** | `head.mp4` + `renders/render.mp4` + `tail.mp4` |
| **输出** | `output/output.mp4` |

**流程**：
```
head.mp4  ──→ h264_mp4toannexb → head.ts   ──┐
render.mp4 ──→ h264_mp4toannexb → render.ts ──┤ concat → output.mp4
tail.mp4  ──→ h264_mp4toannexb → tail.ts   ──┘
```

全部 `-c copy`（流拷贝），不重新编码，画质无损失。前提：三段分辨率、帧率、像素格式一致——由 Step 4-5 保证。

---

## 项目完整目录结构（运行后）

```
CameraData/<project>/
├── config.json              # 自动生成
├── raw_frames/              # [初始] 原始多机位帧
├── colmap_bins/             # [初始] COLMAP bin 文件
├── *.ply                    # [初始] 3DGS 点云
│
├── cameras.json             # [Step 1] GT 相机内外参
├── anchor_frames/           # [Step 2] 实拍帧 (0001~NNNN.jpg)
├── cameras_align.json       # [Step 3] 完整合成轨迹
├── renders/                 # [Step 4] render.mp4（手动放入）
├── head.mp4 / tail.mp4      # [Step 5] 中间产物（自动删除）
└── output/
    └── output.mp4           # [Step 6] 最终视频
```

## 脚本速查

| 脚本 | 功能 |
|------|------|
| `tills/paths.py` | 共享路径常量 |
| `tills/run_pipeline.py` | v1 主控脚本（圆轨迹 → blend PNGs） |
| `tills/run_pipeline_v2.py` | **v2 主控脚本（UE轨迹+桥接 → MP4 concat）** |
| `tills/colmap_bin_to_json.py` | COLMAP bin → cameras.json |
| `tills/extract_camera.py` | 提取指定相机/帧段 |
| `tills/interpolate_cameras_circle.py` | 圆插值生成环绕位姿（v1） |
| `tills/bridge_interpolate.py` | 圆弧桥接位姿生成（v2） |
| `tills/merge_trajectory.py` | 桥接段+UE序列合并（v2） |
| `tills/blend_frames.py` | v1 实拍+渲染 PNG 混合编排 |
| `tills/pngs_to_mp4.py` | v1 PNG 序列 → MP4 |
