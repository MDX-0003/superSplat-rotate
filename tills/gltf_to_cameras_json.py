#!/usr/bin/env python3
"""
glTF相机动画转SuperSplat cameras.json工具
直接读取gltf文件，不依赖Blender
python tills/gltf_to_cameras_json.py SequenceData/01/CamSqe.gltf -o SequenceData/01/cameras.json

"""

import sys
import json
import math
import argparse
import struct
import os


def quaternion_to_rotation_matrix(q):
    """将四元数转换为旋转矩阵"""
    x, y, z, w = q
    
    return [
        [1 - 2*y*y - 2*z*z, 2*x*y - 2*z*w, 2*x*z + 2*y*w],
        [2*x*y + 2*z*w, 1 - 2*x*x - 2*z*z, 2*y*z - 2*x*w],
        [2*x*z - 2*y*w, 2*y*z + 2*x*w, 1 - 2*x*x - 2*y*y]
    ]


def matrix_multiply(a, b):
    """4x4矩阵乘法"""
    result = [[0]*4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            for k in range(4):
                result[i][j] += a[i][k] * b[k][j]
    return result


def transform_matrix_ue_to_glsl(m):
    """UE坐标系(Z-up)转glTF坐标系(Y-up)
    UE: X向前, Y向右, Z向上 (左手系)
    glTF: X向右, Y向上, Z指向屏幕外 (右手系)
    """
    # 绕X轴旋转-90度: Z轴向上 -> Y轴向上
    # 同时需要处理左右手系差异
    rot_x_minus_90 = [
        [1, 0, 0, 0],
        [0, 0, 1, 0],
        [0, -1, 0, 0],
        [0, 0, 0, 1]
    ]
    
    # 绕Z轴180度处理左右手系差异
    rot_z_180 = [
        [-1, 0, 0, 0],
        [0, -1, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1]
    ]
    
    return matrix_multiply(rot_z_180, rot_x_minus_90)


def get_node_world_transform(node_index, nodes, matrices_cache=None, parent_matrix=None):
    """获取节点的世界变换"""
    if matrices_cache is None:
        matrices_cache = {}
    
    if node_index in matrices_cache:
        return matrices_cache[node_index]
    
    node = nodes[node_index]
    
    # 从TRS构建本地矩阵
    translation = node.get('translation', [0, 0, 0])
    rotation = node.get('rotation', [0, 0, 0, 1])
    scale = node.get('scale', [1, 1, 1])
    
    rot_matrix = quaternion_to_rotation_matrix(rotation)
    
    local_matrix = [
        [rot_matrix[0][0]*scale[0], rot_matrix[0][1]*scale[1], rot_matrix[0][2]*scale[2], translation[0]],
        [rot_matrix[1][0]*scale[0], rot_matrix[1][1]*scale[1], rot_matrix[1][2]*scale[2], translation[1]],
        [rot_matrix[2][0]*scale[0], rot_matrix[2][1]*scale[1], rot_matrix[2][2]*scale[2], translation[2]],
        [0, 0, 0, 1]
    ]
    
    # 如果有父节点矩阵，组合它们
    if parent_matrix is not None:
        world_matrix = matrix_multiply(parent_matrix, local_matrix)
    else:
        world_matrix = local_matrix
    
    matrices_cache[node_index] = world_matrix
    return world_matrix


def build_parent_map(nodes):
    """构建节点索引到父节点的映射"""
    parent_map = {}
    for idx, node in enumerate(nodes):
        if 'children' in node:
            for child_idx in node['children']:
                parent_map[child_idx] = idx
    return parent_map


def get_world_transform_with_parents(node_index, nodes, parent_map, matrices_cache=None):
    """获取节点的世界变换（正确处理父子关系）"""
    if matrices_cache is None:
        matrices_cache = {}
    
    if node_index in matrices_cache:
        return matrices_cache[node_index]
    
    node = nodes[node_index]
    
    # 获取本地矩阵
    translation = node.get('translation', [0, 0, 0])
    rotation = node.get('rotation', [0, 0, 0, 1])
    scale = node.get('scale', [1, 1, 1])
    
    rot_matrix = quaternion_to_rotation_matrix(rotation)
    
    local_matrix = [
        [rot_matrix[0][0]*scale[0], rot_matrix[0][1]*scale[1], rot_matrix[0][2]*scale[2], translation[0]],
        [rot_matrix[1][0]*scale[0], rot_matrix[1][1]*scale[1], rot_matrix[1][2]*scale[2], translation[1]],
        [rot_matrix[2][0]*scale[0], rot_matrix[2][1]*scale[1], rot_matrix[2][2]*scale[2], translation[2]],
        [0, 0, 0, 1]
    ]
    
    # 获取父节点的世界矩阵
    if node_index in parent_map:
        parent_world = get_world_transform_with_parents(parent_map[node_index], nodes, parent_map, matrices_cache)
        world_matrix = matrix_multiply(parent_world, local_matrix)
    else:
        world_matrix = local_matrix
    
    matrices_cache[node_index] = world_matrix
    return world_matrix


def fov_to_focal_length(fov_deg, width):
    """将FOV转换为焦距"""
    fov_rad = math.radians(fov_deg)
    return width / (2 * math.tan(fov_rad / 2))


def get_buffer_data(gltf_data, uri):
    """获取buffer的二进制数据"""
    if uri.startswith('data:'):
        # 内嵌的base64数据
        import base64
        data = uri.split(',')[1]
        return base64.b64decode(data)
    else:
        # 外部文件
        base_dir = os.path.dirname(os.path.abspath(gltf_data.get('_filepath', '')))
        with open(os.path.join(base_dir, uri), 'rb') as f:
            return f.read()


def read_gltf(filepath):
    """读取glTF文件"""
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    data['_filepath'] = filepath
    return data


def get_animation_times(gltf_data, sampler_index, sampler):
    """获取动画采样器的时间值"""
    import base64
    
    timesAccessor = gltf_data['accessors'][sampler['input']]
    timesView = gltf_data['bufferViews'][timesAccessor['bufferView']]
    buffer_data = gltf_data['_buffers'][timesView['buffer']]
    
    byte_offset = timesView.get('byteOffset', 0) + timesAccessor.get('byteOffset', 0)
    count = timesAccessor['count']
    component_type = timesAccessor['componentType']
    
    if component_type == 5126:  # FLOAT
        dtype = 'f'
    elif component_type == 5123:  # UNSIGNED_SHORT
        dtype = 'H'
    else:
        dtype = 'f'
    
    stride = timesView.get('byteStride', 4)
    times = []
    
    for i in range(count):
        offset = byte_offset + i * stride
        if component_type == 5126:
            val = struct.unpack_from('<f', buffer_data, offset)[0]
        else:
            val = struct.unpack_from('<H', buffer_data, offset)[0] / 65535.0 * (timesAccessor.get('max', [1])[0] - timesAccessor.get('min', [0])[0]) + timesAccessor.get('min', [0])[0]
        times.append(val)
    
    return times


def get_animation_values(gltf_data, sampler_index, sampler):
    """获取动画采样器的值"""
    import base64
    
    valuesAccessor = gltf_data['accessors'][sampler['output']]
    valuesView = gltf_data['bufferViews'][valuesAccessor['bufferView']]
    buffer_data = gltf_data['_buffers'][valuesView['buffer']]
    
    byte_offset = valuesView.get('byteOffset', 0) + valuesAccessor.get('byteOffset', 0)
    count = valuesAccessor['count']
    component_type = valuesAccessor['componentType']
    type_name = valuesAccessor['type']  # SCALAR, VEC3, VEC4, MAT4
    
    if component_type == 5126:  # FLOAT
        dtype = '<f'
    else:
        dtype = '<f'
    
    # 计算每个值的元素数量
    if type_name == 'SCALAR':
        elems = 1
    elif type_name == 'VEC3':
        elems = 3
    elif type_name == 'VEC4':
        elems = 4
    elif type_name == 'MAT4':
        elems = 16
    else:
        elems = 1
    
    stride = valuesView.get('byteStride', 4 * elems)
    values = []
    
    for i in range(count):
        offset = byte_offset + i * stride
        val = []
        for j in range(elems):
            v = struct.unpack_from('<f', buffer_data, offset + j * 4)[0]
            val.append(v)
        values.append(val)
    
    return values


def interpolate_animation(time, times, values):
    """线性插值动画值"""
    if time <= times[0]:
        return values[0]
    if time >= times[-1]:
        return values[-1]
    
    for i in range(len(times) - 1):
        if times[i] <= time <= times[i+1]:
            t = (time - times[i]) / (times[i+1] - times[i])
            if isinstance(values[i], list):
                return [values[i][j] * (1-t) + values[i+1][j] * t for j in range(len(values[i]))]
            else:
                return values[i] * (1-t) + values[i+1] * t
    
    return values[0]


def main():
    parser = argparse.ArgumentParser(description='glTF转cameras.json')
    parser.add_argument('input', help='输入glTF/glb文件')
    parser.add_argument('-o', '--output', required=True, help='输出JSON文件')
    parser.add_argument('--width', type=int, default=1920, help='图像宽度')
    parser.add_argument('--height', type=int, default=1080, help='图像高度')
    parser.add_argument('--fov', type=float, default=None, help='指定视野角(度)，覆盖glTF中的相机FOV')
    
    args = parser.parse_args()
    
    # 读取文件
    filepath = os.path.abspath(args.input)
    with open(filepath, 'rb') as f:
        raw_data = f.read()
    
    # 检测文件格式
    if raw_data[:4] == b'glTF':  # glb格式
        # glb格式: 12字节头部 + chunks
        # 头部: magic(4) + version(4) + length(4)
        # chunk: length(4) + type(4) + data
        
        # 解析第一个chunk (JSON)
        json_chunk_length = int.from_bytes(raw_data[12:16], 'little')
        json_chunk_type = int.from_bytes(raw_data[16:20], 'little')
        
        if json_chunk_type == 0x4E4F534A:  # JSON chunk
            json_data = raw_data[20:20+json_chunk_length].decode('utf-8')
            data = json.loads(json_data)
        
        # 检查是否有BIN chunk
        bin_offset = 20 + json_chunk_length
        bin_length = 0
        if bin_offset < len(raw_data) - 8:
            bin_length = int.from_bytes(raw_data[bin_offset:bin_offset+4], 'little')
            bin_chunk_type = int.from_bytes(raw_data[bin_offset+4:bin_offset+8], 'little')
            if bin_chunk_type == 0x004E4942:  # BIN chunk
                data['_bin_data'] = raw_data[bin_offset+8:bin_offset+8+bin_length]
            else:
                data['_bin_data'] = b''
        
        data['_buffers'] = {}
        if data.get('buffers') and '_bin_data' in data:
            data['_buffers'][0] = data['_bin_data']
        
        data['_filepath'] = filepath
    else:  # gltf格式 (JSON文本)
        data = json.loads(raw_data.decode('utf-8'))
        data['_filepath'] = filepath
        
        # 加载外部buffer数据
        data['_buffers'] = {}
        if 'buffers' in data:
            for i, buffer_info in enumerate(data['buffers']):
                if 'uri' in buffer_info:
                    data['_buffers'][i] = get_buffer_data(data, buffer_info['uri'])
    
    # 查找相机
    camera_index = None
    if 'cameras' in data:
        for idx, cam in enumerate(data['cameras']):
            print(f"相机 {idx}: {cam.get('name', 'unnamed')} - 类型: {cam.get('type', 'unknown')}")
            if cam.get('type') == 'perspective':
                camera_index = idx
    
    if camera_index is None:
        print("警告: 未找到透视相机，使用默认FOV")
    
    # 查找相机节点
    camera_node_index = None
    if 'nodes' in data:
        for idx, node in enumerate(data['nodes']):
            if node.get('camera') == camera_index:
                camera_node_index = idx
                print(f"找到相机节点: {idx}")
    
    if camera_node_index is None:
        print("错误: 未找到相机节点")
        sys.exit(1)
    
    # 查找相机节点的变换
    def get_node_world_transform(node_index, nodes, matrices_cache=None):
        """获取节点的世界变换"""
        if matrices_cache is None:
            matrices_cache = {}
        
        if node_index in matrices_cache:
            return matrices_cache[node_index]
        
        node = nodes[node_index]
        
        # 获取本地变换
        local_matrix = None
        
        if 'matrix' in node:
            m = node['matrix']
            local_matrix = [
                [m[0], m[1], m[2], m[3]],
                [m[4], m[5], m[6], m[7]],
                [m[8], m[9], m[10], m[11]],
                [m[12], m[13], m[14], m[15]]
            ]
        else:
            # 从TRS构建矩阵
            translation = node.get('translation', [0, 0, 0])
            rotation = node.get('rotation', [0, 0, 0, 1])
            scale = node.get('scale', [1, 1, 1])
            
            # 构建变换矩阵
            rot_matrix = quaternion_to_rotation_matrix(rotation)
            
            local_matrix = [
                [rot_matrix[0][0]*scale[0], rot_matrix[0][1]*scale[1], rot_matrix[0][2]*scale[2], translation[0]],
                [rot_matrix[1][0]*scale[0], rot_matrix[1][1]*scale[1], rot_matrix[1][2]*scale[2], translation[1]],
                [rot_matrix[2][0]*scale[0], rot_matrix[2][1]*scale[1], rot_matrix[2][2]*scale[2], translation[2]],
                [0, 0, 0, 1]
            ]
        
        # 获取父节点变换
        parent_matrix = [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
        if 'children' in node:
            # 查找父节点
            for idx, n in enumerate(nodes):
                if 'children' in n and node_index in n['children']:
                    parent_matrix = get_node_world_transform(idx, nodes, matrices_cache)
                    break
        
        # 组合世界矩阵
        world_matrix = [[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,1]]
        for i in range(4):
            for j in range(4):
                for k in range(4):
                    world_matrix[i][j] += parent_matrix[i][k] * local_matrix[k][j]
        
        matrices_cache[node_index] = world_matrix
        return world_matrix
    
    # 收集所有动画数据
    animations_data = {}
    animated_nodes = set()  # 所有有动画的节点
    
    if 'animations' in data:
        for anim_idx, anim in enumerate(data['animations']):
            print(f"动画 {anim_idx}: {anim.get('name', 'unnamed')}")
            animations_data[anim_idx] = {}
            
            for sampler_idx, sampler in enumerate(anim['samplers']):
                times = get_animation_times(data, sampler_idx, sampler)
                values = get_animation_values(data, sampler_idx, sampler)
                animations_data[anim_idx][sampler_idx] = {
                    'input': sampler['input'],
                    'output': sampler['output'],
                    'times': times,
                    'values': values
                }
            
            # 收集所有有动画的节点
            for channel in anim.get('channels', []):
                target = channel.get('target', {})
                node_id = target.get('node')
                path = target.get('path', '')
                sampler_idx = channel.get('sampler', 0)
                
                print(f"  通道: 节点 {node_id}, 属性 {path}, 采样器 {sampler_idx}")
                animated_nodes.add(node_id)
    
    # 确定时间范围
    time_start = 0.0
    time_end = 1.0
    
    if 'animations' in data and len(data['animations']) > 0:
        anim = data['animations'][0]
        for channel in anim.get('channels', []):
            sampler_idx = channel.get('sampler', 0)
            if sampler_idx in animations_data[0]:
                times = animations_data[0][sampler_idx]['times']
                time_start = min(time_start, min(times))
                time_end = max(time_end, max(times))
    
    print(f"动画时间范围: {time_start} - {time_end}")
    
    # 生成相机位姿
    nodes = data.get('nodes', [])
    cameras = data.get('cameras', [])
    
    # 获取相机FOV
    # 如果用户指定了--fov参数，使用用户指定的值；否则使用glTF中的FOV
    if args.fov is not None:
        fov = args.fov
        print(f"使用用户指定的FOV: {fov}度")
    else:
        fov = 60.0
        if camera_index is not None and camera_index < len(cameras):
            cam = cameras[camera_index]
            if 'perspective' in cam:
                fov = math.degrees(cam['perspective'].get('yfov', math.radians(60)))
        print(f"使用glTF中的FOV: {fov}度")
    
    # 根据FOV和图像尺寸计算fx, fy
    # gltf只存储fovy，因此只有fy能直接使用来自文件的fov来计算
    # fx默认等于fy即可
    fy = fov_to_focal_length(fov, args.height)
    fx = fy
    
    # 构建父节点映射
    parent_map = build_parent_map(nodes)
    
    # 查找CameraTarget节点（作为注视目标）
    camera_target_node = None
    for idx, node in enumerate(nodes):
        if node.get('name') == 'CameraTarget':
            camera_target_node = idx
            print(f"找到CameraTarget节点: {idx}")
            break
    
    # 查找相机节点链（从相机节点向上找到根）
    camera_chain = []
    current = camera_node_index
    while current is not None:
        camera_chain.insert(0, current)
        current = parent_map.get(current)
    
    print(f"相机链: {camera_chain}")
    
    # 采样动画（60fps）
    num_frames = int(time_end * 60) + 1  # 60fps
    poses = []
    
    for i in range(num_frames):
        time = time_start + (time_end - time_start) * i / (num_frames - 1) if num_frames > 1 else time_start
        
        # 为每个节点计算世界变换（考虑动画）
        def get_animated_world_transform(node_index, nodes, parent_map, animations_data, time, anim_idx=0):
            """获取节点在指定时间的动画世界变换"""
            node = nodes[node_index]
            
            # 获取节点的本地变换
            translation = list(node.get('translation', [0, 0, 0]))
            rotation = list(node.get('rotation', [0, 0, 0, 1]))
            scale = list(node.get('scale', [1, 1, 1]))
            
            # 如果有动画，应用动画数据
            if 'animations' in data:
                anim = data['animations'][anim_idx]
                for channel in anim.get('channels', []):
                    target = channel.get('target', {})
                    if target.get('node') == node_index:
                        path = target.get('path', '')
                        sampler_idx = channel.get('sampler', 0)
                        
                        if sampler_idx in animations_data[anim_idx]:
                            anim_data = animations_data[anim_idx][sampler_idx]
                            value = interpolate_animation(time, anim_data['times'], anim_data['values'])
                            
                            if path == 'translation':
                                translation = value
                            elif path == 'rotation':
                                rotation = value
                            elif path == 'scale':
                                scale = value
            
            # 构建本地矩阵
            rot_matrix = quaternion_to_rotation_matrix(rotation)
            local_matrix = [
                [rot_matrix[0][0]*scale[0], rot_matrix[0][1]*scale[1], rot_matrix[0][2]*scale[2], translation[0]],
                [rot_matrix[1][0]*scale[0], rot_matrix[1][1]*scale[1], rot_matrix[1][2]*scale[2], translation[1]],
                [rot_matrix[2][0]*scale[0], rot_matrix[2][1]*scale[1], rot_matrix[2][2]*scale[2], translation[2]],
                [0, 0, 0, 1]
            ]
            
            # 如果有父节点，递归获取父节点的世界变换
            if node_index in parent_map:
                parent_world = get_animated_world_transform(parent_map[node_index], nodes, parent_map, animations_data, time, anim_idx)
                world_matrix = matrix_multiply(parent_world, local_matrix)
            else:
                world_matrix = local_matrix
            
            return world_matrix
        
        # 计算相机节点的世界变换
        camera_world = get_animated_world_transform(camera_node_index, nodes, parent_map, animations_data, time)
        
        # 提取相机位置
        camera_position = [camera_world[0][3], camera_world[1][3], camera_world[2][3]]
        
        # 应用坐标变换：y轴取反，x和z互换
        # (x, y, z) -> (z, -y, x)
        camera_position = [camera_position[2], -camera_position[1], camera_position[0]]
        
        # 计算CameraTarget的世界位置（用于朝向计算）
        target_position = [0, 0, 0]
        if camera_target_node is not None:
            target_world = get_animated_world_transform(camera_target_node, nodes, parent_map, animations_data, time)
            target_position = [target_world[0][3], target_world[1][3], target_world[2][3]]
        
        # 应用坐标变换：y轴取反，x和z互换
        # (x, y, z) -> (z, -y, x)
        target_position = [target_position[2], -target_position[1], target_position[0]]
        
        # 计算朝向：相机始终看向CameraTarget
        look_dir = [
            target_position[0] - camera_position[0],
            target_position[1] - camera_position[1],
            target_position[2] - camera_position[2]
        ]
        
        # 归一化视线方向
        look_len = math.sqrt(look_dir[0]**2 + look_dir[1]**2 + look_dir[2]**2)
        if look_len > 0:
            look_dir = [look_dir[0]/look_len, look_dir[1]/look_len, look_dir[2]/look_len]
        
        # 构建旋转矩阵（看向目标）
        up = [0, 1, 0]
        
        # 计算right向量 = up × look_dir
        right = [
            up[1]*look_dir[2] - up[2]*look_dir[1],
            up[2]*look_dir[0] - up[0]*look_dir[2],
            up[0]*look_dir[1] - up[1]*look_dir[0]
        ]
        right_len = math.sqrt(right[0]**2 + right[1]**2 + right[2]**2)
        if right_len > 0:
            right = [right[0]/right_len, right[1]/right_len, right[2]/right_len]
        
        # 重新计算up向量 = look_dir × right
        new_up = [
            look_dir[1]*right[2] - look_dir[2]*right[1],
            look_dir[2]*right[0] - look_dir[0]*right[2],
            look_dir[0]*right[1] - look_dir[1]*right[0]
        ]
        
        # 构建旋转矩阵（行向量形式，符合COLMAP格式）
        rot_matrix_out = [
            [right[0], new_up[0], look_dir[0]],
            [right[1], new_up[1], look_dir[1]],
            [right[2], new_up[2], look_dir[2]]
        ]
        
        # 保存原始旋转矩阵用于生成多个版本
        rot_matrix_original = [row[:] for row in rot_matrix_out]
        
        poses.append({
            'id': i,
            'img_name': f"camera_{i:04d}",
            'width': args.width,
            'height': args.height,
            'position': camera_position,
            'rotation': rot_matrix_out,
            'fy': fy,
            'fx': fx
        })
    
    with open(args.output, 'w') as f:
        json.dump(poses, f, indent=2)
    
    print(f"\n生成 {len(poses)} 个相机位姿")
    print(f"位置范围: x=[{min(p['position'][0] for p in poses):.4f}, {max(p['position'][0] for p in poses):.4f}]")


if __name__ == '__main__':
    main()
