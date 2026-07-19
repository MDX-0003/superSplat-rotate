#!/bin/bash
# ============================================================
# 临时脚本：批量重命名文件/文件夹中的日期和序列号
# 源目录中的 "07-18" → "07-19"，"234508" → "546321"
# ============================================================
set -euo pipefail

# ==================== 硬编码配置（方便复用） ====================
SRC_DIR="C:/code/supersplat/CameraData/130-2026-07-18-234508"

# 替换对：OLD → NEW（按数组顺序逐一替换）
OLD_DATE="07-18"
NEW_DATE="07-19"

OLD_SEQ="234508"
NEW_SEQ="546321"
# =============================================================

echo "源目录: $SRC_DIR"
echo "替换规则: '$OLD_DATE' → '$NEW_DATE'   '$OLD_SEQ' → '$NEW_SEQ'"
echo "----------------------------------------"

if [ ! -d "$SRC_DIR" ]; then
    echo "错误: 源目录不存在 — $SRC_DIR"
    exit 1
fi

renamed_count=0

# 先处理深层再处理浅层，避免改名后路径失效
while IFS= read -r -d '' oldpath; do
    dir=$(dirname "$oldpath")
    oldname=$(basename "$oldpath")

    # 只对包含目标字符串的进行替换
    newname="$oldname"
    newname="${newname//$OLD_DATE/$NEW_DATE}"
    newname="${newname//$OLD_SEQ/$NEW_SEQ}"

    if [ "$oldname" != "$newname" ]; then
        newpath="$dir/$newname"
        echo "重命名: $oldname  →  $newname"
        mv "$oldpath" "$newpath"
        renamed_count=$((renamed_count + 1))
    fi
done < <(find "$SRC_DIR" -depth -print0)

echo "----------------------------------------"
echo "完成，共重命名 $renamed_count 个项目。"
