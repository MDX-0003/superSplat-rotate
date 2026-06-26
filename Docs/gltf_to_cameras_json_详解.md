# 从 UE glTF 动画到相机关键帧：`gltf_to_cameras_json.py` 全流程解析

**阅读前提：** 无。本文假定你对 glTF、动画存储格式、3D 坐标系均无了解。
**目标：** 逐阶段理解这个脚本如何从一个 UE 导出的 glTF 动画文件中提取相机轨迹，输出可供 3D 重建工具使用的位置+朝向序列。

---

## 0. 这篇文章讲什么

你有一段 UE 中的相机动画——相机在场景中飞行了 14 秒。UE 把它导出为一个 `.gltf` 文件。现在你需要把这段动画变成 **858 个离散的相机位姿**（每帧一个位置 + 一个朝向矩阵），喂给 3D 高斯泼溅（3DGS）之类的重建管线。

这个脚本做的就是这件事。下面沿着 `main()` 的执行顺序，逐阶段拆解。

---

## 1. 阶段一：打开文件 —— glTF 的两种物理形态

> **代码入口：** `main()` 第 276-319 行

### 做了什么

脚本以二进制方式读取整个输入文件，然后检查**前 4 个字节**。

### 为什么这样设计

glTF 有两个物理版本，共享同一套逻辑结构，但文件组织方式不同：

| 形态 | 扩展名 | 结构 | 类比 |
|------|--------|------|------|
| **glTF (JSON + 外部文件)** | `.gltf` | 一个 JSON 文本 + 若干外部 `.bin` 文件 + 图片文件 | 就像一篇 HTML 引用了外部 CSS 和图片 |
| **GLB (二进制单文件)** | `.glb` | 所有内容打进一个二进制文件 | 就像把 HTML+CSS+图片全塞进一个 zip，但这个 zip 有固定格式 |

UE 导出的一般是 `.gltf` 格式（JSON + 单独的 `.bin`）。

### 怎么做

```
读取文件头 4 字节:
  ├── 是 b'glTF' → GLB 格式，按 chunk 结构解析
  │                 12 字节头部 + JSON chunk + BIN chunk
  └── 不是       → glTF 格式，直接当 JSON 解码
                   然后找到 buffers 数组，把每个 .bin 文件读进内存
```

不管是哪种形态，解析完成后脚本内部得到一个统一的 Python 字典 `data`，结构大致是：

```python
data = {
    "cameras": [...],      # 相机光学属性
    "nodes": [...],        # 场景层级树
    "animations": [...],   # 动画数据
    "accessors": [...],    # 数据访问器（见下一节）
    "bufferViews": [...],  # 缓冲区视图（见下一节）
    "buffers": [...],      # 原始二进制缓冲区
    "_buffers": {0: b'\x00\x01...', ...},  # 脚本内部：已加载到内存的二进制数据
    "_filepath": "..."     # 脚本内部：文件路径
}
```

十六进制 `0x4E4F534A` 是 `JSON` 四个字符的小端序编码，`0x004E4942` 是 `BIN\0`。它们是 GLB chunk 的类型标识——GLB 用这种魔数来标记每个数据块的用途。

---

## 2. 插曲：glTF 的三层数据模型 —— 为什么需要 Buffers、BufferViews、Accessors

在进入后续阶段之前，必须先理解 glTF 最核心的设计：**三层数据访问模型**。这是整个脚本能读到动画关键帧数值的前提。

### 它要解决什么问题

一个 `.bin` 文件里塞了几万甚至几十万个 float 值——位置坐标、旋转四元数、时间戳、法线、UV 坐标……全混在一起，是一大段无结构的字节流。

你需要在读取时回答三个不同层面的问题：

1. **数据在哪？** → 哪个文件、从文件的第几个字节开始
2. **数据怎么排？** → 每个元素占多少字节、间隔多少（stride）
3. **数据是什么？** → 这些字节是 float 还是 int？是标量、三维向量还是四维向量？

如果把这三种信息全塞进一个字段，会导致大量重复定义。glTF 把它们拆成三层，每层只负责一个维度：

