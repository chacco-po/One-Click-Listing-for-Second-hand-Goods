#!/usr/bin/env bash
# 打包一个「干净」的源码压缩包，用于 GitHub 网页版上传（Releases / 直接拖 zip）
#
# 为什么需要这个：
#   GitHub 网页端「Add file → Upload files」完全无视 .gitignore，
#   拖整个文件夹会连 data/ 与 models/ 一起传，总计约 250MB，必然失败。
#   本脚本按 git 的已跟踪清单打包，保证压缩包里只有源码。
#
# 用法：
#   bash scripts/pack_source.sh            # 生成到 dist/
#   bash scripts/pack_source.sh /任意/路径   # 生成到指定目录
set -euo pipefail

cd "$(dirname "$0")/.."
OUT_DIR="${1:-dist}"
ARCHIVE="$OUT_DIR/idle-item-lister-source.tar.gz"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "[fail] 当前不是 git 仓库，无法按已跟踪清单打包" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

# 以 HEAD 的已跟踪文件为准，天然排除 data/ 与 models/
FILES=$(git ls-files)
if [[ -z "$FILES" ]]; then
  echo "[fail] 没有已跟踪文件，先 git add 至少一个文件" >&2
  exit 1
fi

echo "[1/3] 统计待打包文件…"
COUNT=$(printf '%s\n' "$FILES" | wc -l | tr -d ' ')
SIZE_KB=$(printf '%s\n' "$FILES" | tr '\n' '\0' | xargs -0 -r du -ck 2>/dev/null | tail -1 | awk '{print $1}')
echo "      ${COUNT} 个文件，合计约 $((SIZE_KB / 1024))MB"

# 校验：不允许夹带大文件
BIG=$(printf '%s\n' "$FILES" | tr '\n' '\0' | xargs -0 -r ls -l 2>/dev/null \
      | awk '$5 > 52428800 {printf "%.1f MB  %s\n", $5/1048576, $9}')
if [[ -n "$BIG" ]]; then
  echo "[fail] 已跟踪文件中存在超大文件，请先处理：" >&2
  printf '%s\n' "$BIG" >&2
  exit 1
fi

echo "[2/3] 生成压缩包…"
rm -f "$ARCHIVE"
tar -czf "$ARCHIVE" -T <(printf '%s\n' "$FILES")

echo "[3/3] 校验产物…"
ARCHIVE_MB=$(( $(du -k "$ARCHIVE" | awk '{print $1}') / 1024 ))
echo "      $ARCHIVE （约 ${ARCHIVE_MB}MB）"

# 确认没有夹带 data/ models/
if tar -tzf "$ARCHIVE" | grep -qE '^(data|models)/.*[^k]$'; then
  echo "[fail] 压缩包中疑似夹带 data/ 或 models/ 内容，请检查" >&2
  exit 1
fi

echo ""
echo "完成。上传方式二选一："
echo "  A) 解压到本地后用 git 提交推送（推荐，保留提交历史）："
echo "       cd <解压目录> && git init && git add -A && git commit -m 'init'"
echo "  B) 直接上传到 GitHub 仓库页面："
echo "       打开仓库 → Add file → Upload files → 拖入本目录下的源码文件夹"
echo ""
echo "注意：无论哪种方式，models/ 与 data/ 都不在压缩包内，"
echo "      克隆后需执行 bash scripts/fetch_models.sh 预置权重。"
