"""
Convert COLMAP cameras.bin + images.bin → cameras.json.

Usage:
  python tills/colmap_bin_to_json.py --project 02
    reads  CameraData/02/colmap_bins/cameras.bin + images.bin
    writes CameraData/02/cameras.json
"""
import argparse
import collections
import json
import math
import os
import struct
from pathlib import Path

import numpy as np

from paths import project


CameraModel = collections.namedtuple(
    "CameraModel", ["model_id", "model_name", "num_params"]
)
Camera = collections.namedtuple("Camera", ["id", "model", "width", "height", "params"])
Image = collections.namedtuple(
    "Image", ["id", "qvec", "tvec", "camera_id", "name"]
)

CAMERA_MODELS = {
    CameraModel(model_id=0, model_name="SIMPLE_PINHOLE", num_params=3),
    CameraModel(model_id=1, model_name="PINHOLE", num_params=4),
    CameraModel(model_id=2, model_name="SIMPLE_RADIAL", num_params=4),
    CameraModel(model_id=3, model_name="RADIAL", num_params=5),
    CameraModel(model_id=4, model_name="OPENCV", num_params=8),
    CameraModel(model_id=5, model_name="OPENCV_FISHEYE", num_params=8),
    CameraModel(model_id=6, model_name="FULL_OPENCV", num_params=12),
    CameraModel(model_id=7, model_name="FOV", num_params=5),
    CameraModel(model_id=8, model_name="SIMPLE_RADIAL_FISHEYE", num_params=4),
    CameraModel(model_id=9, model_name="RADIAL_FISHEYE", num_params=5),
    CameraModel(model_id=10, model_name="THIN_PRISM_FISHEYE", num_params=12),
}
CAMERA_MODEL_IDS = {
    camera_model.model_id: camera_model for camera_model in CAMERA_MODELS
}


def read_next_bytes(fid, num_bytes, format_char_sequence, endian_character="<"):
    data = fid.read(num_bytes)
    return struct.unpack(endian_character + format_char_sequence, data)


def read_intrinsics_binary(path_to_model_file):
    cameras = {}
    with open(path_to_model_file, "rb") as fid:
        num_cameras = read_next_bytes(fid, 8, "Q")[0]
        for _ in range(num_cameras):
            camera_properties = read_next_bytes(
                fid, num_bytes=24, format_char_sequence="iiQQ"
            )
            camera_id = camera_properties[0]
            model_id = camera_properties[1]
            model_name = CAMERA_MODEL_IDS[model_id].model_name
            width = camera_properties[2]
            height = camera_properties[3]
            num_params = CAMERA_MODEL_IDS[model_id].num_params
            params = read_next_bytes(
                fid, num_bytes=8 * num_params, format_char_sequence="d" * num_params
            )
            cameras[camera_id] = Camera(
                id=camera_id,
                model=model_name,
                width=width,
                height=height,
                params=np.array(params),
            )
    assert len(cameras) == num_cameras
    return cameras


def read_extrinsics_binary(path_to_model_file):
    images = {}
    with open(path_to_model_file, "rb") as fid:
        num_reg_images = read_next_bytes(fid, 8, "Q")[0]
        for _ in range(num_reg_images):
            binary_image_properties = read_next_bytes(
                fid, num_bytes=64, format_char_sequence="idddddddi"
            )
            image_id = binary_image_properties[0]
            qvec = np.array(binary_image_properties[1:5])
            tvec = np.array(binary_image_properties[5:8])
            camera_id = binary_image_properties[8]

            image_name = ""
            current_char = read_next_bytes(fid, 1, "c")[0]
            while current_char != b"\x00":
                image_name += current_char.decode("utf-8")
                current_char = read_next_bytes(fid, 1, "c")[0]

            num_points2d = read_next_bytes(fid, num_bytes=8, format_char_sequence="Q")[0]
            read_next_bytes(
                fid,
                num_bytes=24 * num_points2d,
                format_char_sequence="ddq" * num_points2d,
            )
            images[image_id] = Image(
                id=image_id,
                qvec=qvec,
                tvec=tvec,
                camera_id=camera_id,
                name=image_name,
            )
    return images


