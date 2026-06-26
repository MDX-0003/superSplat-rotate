---
name: pipeline-v4
description: "run_pipeline_v4.py — flat sequential images, 0-indexed, real→render→real concat, no COLMAP/UE dependency"
metadata: 
  node_type: memory
  type: project
  originSessionId: 200a40e4-a9fb-44c0-aaef-a4778c83a728
---

# Pipeline v4 (`tills/run_pipeline_v4.py`)

**定位**: 当前主力视频管线。将实拍序列 + SuperSplat 渲染 MP4 混合拼接为最终输出视频。

**何时使用**: 项目使用单一相机拍摄的连续帧（扁平 JPG），需要插入 SuperSplat 渲染段时。

## 输入 / 输出

```
CameraData/<project>/
  raw_images/                 # 扁平 JPG（或子目录）, 文件名如 "DJD-2026-06-22-214307 000.jpg"
  renders/                    # SuperSplat 渲染 MP4（手动步骤）
    seq_f075.mp4
  anchor_frames/              # [自动] 提取的实拍帧
  output/                     # [自动] 最终 output.mp4
  Train_imgs/<date>/          # [自动] 训练图（被渲染替换的源帧）
```

## Timeline JSON 格式

```json
{
  "fps": 25, "crf": 6, "resolution": "3840x2160",
  "source": "raw_images",
  "segments": [
    { "type": "real",   "start": 0,   "end": 74 },
    { "type": "render", "seq": "seq_f075", "replace_frames": 90 },
    { "type": "real",   "start": 165, "end": 238 }
  ]
}
```

- `start`/`end`: 0-indexed flat image indices
- `replace_frames`: 文档性质（该渲染替换了多少源帧），不影响逻辑
- `source`: 扁平图像目录名，默认 `"raw_images"`

## 执行流程

| Step | 操作 | 说明 |
|------|------|------|
| 0 | Source discovery | 自动检测文件名前缀 + 零填充宽度 |
| 1.5 | Extract train images | 将渲染替换的源帧复制到 `Train_imgs/` |
| 2 | Extract real frames | 从扁平 JPG 复制到 `anchor_frames/`，0-indexed 连续命名 |
| 3 | Wait for render MP4s | 手动步骤暂停，等待用户放入 `renders/` |
| 4 | JPGs → MP4 | 连续实拍段编码为 H.264 MP4 |
| 5 | TS concat → output.mp4 | 按 timeline 顺序拼接实拍+渲染 TS 流 |

## 命令行

```bash
python tills/run_pipeline_v4.py --project 09 --timeline tills/timeline/tl_09_01.json
python tills/run_pipeline_v4.py --project 09 --timeline ... --force --debug
```

- `--force`: 删除中间产物重跑
- `--debug`: 保留中间 TS/MP4 + 导出 output frames 为 PNG

**Why:** v4 是为扁平单相机拍摄场景设计的，无需 COLMAP 重建和 UE 虚拟相机。与 v3 的核心区别是去掉了 COLMAP bin → JSON 转换和 UE seq 依赖。

**How to apply:** 新建项目时，先准备好 `raw_images/` 扁平 JPG，编写 timeline JSON，然后运行 v4。渲染 MP4 需要手动在 SuperSplat 中完成。
