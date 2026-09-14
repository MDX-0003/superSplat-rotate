---
name: fuse-bias-grid-overflow
description: "fuse_ply.py bias 修正的密度网格 int32 溢出陷阱 — main PLY 离群点撑爆包围盒导致 np.bincount 报负值错误"
metadata: 
  node_type: memory
  type: project
---

# `fuse_ply.py` bias 密度网格的 int32 溢出陷阱

**症状**: 开 `--bias`（preset 如「明亮场景-多人-中bias」）时 fuse 崩溃：

```
File "tills_ply/fuse_ply.py", line 458, in apply_bias
    main_c, main_r, main_peak, main_core = density_core(main_2d)
File "tills_ply/fuse_ply.py", line 439, in density_core
    counts = np.bincount(flat, minlength=nx * ny)
ValueError: 'list' argument must have no negative elements
```

**根因**: `density_core()` 用投影点的**包围盒**算网格尺寸，再以 `int32` 算格索引：

```python
ix = ...astype(np.int32)
flat = ix * ny + iy          # int32 乘法溢出 → 变负
counts = np.bincount(flat, minlength=nx * ny)
```

`main_verts` 是**全量保留、从不过圆柱过滤**的（`--files` 的第一个文件），LiteGS 产出里带远场离群点。0914 实测 `0914-175813.ply` 最远点离圆心 **11.8 km**：

| 量 | 值 |
|---|---|
| 2D 投影范围 | x `[-11778.4, 80.5]`，y `[-444.4, 4584.6]` |
| 网格尺寸 | 79062 × 33529 = **2 650 869 798** 格 > int32 上限 2147483647 |
| `flat` 负值元素 | 294015 / 294016 |

即：**先 int32 溢出变负，再被 np.bincount 拒绝**。真实点云半径只有约 2.1 m。

**为什么以前没暴露**: 该路径只在 `args.bias` 为真时进入。0914 在 18:11 之前的所有 fuse 都是 `--no-bias`，18:11 起改用带 bias 的 preset 后**每一次都崩**。所以「换个 preset 就报错」通常意味着换 preset 第一次执行了某条休眠代码路径，**不是 preset 参数本身有问题**。

## 修复（2026-09-14）

1. 格索引用 **int64**（`astype(np.int64)`），消除溢出。
2. **核心估计前裁掉离群点**：`core_sel = ‖main_2d‖ <= effective_r + 0.15`，只让可能进入核心的点参与密度网格。不裁的话即使修好溢出，仍要分配 26.5 亿的 `counts` 数组。裁掉的点**照常写入输出**，只是不参与峰值统计；被排除数量会打印出来。
3. `density_core()` 因此返回**裁剪子集**的 mask，调用方需把 mask 映射回全量下标后再使用。

> `_filter_ground_adaptive()` 第 288 行有同样写法，但它的输入已按 `effective_r` + 高度**硬过滤**过，包围盒有界，暂无风险。若将来去掉那层硬过滤，同一 bug 会在那里复现。

## 验证要点

回归对比（2 650 869 798 格 vs 裁剪后 1024 格）核心质心偏差 **4.8 mm**、半径偏差 **0.7 mm**，相对人物间距 0.9 m 可忽略，不改变 bias 判定。

**How to apply**: 改动 `density_core` 的网格 / 索引逻辑时，务必确认输入点云的包围盒有界；新增「不过滤全量保留」的点云进类似网格算法时，先确认最远点到参考点的距离是否量级正常。
