#!/usr/bin/env python3
"""Shared PLY I/O and circle fitting — used by fuse_ply.py and clip_ply.py."""

import numpy as np


# ---------------------------------------------------------------------------
# PLY binary read / write  (binary_little_endian, float32 properties)
# ---------------------------------------------------------------------------
def read_ply(filepath: str):
    """Read a binary little-endian PLY file.
    Returns (header_lines, properties, vertices) where vertices is (N, P) float32."""
    with open(filepath, "rb") as f:
        header_lines = []
        while True:
            line = f.readline().decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
            header_lines.append(line)
            if line == "end_header":
                break

    vertex_count = 0
    properties = []
    for line in header_lines:
        if line.startswith("element vertex "):
            vertex_count = int(line.split()[-1])
        elif line.startswith("property "):
            properties.append(line)

    num_props = len(properties)
    header_text = "\n".join(header_lines) + "\n"
    header_len = len(header_text.encode("utf-8"))

    with open(filepath, "rb") as f:
        f.seek(header_len)
        raw = f.read()

    expected = vertex_count * num_props * 4
    if len(raw) < expected:
        print(f"WARNING: {filepath}: expected {expected} bytes, got {len(raw)}")

    vertices = np.frombuffer(raw[:expected], dtype=np.float32).reshape(vertex_count, num_props)
    return header_lines, properties, vertices


def write_ply(filepath: str, header_lines, properties, vertices):
    """Write a binary little-endian PLY file.
    vertices can be a single (N, P) array or a list of arrays to vstack."""
    if isinstance(vertices, list):
        if vertices:
            all_verts = np.vstack(vertices)
        else:
            all_verts = np.empty((0, len(properties)), dtype=np.float32)
    else:
        all_verts = vertices
    total = all_verts.shape[0]

    with open(filepath, "wb") as f:
        for line in header_lines:
            if line.startswith("element vertex "):
                f.write(f"element vertex {total}\n".encode("utf-8"))
            else:
                f.write(f"{line}\n".encode("utf-8"))
        f.write(all_verts.tobytes())


# ---------------------------------------------------------------------------
# circle fitting (SVD plane + least-squares)
# ---------------------------------------------------------------------------
def fit_circle(points: np.ndarray):
    """Fit a 3D circle via SVD plane + least-squares.
    Returns (center, normal, r_fit, u1, u2)."""
    centroid = np.mean(points, axis=0)
    shifted = points - centroid
    _, _, vh = np.linalg.svd(shifted)
    normal = vh[2]
    u1 = vh[0]
    u2 = vh[1]

    x = shifted @ u1
    y = shifted @ u2
    A = np.column_stack([2 * x, 2 * y, np.ones_like(x)])
    b = x * x + y * y
    sol, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy, d = sol
    r_fit = float(np.sqrt(d + cx * cx + cy * cy))

    center = centroid + cx * u1 + cy * u2
    return center, normal, r_fit, u1, u2