```
Buffer      → "数据存在哪个文件里"
  └─→ 只记录文件 URI 和总字节数

BufferView  → "从第几个字节开始，每份数据间隔多少"
  └─→ 记录 byteOffset（起始位置）和 byteStride（步长）

Accessor    → "这些字节应解读为多少个什么类型的值"
  └─→ 记录 count（个数）、componentType（float/int）、type（SCALAR/VEC3/VEC4/MAT4）
```

### 三层之间如何衔接

用你的实际数据举例。假设动画中某个采样器存储了 858 个平移向量（VEC3），它们在二进制文件中的访问路径是：

```
animations[0].samplers[0].output = 3
        ↓
accessors[3] = {
    "bufferView": 1,        → 使用 bufferView #1
    "componentType": 5126,  → FLOAT (IEEE 754)
    "type": "VEC3",         → 每个数据是 3 个 float
    "count": 858            → 共 858 个 VEC3
}
        ↓
bufferViews[1] = {
    "buffer": 0,            → 使用 buffer #0
    "byteOffset": 16896,    → 从 .bin 文件第 16896 字节开始
    "byteStride": 12        → 每个 VEC3 占 12 字节 (3×4)
}
        ↓
buffers[0] = {
    "uri": "CamSqe.bin"     → 数据在这个文件里
}
```

最终的读取过程：

```
打开 CamSqe.bin → 跳转到字节 16896 → 每次读 12 字节
→ 按 float 格式解包为 3 个 32-bit 浮点数 → 重复 858 次
```

脚本中实际执行这个过程的是两个函数：

> **代码入口：** `get_animation_times()` 第 170-200 行（读时间值），`get_animation_values()` 第 203-244 行（读属性值）

核心读取代码（以读 VEC3 值为例）。注意代码中的 `sampler` 变量来自对 `anim['samplers']` 数组的遍历，不是 `data` 的直接子项：

```python
# sampler 来自: for sampler_idx, sampler in enumerate(anim['samplers'])
# sampler['output'] 是一个整数，作为 accessors 数组的索引
valuesAccessor = data['accessors'][sampler['output']]
valuesView = data['bufferViews'][valuesAccessor['bufferView']]
buffer_data = data['_buffers'][valuesView['buffer']]

# 计算起始字节
byte_offset = valuesView.get('byteOffset', 0) + valuesAccessor.get('byteOffset', 0)

# 逐元素读取
for i in range(count):
    offset = byte_offset + i * stride
    val = [struct.unpack_from('<f', buffer_data, offset + j*4)[0]
           for j in range(elems)]
```

### 为什么要三层而不是一层

如果没有这个分层，每次定义"这 858 个 VEC3"都得重复写 URI、offset、stride、type、count 五样信息。一个文件里有几十个这样的数据段，JSON 会迅速膨胀。分层后，同一个 bufferView 可以被多个 accessor 复用（比如位置和法线共享同一段数据），同一个 buffer 可以被多个 bufferView 引用。**每一层只定义一次，上层通过索引引用下层。**

---

## 3. 阶段二：找相机 —— Cameras 与 Nodes 的分离设计

> **代码入口：** `main()` 第 321-342 行

### 做了什么

遍历 `data["cameras"]` 找第一个透视相机，再遍历 `data["nodes"]` 找到引用这个相机的节点。

打印输出对应：

```
相机 0: CameraComponent - 类型: perspective
相机 1: CameraComponent - 类型: perspective
找到相机节点: 13
```

### 为什么 Cameras 和 Nodes 是分开的

在 glTF 中，**"相机是什么"** 和 **"相机在哪"** 是两个独立的概念，分属两个数组：

- `cameras` 数组只描述**光学属性**：FOV、近远裁面、投影类型。它是一个"镜头规格表"。
- `nodes` 数组描述**空间位置**：这个镜头挂在场景层级树的哪个位置。

一个节点通过 `"camera": 索引` 字段来指向 cameras 数组，意思是"我这个节点用的镜头规格是这一款"。多个节点可以共用同一个相机定义——就像多台摄影机可以装同一款镜头。

```json
// cameras: "镜头规格"
{"name": "CameraComponent", "type": "perspective",
 "perspective": {"yfov": 1.178, "znear": 0.01, "zfar": 1000.0}}

// nodes: "谁挂了这个镜头"
{"camera": 0, "name": "Camera", "children": [...]}
```

