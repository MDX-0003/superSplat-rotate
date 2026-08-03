"""
从 raw_images 按前缀筛选 & 截取前 N 张，输出到 raw_images_clip。
只复制，不改动原始目录。
"""

import shutil
from pathlib import Path

# ============================================================
#   可调静态变量
# ============================================================
SOURCE_DIR = Path(r"E:\work\26.7_SKNJ\supersplat\CameraData\0719\raw_images")
OUTPUT_DIR = Path(r"E:\work\26.7_SKNJ\supersplat\CameraData\0719\raw_images_clip")

OLD_PREFIX = "130"       # 源文件夹 / 文件名中包含的前缀
NEW_PREFIX = "90"        # 替换后的前缀
KEEP_COUNT = 90          # 每个文件夹只保留前 N 张图

# 图片扩展名（小写匹配）
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
# ============================================================


def _is_image(file: Path) -> bool:
    return file.suffix.lower() in IMAGE_EXTENSIONS


def _renamed_path(src_path: Path, new_stem: str) -> Path:
    """Return src_path with the same parent but a new stem + original suffix."""
    return src_path.with_stem(new_stem)


def clip_folder(src_folder: Path, dst_folder: Path) -> tuple[int, int]:
    """Clip one source folder → destination folder.

    Returns (copied, skipped_not_image).
    """
    dst_folder.mkdir(parents=True, exist_ok=True)

    # 收集所有图片文件并按文件名排序
    images = sorted(
        [f for f in src_folder.iterdir() if f.is_file() and _is_image(f)],
        key=lambda f: f.name,
    )

    copied = 0
    skipped_not_image = 0

    # 只处理前 KEEP_COUNT 张
    for img in images[:KEEP_COUNT]:
        # 文件名中的 OLD_PREFIX → NEW_PREFIX
        new_name = img.name.replace(OLD_PREFIX, NEW_PREFIX)
        dst = dst_folder / new_name
        shutil.copy2(img, dst)
        copied += 1

    # 统计被跳过的非图片（或超过 keep 的图片）
    skipped_not_image = len([f for f in src_folder.iterdir() if f.is_file()]) - len(images)

    return copied, skipped_not_image


def main():
    if not SOURCE_DIR.is_dir():
        print(f"[ERROR] 源目录不存在: {SOURCE_DIR}")
        return

    print(f"源目录:   {SOURCE_DIR}")
    print(f"输出目录:  {OUTPUT_DIR}")
    print(f"前缀替换:  {OLD_PREFIX!r} → {NEW_PREFIX!r}")
    print(f"每文件夹保留前 {KEEP_COUNT} 张\n")

    # 收集所有子文件夹（非递归）
    src_folders = sorted(
        [f for f in SOURCE_DIR.iterdir() if f.is_dir()],
        key=lambda f: f.name,
    )

    if not src_folders:
        print("[WARN] 未找到任何子文件夹。")
        return

    total_copied = 0

    for folder in src_folders:
        # 文件夹名中的 OLD_PREFIX → NEW_PREFIX
        dst_name = folder.name.replace(OLD_PREFIX, NEW_PREFIX)
        dst_folder = OUTPUT_DIR / dst_name

        copied, skipped = clip_folder(folder, dst_folder)
        total_copied += copied

        print(f"  [{folder.name}] → [{dst_name}]  复制 {copied} 张图片"
              f"{'  跳过非图片 ' + str(skipped) if skipped else ''}")

    print(f"\n完成！共处理 {len(src_folders)} 个文件夹，复制 {total_copied} 张图片到：")
    print(f"  {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
