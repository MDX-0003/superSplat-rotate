# WorkFlow_1 — 全自动管线设计

## 核心理念

每次运行在 `CameraData/` 下创建一个项目文件夹。用户只需提供：

| 输入 | 说明 | 示例 |
|------|------|------|
| 原始多机位帧数据 | `NNNN/CCC.jpg` 结构 | `2026-06-04-214336/0001/006.jpg` |
| COLMAP bin 文件 | `cameras.bin` + `images.bin` + `points3D.bin` | 同一目录下 |
| 相机编号 | 作为旋转起始终点的相机 | `6` |
| head 截止帧 | 实拍保留区间的上界 | `64` |
| tail 起始帧 | 实拍保留区间的下界 | `161` |

所有中间产物放在项目文件夹的约定子目录下，路径可预测，脚本自动串联。

## 项目目录结构

```
CameraData/
└── <project>/                      # 每次运行在此创建
    ├── config.json                 # 记录本次参数 (自动生成)
    ├── input/                      # 用户自行放入的原始数据
    │   ├── raw_frames/             #   原始多机位帧 → 可放快捷方式/符号链接/直接拷贝
    │   │   ├── 0001/001~063.jpg
    │   │   └── ...
    │   └── colmap_bin/             #   cameras.bin + images.bin + points3D.bin
    │       ├── cameras.bin
    │       ├── images.bin
    │       └── points3D.bin
    │
    ├── cameras.json                # [Step 1] colmap bin → json
    ├── anchor_frames/              # [Step 2] 提取锚点相机的全部帧
    │   ├── 0001.jpg
    │   └── ...
    ├── cameras_align.json          # [Step 3] 圆插值 300 位姿
    ├── renders/                    # [Step 4] SuperSplat 渲染结果
    │   ├── circle_0001.png
    │   └── ...
    ├── blended/                    # [Step 5] 实拍+渲染混合序列
    │   ├── frame_0001.jpg
    │   └── ...
    └── output/                     # [Step 6] 最终视频
        └── final.mp4
```

## 管线步骤

```
Step 1 ──→ Step 2 ──→ Step 3 ──→ Step 4 ──→ Step 5 ──→ Step 6
 bin→json   提取相机   圆插值    SuperSplat   混合编排    编码MP4
                         ↑      (唯一手動)
```

### Step 1 — colmap_bin_to_json

```
输入:  input/colmap_bin/cameras.bin + images.bin
输出:  cameras.json
脚本:  tills/colmap_bin_to_json.py (待创建，调用现有 colmap 工具或自己解析 bin)
```

### Step 2 — 提取锚点相机

```
输入:  input/raw_frames/ (168帧 × 63相机)
       config: 相机编号 (如 6)
输出:  anchor_frames/0001~0168.jpg
脚本:  tills/extract_camera.py (已有，改路径指向项目目录)
```

### Step 3 — 圆插值

```
输入:  cameras.json (45个关键帧)
       config: 锚点相机ID, head_end, tail_start → 推算出 ANCHOR
输出:  cameras_align.json (300个位姿)
脚本:  tills/interpolate_cameras_circle.py (已有，改路径)
```

### Step 4 — SuperSplat 渲染 (唯一步驟需手動)

```
输入:  cameras_align.json + 对应的 .ply 点云
操作:  网页端加载 ply → 导入 align.json → Export All
输出:  renders/circle_0001~0300.png
```
> 这个步骤现在必须手动。但流水线脚本会在此暂停，打印提示信息，等待用户把 PNG 放入 `renders/` 后按回车继续。

### Step 5 — 混合编排

```
输入:  anchor_frames/ (实拍, 1~168)
       renders/ (SuperSplat, 1~300)
       config: head_end=64, tail_start=161
输出:  blended/frame_0001~0372.{jpg,png}
脚本:  tills/blend_frames.py (已有, 改路径)
```

### Step 6 — 编码视频

```
输入:  blended/frame_*.jpg
输出:  output/final.mp4
脚本:  tills/pngs_to_mp4.py (待创建, 从 CameraData/02 迁移并通用化)
```

## 主控脚本 `tills/run_pipeline.py`

```
用法: python tills/run_pipeline.py <project_dir> [options]

参数:
  --camera N          锚点相机编号 (默认: 6)
  --head N            实拍保留上界帧号 (默认: 64)
  --tail N            实拍保留下界帧号 (默认: 161)
  --total N           SuperSplat 渲染数量 (默认: 300)
  --radius-scale F    圆半径缩放 (默认: 0.85)

流程:
  1. 检查 <project_dir>/input 是否包含必要文件
  2. 生成 config.json 记录参数
  3. 依次执行 Step 1~6
  4. Step 4 处暂停，提示用户完成渲染后按回车
  5. 每步执行前检查输入是否存在，失败时打印清晰错误信息
```

## config.json 示例

```json
{
  "project": "mangoTV_dome",
  "camera": 6,
  "head_end": 64,
  "tail_start": 161,
  "total_renders": 300,
  "radius_scale": 0.85,
  "created": "2026-06-05T..."
}
```

## 需要新建/改造的脚本

| 文件 | 动作 | 说明 |
|------|------|------|
| `tills/run_pipeline.py` | **新建** | 主控脚本 |
| `tills/colmap_bin_to_json.py` | **新建** | bin → json 转换 |
| `tills/extract_camera.py` | **改造** | 路径改为接受 project_dir 参数 |
| `tills/interpolate_cameras_circle.py` | **改造** | 路径改为接受 project_dir 参数, ANCHOR 从 config 读取 |
| `tills/blend_frames.py` | **改造** | 路径改为接受 project_dir 参数 |
| `tills/pngs_to_mp4.py` | **新建** | 从 CameraData/02 迁移并通用化 |
| `tills/paths.py` | **新建** | 统一路径解析 (ROOT, 项目目录等) |

## 路径规则

所有脚本通过 `--project <name>` 参数定位项目目录：

```
项目根 = CameraData/<name>/
cameras.json     = 项目根 / "cameras.json"
anchor_frames/   = 项目根 / "anchor_frames"
cameras_align    = 项目根 / "cameras_align.json"
renders/         = 项目根 / "renders"
blended/         = 项目根 / "blended"
output/          = 项目根 / "output"
```

这样每个项目自包含，互不干扰，可以同时存在多个项目。