### 为什么有 2 个相机但只用 1 个

你的文件中有 2 个相机对象（都是 perspective），脚本用 `break` 条件（实际上没写 break——它遍历完所有 cameras，最后一个 `type == "perspective"` 的会覆盖前面的）。最终取到的是相机索引 1。然后在 nodes 中找到引用这个相机索引的节点——节点 13。

---

## 4. 阶段三：理解场景层级 —— Node 树与 TRS 变换

> **代码入口：** `build_parent_map()` 第 97-104 行，`get_node_world_transform()` 第 345-399 行

### 做了什么

构建父子关系映射表 `parent_map`，然后从相机节点（13）向上追溯到根，得到相机链 `[3, 11, 12, 13]`。

### 为什么需要层级

3D 场景中的物体不是各自独立放置的。一个典型的相机装备可以这样描述：

- "三脚架放在地板上某个位置"（根节点的位置）
- "云台装在三脚架顶部，可以旋转"（相对三脚架的旋转）
- "相机装在云台上"（相对云台的固定偏移）

这种"A 在 B 上面，B 在 C 里面"的关系，在 glTF 中用 **Nodes 的 children 数组** 表达为一棵树：

```json
"nodes": [
  {"name": "CameraTarget", "children": [11]},           // 节点 3 → 子节点 11
  {"children": [12], "translation": [...], ...},        // 节点 11 → 子节点 12
  {"children": [13]},                                   // 节点 12 → 子节点 13
  {"camera": 1}                                         // 节点 13 → 无子节点
]
```

脚本用 `build_parent_map()` 反转这个关系——给定一个子节点，快速查到它的父节点是谁。这样从节点 13 就能一路回溯：`13 → 12 → 11 → 3`。

### TRS 变换：节点如何表达自己的位置和朝向

每个节点用三个属性描述自己的**相对父节点的**空间变换：

| 属性 | 含义 | 默认值 | 存储形式 |
|------|------|--------|---------|
| **T**ranslation | 位移 | `[0,0,0]` | 3 个 float → 一个 3D 向量 |
| **R**otation | 旋转 | `[0,0,0,1]` | 4 个 float → 一个四元数 (x,y,z,w) |
| **S**cale | 缩放 | `[1,1,1]` | 3 个 float → 三个轴的缩放系数 |

> **为什么用四元数存旋转而不直接用角度？** 欧拉角（"绕 X 转 30°，绕 Y 转 45°"）会遇到万向节死锁——两个旋转轴重合时，丢失一个自由度。四元数避免了这个问题，且做矩阵乘法和插值都更高效。代价是四个数字不如角度直观。

