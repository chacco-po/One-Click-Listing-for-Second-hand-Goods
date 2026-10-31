#!/usr/bin/env bash
# 预置模型权重到 models/ 目录
#
# 用法：
#   bash scripts/fetch_models.sh                      # 默认走 hf-mirror 镜像（国内最快）
#   HF_BASE=https://huggingface.co bash scripts/fetch_models.sh   # 强制指定源
#   bash scripts/fetch_models.sh --all-sources        # 依次尝试所有源
#   bash scripts/fetch_models.sh --check              # 只体检不下载
#
# 说明：
# - u2net.onnx    默认抠图后端权重（rembg 使用，许可宽松）      约 168MB
# - yolo11n.pt    识别模型权重（Ultralytics YOLO11，AGPL-3.0） 约 5.4MB
# - rmbg-1.4.onnx 可选高精度抠图权重（BriaAI，非商用许可）      约 168MB
#                 启用方式：MATTING_BACKEND=rmbg14
#
# 关键设计：
# 1) 多源自动回退 —— 官方 huggingface.co 在部分网络下会 502，
#    镜像源 hf-mirror.com 通常可用；脚本按序尝试直到成功。
# 2) 断点续传 —— curl -C - 支持中断后继续下载，避免重下 168MB。
# 3) 校验字节数 —— 下载不完整会删除半成品文件，避免后续加载报 cryptic 错误。
set -uo pipefail

HF_BASE="${HF_BASE:-https://hf-mirror.com}"
MODELS_DIR="${MODELS_DIR:-$(cd "$(dirname "$0")/.." && pwd)/models}"
CHECK_ONLY=0
TRY_ALL=0

# 候选源：第一个可用即采用；--all-sources 时逐个尝试
SOURCES=(
  "${HF_BASE}"
  "https://hf-mirror.com"
  "https://huggingface.co"
)

while [ $# -gt 0 ]; do
  case "$1" in
    --check)      CHECK_ONLY=1 ;;
    --all-sources) TRY_ALL=1 ;;
    -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（用 --help 查看用法）" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$MODELS_DIR"

# 权重清单："仓库路径|本地文件名|最小字节数|是否必需"
MANIFEST=(
  "AlenZeng/u2netonnxmodel/resolve/main/u2net.onnx|u2net.onnx|170000000|1"
  "Ultralytics/YOLO11/resolve/main/yolo11n.pt|yolo11n.pt|5000000|1"
  "briaai/RMBG-1.4/resolve/main/onnx/model.onnx|rmbg-1.4.onnx|170000000|0"
)

# 当前生效的源（探测阶段确定）
ACTIVE_SRC=""

probe_source() { # $1=base url -> 0 表示可达
  local code
  code=$(curl -sIL --max-time 15 -o /dev/null -w '%{http_code}' "$1/Ultralytics/YOLO11/resolve/main/yolo11n.pt" 2>/dev/null)
  [[ "$code" == "200" ]]
}

echo "=============================================="
echo " 模型权重预置"
echo " 目标目录：$MODELS_DIR"
echo "=============================================="

# ---- 源探测 ----
if [ "$TRY_ALL" -eq 1 ]; then
  for s in "${SOURCES[@]}"; do
    if probe_source "$s"; then ACTIVE_SRC="$s"; break; fi
  done
else
  # 首选源优先，不通则自动回退到其余源
  for s in "${HF_BASE}" "https://hf-mirror.com" "https://huggingface.co"; do
    if probe_source "$s"; then ACTIVE_SRC="$s"; break; fi
  done
fi

if [ -z "$ACTIVE_SRC" ]; then
  echo "[fail] 所有候选源均不可达，无法下载权重。" >&2
  echo "" >&2
  echo "      已尝试：" >&2
  for s in "${SOURCES[@]}"; do echo "        - $s" >&2; done
  echo "" >&2
  echo "      排查建议：" >&2
  echo "        1) 检查网络/代理：curl -I https://hf-mirror.com" >&2
  echo "        2) 手动下载后放入 models/ 目录（文件名须完全一致）：" >&2
  echo "             u2net.onnx    ← AlenZeng/u2netonnxmodel" >&2
  echo "             yolo11n.pt    ← Ultralytics/YOLO11" >&2
  echo "             rmbg-1.4.onnx ← briaai/RMBG-1.4 (可选)" >&2
  echo "        3) 网络完全不可用时，程序仍可启动：抠图与识别会降级，" >&2
  echo "           界面会提示「模型不可用」，其余功能（登录/商城/定价/文案）不受影响。" >&2
  exit 1
fi

echo "[源  ] $ACTIVE_SRC"
echo ""

fetch() { # $1=仓库路径 $2=本地文件名 $3=最小字节数
  local repo="$1" out="$MODELS_DIR/$2" min="$3"
  local url="$ACTIVE_SRC/$repo"

  if [ -f "$out" ] && [ "$(wc -c < "$out")" -ge "$min" ]; then
    echo "[skip] $2 已存在（$(wc -c < "$out") 字节）"
    return 0
  fi

  if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "[待取] $2 （需要 $min 字节以上）"
    return 0
  fi

  echo "[get ] $2 <- $url"
  # -C - 支持断点续传；--retry 应对瞬时抖动
  if ! curl -fL -C - --retry 3 --retry-delay 2 --connect-timeout 20 \
            -o "$out" "$url" 2>/dev/null; then
    echo "[fail] $2 下载失败（网络中断或源不可用）" >&2
    # 半成品留着无益，但保留以便断点续传
    return 1
  fi

  local size; size=$(wc -c < "$out")
  if [ "$size" -lt "$min" ]; then
    echo "[fail] $2 下载不完整（$size < $min 字节）" >&2
    rm -f "$out"
    return 1
  fi
  echo "[ok  ] $2 ($size 字节)"
  return 0
}

FAILED=0
for entry in "${MANIFEST[@]}"; do
  IFS='|' read -r repo fname min required <<< "$entry"
  if ! fetch "$repo" "$fname" "$min"; then
    if [ "$required" = "1" ]; then
      FAILED=1
    else
      echo "[warn] $fname 获取失败（非必需，可跳过）" >&2
    fi
  fi
  echo ""
done

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "体检模式（未实际下载）。当前目录："
  ls -lh "$MODELS_DIR" 2>/dev/null || true
  exit 0
fi

echo "权重目录：$MODELS_DIR"
ls -lh "$MODELS_DIR" 2>/dev/null || true

echo ""
if [ "$FAILED" -eq 1 ]; then
  echo "[结果] 存在必需权重获取失败，请检查上方错误信息。" >&2
  exit 1
fi

echo "[结果] 必需权重已就绪，可执行 bash scripts/dev.sh 启动服务。"