def qvec2rotmat(qvec):
    return np.array(
        [
            [
                1 - 2 * qvec[2] ** 2 - 2 * qvec[3] ** 2,
                2 * qvec[1] * qvec[2] - 2 * qvec[0] * qvec[3],
                2 * qvec[3] * qvec[1] + 2 * qvec[0] * qvec[2],
            ],
            [
                2 * qvec[1] * qvec[2] + 2 * qvec[0] * qvec[3],
                1 - 2 * qvec[1] ** 2 - 2 * qvec[3] ** 2,
                2 * qvec[2] * qvec[3] - 2 * qvec[0] * qvec[1],
            ],
            [
                2 * qvec[3] * qvec[1] - 2 * qvec[0] * qvec[2],
                2 * qvec[2] * qvec[3] + 2 * qvec[0] * qvec[1],
                1 - 2 * qvec[1] ** 2 - 2 * qvec[2] ** 2,
            ],
        ]
    )


def focal2fov(focal, pixels):
    return 2 * math.atan(pixels / (2 * focal))


def fov2focal(fov, pixels):
    return pixels / (2 * math.tan(fov / 2))


def camera_to_json(idx, image, camera):
    if camera.model == "SIMPLE_PINHOLE":
        focal_length_x = camera.params[0]
        fovy = focal2fov(focal_length_x, camera.height)
        fovx = focal2fov(focal_length_x, camera.width)
    elif camera.model == "PINHOLE":
        focal_length_x = camera.params[0]
        focal_length_y = camera.params[1]
        fovy = focal2fov(focal_length_y, camera.height)
        fovx = focal2fov(focal_length_x, camera.width)
    else:
        raise ValueError(
            "Unsupported COLMAP camera model '{}'. This project expects "
            "undistorted SIMPLE_PINHOLE or PINHOLE cameras.".format(camera.model)
        )

    r = np.transpose(qvec2rotmat(image.qvec))
    t = np.array(image.tvec)

    rt = np.zeros((4, 4))
    rt[:3, :3] = r.transpose()
    rt[:3, 3] = t
    rt[3, 3] = 1.0

    w2c = np.linalg.inv(rt)
    position = w2c[:3, 3]
    rotation = w2c[:3, :3]

    return {
        "id": idx,
        "img_name": os.path.basename(image.name).split(".")[0],
        "width": camera.width,
        "height": camera.height,
        "position": position.tolist(),
        "rotation": [row.tolist() for row in rotation],
        "fy": fov2focal(fovy, camera.height),
        "fx": fov2focal(fovx, camera.width),
    }


def convert(cameras_bin, images_bin, output_json):
    cam_intrinsics = read_intrinsics_binary(cameras_bin)
    cam_extrinsics = read_extrinsics_binary(images_bin)

    images = sorted(cam_extrinsics.values(),
                    key=lambda image: os.path.basename(image.name).split(".")[0])

    json_cameras = []
    for idx, image in enumerate(images):
        camera = cam_intrinsics[image.camera_id]
        json_cameras.append(camera_to_json(idx, image, camera))

    with open(output_json, "w") as file:
        json.dump(json_cameras, file)

    return len(json_cameras)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Convert COLMAP cameras.bin + images.bin → cameras.json"
    )
    parser.add_argument(
        "--project", required=True,
        help="Project name under CameraData/ (e.g. '02')"
    )
    parser.add_argument(
        "--cameras-bin", default=None,
        help="Override path to cameras.bin (default: <project>/colmap_bins/cameras.bin)"
    )
    parser.add_argument(
        "--images-bin", default=None,
        help="Override path to images.bin (default: <project>/colmap_bins/images.bin)"
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Override output path (default: <project>/cameras.json)"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    proj = project(args.project)

    cameras_bin = Path(args.cameras_bin) if args.cameras_bin else proj / "colmap_bins" / "cameras.bin"
    images_bin = Path(args.images_bin) if args.images_bin else proj / "colmap_bins" / "images.bin"
    output = Path(args.output) if args.output else proj / "cameras.json"

    if not cameras_bin.is_file():
        print(f"ERROR: cameras.bin not found: {cameras_bin}")
        return
    if not images_bin.is_file():
        print(f"ERROR: images.bin not found: {images_bin}")
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    count = convert(str(cameras_bin), str(images_bin), str(output))
    print(f"Wrote {count} cameras → {output}")


if __name__ == "__main__":
    main()