脚本中从 TRS 构建 4×4 矩阵的过程（[第 367-380 行](tills/gltf_to_cameras_json.py#L367-L380)）：

```python
# 四元数 → 3×3 旋转矩阵
rot_matrix = quaternion_to_rotation_matrix(rotation)

# 旋转 × 缩放，平移填入第 4 列
local_matrix = [
    [R00*sx, R01*sy, R02*sz, tx],
    [R10*sx, R11*sy, R12*sz, ty],
    [R20*sx, R21*sy, R22*sz, tz],
    [0,      0,      0,       1]
]
```

最下面一行 `[0,0,0,1]` 是齐次坐标的数学约定——保证平移能通过矩阵乘法表达。

---

## 5. 阶段四：读取动画数据 —— Channels 与 Samplers

> **代码入口：** `main()` 第 405-428 行

### 做了什么

遍历 `data["animations"]` 中的每个动画，为每个采样器读取关键帧时间数组和值数组，然后遍历通道列表打印节点-属性-采样器的对应关系。

打印输出对应：

```
动画 0: Sq_SKNJ_H_2_0
  通道: 节点 11, 属性 translation, 采样器 0
  通道: 节点 11, 属性 rotation, 采样器 1
  通道: 节点 11, 属性 scale, 采样器 2
  通道: 节点 3, 属性 translation, 采样器 3
  通道: 节点 3, 属性 rotation, 采样器 4
```

### glTF 动画的两层结构

glTF 把动画定义为两个分离的数组，这又是一次"拆开定义，通过索引关联"的设计：

```
Animation
├── channels[]   ← "谁 + 什么属性 + 用哪个采样器"
│   └── {sampler: 索引, target: {node: 索引, path: "translation"}}
│
└── samplers[]   ← "关键帧时间在哪 + 关键帧值在哪"
    └── {input: accessor索引, output: accessor索引, interpolation: "LINEAR"}
```

**Channel（通道）** 回答"谁被驱动、哪个属性、数据在哪"：
- `target.node` → 被驱动的节点编号
- `target.path` → 被驱动的属性（`translation` / `rotation` / `scale`）
- `sampler` → 指向 `samplers` 数组的索引

**Sampler（采样器）** 回答"关键帧数据在哪里"：
- `input` → accessor 索引，这个 accessor 存的是**时间戳**
- `output` → accessor 索引，这个 accessor 存的是**属性值**
- `interpolation` → 插值方式（`LINEAR` 线性 / `STEP` 阶梯 / `CUBICSPLINE` 三次样条）

### 你的数据长什么样

```
节点 3 (CameraTarget):
  translation 动画 → sampler[3] → times=accessor[?], values=accessor[?]
  rotation 动画    → sampler[4] → times=accessor[?], values=accessor[?]

节点 11 (中间节点):
  translation 动画 → sampler[0] → times=accessor[?], values=accessor[?]
  rotation 动画    → sampler[1] → times=accessor[?], values=accessor[?]
  scale 动画       → sampler[2] → times=accessor[?], values=accessor[?]
```

每个采样器的关键帧数据通过 [阶段二](#2-插曲gltf-的三层数据模型--为什么需要-buffersbufferviewsaccessors) 介绍的三层模型从 `.bin` 文件中读出。脚本调用 `get_animation_times()` 和 `get_animation_values()`，将 accessor 索引解析为内存中的 Python 列表，存入 `animations_data` 字典。

> **代码入口：** `get_animation_times()` 第 170-200 行，`get_animation_values()` 第 203-244 行

### 为什么动画不用一个统一的"第 N 帧所有节点状态"表来存

想象一下：如果把每帧所有节点的完整 TRS 全存下来，14 秒 × 60fps = 858 帧 × 5 个节点 × 10 个 float ≈ 42,900 个数字。而 glTF 的关键帧方式：假设每个属性只在少数时间点设关键帧（比如每 5 帧一个），只存 ~170 个关键帧 × 10 个 float ≈ 1,700 个数字，**压缩比达到 25:1**。而且关键帧之间用线性插值自动填充，播放时看不出区别。

---

## 6. 阶段五：确定时间范围与 FOV

> **代码入口：** 时间范围第 430-443 行，FOV 第 449-466 行

### 时间范围

遍历第一个动画的所有采样器，取所有时间数组的**最早时间**和**最晚时间**：

```
动画时间范围: 0.0 - 14.283333778381348
```

这个范围将决定最终输出多少帧：`int(14.28 × 60) + 1 = 858 帧`。

### 相机内参：FOV → 焦距

> **代码入口：** `fov_to_focal_length()` 第 142-145 行

从 glTF 的 perspective 相机中读取 `yfov`（垂直视场角，单位弧度），转为度数后得到 `67.5°`。然后用它计算像素焦距：

```
fy = height / (2 × tan(fov_y / 2))
fx = fy  ← 假定像素是正方形
```

**为什么 fx = fy？** glTF 只存 `yfov`（垂直视场角），没有水平视场角。对于像素为正方形的标准相机，水平焦距和垂直焦距相等。如果像素不是正方形（anamorphic 镜头等），需要单独指定。

> `yfov` 是垂直视场角而非水平视场角，这是图形学界的惯例——因为屏幕的垂直方向通常是约束维度（高度先满，宽度自适应）。

---

## 7. 阶段六：逐帧生成相机位姿（核心循环）

> **代码入口：** `main()` 第 488-614 行

这是整个脚本的核心：858 次循环，每次计算一个时间点的相机世界位置和朝向。每帧经历四步计算：

### 7.1 线性插值：在关键帧之间"补帧"

> **代码入口：** `interpolate_animation()` 第 247-262 行

glTF 只存储了稀疏的关键帧（比如第 0 秒、第 1 秒、第 3 秒……的位置），但我们需要的第 0.5 秒并没有对应的关键帧。需要根据前后两个关键帧**算出**第 0.5 秒的值。

假设关键帧 A 在时间 `t_A` 值为 `v_A`，关键帧 B 在时间 `t_B` 值为 `v_B`。对于目标时间 `t`（`t_A ≤ t ≤ t_B`）：

```
插值因子 α = (t - t_A) / (t_B - t_A)    ← 在 0~1 之间，表示 t 离 A 有多远
结果 = v_A × (1 - α) + v_B × α           ← 加权平均
```

如果 `α = 0.3`，结果就取 A 值的 70% + B 值的 30%。对于向量（translation VEC3、scale VEC3），每个分量独立插值；对于四元数（rotation），同样逐分量线性插值。

> **注意：** 严格的四元数插值应当使用 `slerp`（球面线性插值）以保持旋转的角速度均匀。这里使用了简化的逐分量线性插值，适用于关键帧较密的动画——误差肉眼不可见。

### 7.2 矩阵级联：从局部到世界的变换链

> **代码入口：** `get_animated_world_transform()` 第 496-541 行

每个节点的 TRS 定义的是**相对父节点的**变换。要得到相机在世界空间中的真实位置，需要沿层级链做矩阵乘法：

```
World₃ = local₃                                    ← 节点 3 是根，没有父节点
World₁₁ = World₃ × local₁₁                         ← 节点 11 的父节点是 3
World₁₂ = World₁₁ × local₁₂                        ← 节点 12 的父节点是 11
World₁₃ = World₁₂ × local₁₃  ← 这就是相机世界矩阵   ← 节点 13 的父节点是 12
```

代码中的递归过程：

```python
def get_animated_world_transform(node_index, ...):
    # 1. 从动画数据插值得到本帧的 translation/rotation/scale
    # 2. 构建 4×4 局部矩阵
    # 3. 如果有父节点，递归获取父节点的世界矩阵
    # 4. world = parent_world × local
    return world_matrix
```

**为什么矩阵乘法是这个顺序？** 矩阵乘法不满足交换律。`parent × local` 的含义是："先把点按局部坐标系变换，再把结果按父坐标系变换"——先局部后全局，符合层级的直觉。

### 7.3 坐标系转换：UE 左手系 → 目标右手系

> **代码入口：** 第 549-551 行（相机位置）、第 559-561 行（目标位置）

这是一个容易被忽略但至关重要的步骤。

UE 使用 **左手坐标系**：X 指向前方、Y 指向右方、Z 指向上方。而 3D 重建工具（COLMAP、SuperSplat 等）使用的是 **右手坐标系**，且轴向约定不同。

脚本用一行简单的分量重排完成转换：

```python
# (x, y, z) → (z, -y, x)
camera_position = [camera_position[2], -camera_position[1], camera_position[0]]
```

**这个变换做了什么：**

| UE 坐标系 | 含义 | → | 目标坐标系 | 新含义 |
|-----------|------|---|-----------|--------|
| X (前) | 相机前方 | → | 新 Z | 深度方向 |
| Y (右) | 相机右侧 | → | 新 -Y | 负-Y 方向 |
| Z (上) | 世界上方 | → | 新 X | 水平方向 |

具体步骤拆解：
1. **Z → X**：UE 的上变成了目标系的水平轴
2. **-Y → Y**：UE 的右变成了目标系的反向垂直轴（所以取反）
3. **X → Z**：UE 的前变成了目标系的深度轴

**为什么不能直接用 `transform_matrix_ue_to_glsl()`？** 脚本第 38 行确实定义了这个函数——绕 X 轴 -90° 再绕 Z 轴 180° 的复合旋转矩阵。但它在代码中**从未被调用**。实际使用的是直接分量重排 `(z, -y, x)`，这等价于那两个旋转的复合效果，但更简洁且不需要矩阵乘法。

### 7.4 LookAt：让相机始终看向目标

> **代码入口：** 第 563-600 行

拿到相机位置和 CameraTarget 位置后，需要计算相机的**朝向**——一个 3×3 旋转矩阵。用的是经典的 LookAt 算法：

```
                    CameraTarget
                       ●
                      /
                     /  ← look_dir (视线方向)
                    /
                   ●  Camera
```

**第一步：计算视线方向并归一化**

```python
look_dir = normalize(target_position - camera_position)
```

**第二步：计算相机的"右"轴**

```python
up = [0, 1, 0]             # 世界"上方"
right = normalize(up × look_dir)  # 叉积得到垂直方向
```

叉积（cross product）的几何含义：两个向量张成的平面的法向量。`up × look_dir` 得到的向量同时垂直于"上方"和"视线"——正好是相机的右轴。

**第三步：重新计算相机的"上"轴**

```python
new_up = look_dir × right
```

为什么不用原始的 `[0,1,0]`？因为 look_dir 通常不水平（相机可能俯视或仰视），直接用 `[0,1,0]` 作为相机的上轴会倾斜。用 `look_dir × right` 得到的是**严格垂直于视线和右轴**的上轴——保证三轴两两正交。

**第四步：组装旋转矩阵**

```python
rot_matrix = [
    [right.x,  new_up.x,  look_dir.x],
    [right.y,  new_up.y,  look_dir.y],
    [right.z,  new_up.z,  look_dir.z]
]
```

每一**列**是相机的一个局部坐标轴在世界空间中的分量：
- 第 1 列 = 相机右轴 (X)
- 第 2 列 = 相机上轴 (Y)
- 第 3 列 = 相机前向 (Z)

这是一个 **Camera-to-World 旋转矩阵**。把相机局部坐标 `[1,0,0]`（即相机右侧方向）乘上这个矩阵，就能得到它在世界空间中的方向。

---

## 8. 阶段七：输出 cameras.json

> **代码入口：** 第 605-620 行

每帧的计算结果存入一个列表，最后序列化为 JSON：

```json
[
  {
    "id": 0,
    "img_name": "camera_0000",
    "width": 1920,
    "height": 1080,
    "position": [1.234, -0.567, 3.890],
    "rotation": [
      [0.998,  0.012, -0.056],
      [-0.011, 0.999,  0.032],
      [0.057, -0.031,  0.998]
    ],
    "fx": 907.5,
    "fy": 907.5
  },
  ...
]
```

### 字段含义

| 字段 | 含义 | 值的来源 |
|------|------|---------|
| `id` | 帧序号，从 0 开始 | 循环变量 |
| `img_name` | 帧的图像名 | 格式化为 `camera_0000` 到 `camera_0857` |
| `width` / `height` | 图像分辨率 | 命令行参数，默认 1920×1080 |
| `position` | 相机世界位置 `[x, y, z]` | 矩阵级联 + 坐标变换 |
| `rotation` | 3×3 旋转矩阵（Camera-to-World） | LookAt 算法 |
| `fx` / `fy` | 像素焦距（水平/垂直） | FOV 计算，全程不变 |

---

## 9. 全流程速查

```
读取文件
  ├── .gltf → JSON 解码 + 加载外部 .bin
  └── .glb  → 解析 chunk → JSON + BIN
        │
查找相机 ──→ cameras[] 找 perspective → 得到 camera_index
查找相机节点 ──→ nodes[] 找 node.camera == camera_index → 节点 13
        │
构建层级 ──→ build_parent_map() → 13→12→11→3
        │
读取动画 ──→ 遍历 animations[0]:
  │           ├── channels: 谁 + 什么属性 + 哪个采样器
  │           └── samplers: input(output) accessor → 三层模型读 bin → times & values
        │
确定参数 ──→ 时间范围 0~14.28s、FOV 67.5° → fx=fy
        │
逐帧循环 (858 帧, 60fps) ──→ for each frame:
  │   1. 线性插值 → 得到每个动画节点在本帧的 TRS
  │   2. 矩阵级联 → World = parent × local (沿链 3→11→12→13)
  │   3. 坐标变换 → (x,y,z) → (z,-y,x)
  │   4. LookAt  → 朝向 = lookAt(camera_pos, target_pos)
  │   5. 存入 poses 数组
        │
写入 cameras.json
```
