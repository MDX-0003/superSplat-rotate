# 3DGS 相机轨迹渲染管线 v3 — JSON Timeline + MP4 Concat

## 前提

v3 假设**所有虚拟运镜由 UE 端完成**。UE 导出的 JSON 直接在 SuperSplat 中使用，无需桥接。一条 JSON timeline 组合多段实拍和多段 UE 序列，输出一个完整 MP4。

v1（圆轨迹）→ [PIPELINE.md](PIPELINE.md)  
v2（UE + 桥接）→ [PIPELINE_V2.md](PIPELINE_V2.md)

## 项目初始化

```
CameraData/<project>/
├── raw_frames/              # [初始] 原始多机位帧
│   ├── 0001/
│   │   ├── 001.jpg
│   │   └── ...              # 共 63 个相机
│   ├── 0002/
│   └── ...
│
├── colmap_bins/             # [初始] COLMAP bin（可选，有则生成 cameras.json）
│   ├── cameras.bin
│   ├── images.bin
│   └── points3D.bin
│
├── *.ply                    # [初始] 3DGS 点云
│
├── timeline_02.json         # [手动创建] 时间线定义（见下）
│
├── ue_seqs/                 # [手动放入] UE 导出的相机轨迹 JSON
│   ├── seq_032.json
│   └── seq_015.json
│
└── renders/                 # [Step 3 手动放入] SuperSplat 导出的 MP4
    ├── seq_032.mp4
    └── seq_015.mp4
```

## Timeline JSON 格式

```json
{
  "fps": 60,
  "crf": 6,
  "resolution": "3840x2160",
  "segments": [
    { "type": "real",  "camera": 6,  "start": 1,  "end": 31 },
    { "type": "render", "seq": "seq_032" },
    { "type": "real",  "camera": 32, "start": 33, "end": 66 },
    { "type": "render", "seq": "seq_015" },
    { "type": "real",  "camera": 15, "start": 68, "end": 168 }
  ]
}
```

| 字段 | 含义 |
|------|------|
| `fps` | 全局帧率（默认 60） |
| `crf` | JPG→MP4 的 H.264 编码质量（0=无损, 6=近无损） |
| `resolution` | 全局输出分辨率，如 `"3840x2160"` |
| `segments` | 时间线片段数组 |
| `segments[].type` | `"real"` = 实拍, `"render"` = SuperSplat 渲染 |
| `segments[].camera` | (real) 相机编号，如 `6` |
| `segments[].start` / `end` | (real) 帧号范围，闭区间 |
| `segments[].seq` | (render) 序列名，对应 `ue_seqs/{seq}.json` 和 `renders/{seq}.mp4` |

## 一键运行

```bash
python tills/run_pipeline_v3.py --project 03 --timeline tills/timeline/tl_03_01.json
```
可以在后面加--debug
## 分步说明

### Step 1 — COLMAP bin → cameras.json

与 v1 相同。无 `colmap_bins/` 时可跳过，但 SuperSplat 中无 GT 参考。

### Step 2 — 提取实拍帧

自动解析 timeline 中所有 `type: "real"` 片段，从 `raw_frames/` 提取对应相机的帧号范围到 `anchor_frames/`。输出文件顺序编号（0001, 0002, ...），连续实拍段自动合并编码。

### Step 3 — SuperSplat 渲染视频（手动）

对每个 `type: "render"` 的 seq：

1. 浏览器打开 SuperSplat
2. 拖入 `.ply` 模型
3. 拖入 `cameras.json` → 弹窗选 **Add to GT Cameras**
4. 拖入 `ue_seqs/{seq}.json` → 弹窗选 **Replace Timeline**
5. 菜单 → **Render → Video**
   - Resolution: 与 timeline JSON 一致
   - Frame Rate: 与 timeline JSON 一致
   - Format: MP4
   - Codec: H.264
   - Bitrate: High 或 Ultra
6. 另存为 `{seq}.mp4`，移入 `renders/`
7. 回到终端按 Enter

### Step 4 — JPG → MP4

实拍帧按连续组打包编码为 H.264 MP4（CRF 6，近无损）。

### Step 5 — TS concat → output.mp4

全部 MP4 流拷贝（`-c copy`）拼接到一起，零画质损失。

## 项目完整目录结构（运行后）

```
CameraData/<project>/
├── config.json
├── timeline_02.json
├── raw_frames/              # [初始]
├── colmap_bins/             # [初始]
├── *.ply                    # [初始]
│
├── cameras.json             # [Step 1] GT 相机内外参
├── ue_seqs/                 # [初始] UE 轨迹 JSON
│   ├── seq_032.json
│   └── seq_015.json
├── anchor_frames/           # [Step 2] 实拍帧
├── renders/                 # [Step 3] SuperSplat MP4
│   ├── seq_032.mp4
│   └── seq_015.mp4
└── output/
    └── output.mp4           # [Step 5] 最终视频
```

## 与 v2 的区别

| | v2 | v3 |
|---|---|---|
| 虚拟轨迹来源 | UE JSON + 自动桥接 | UE JSON（全部由 UE 端完成） |
| 输入格式 | CLI 参数 | JSON 文件 |
| 多段渲染 | 1 段 | 任意多段 |
| 圆心计算 | 自动 | 不需要 |
| 依赖 | bridge_interpolate, merge_trajectory | 无额外依赖 |
| GT 相机 | 用于锚点 + 圆心 | 仅 SuperSplat 参考 |
