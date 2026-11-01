#!/usr/bin/env bash
# 一键安装：创建虚拟环境并安装全部依赖
#
# 用法：
#   bash scripts/install.sh                    # 默认在项目根创建 .venv
#   PYTHON_BIN=<python路径> VENV_DIR=<venv路径> bash scripts/install.sh
#   bash scripts/install.sh --skip-ai          # 跳过 AI 依赖（只装 Web 框架，无法抠图/识别）
#
# 重要说明（踩坑记录）：
#   1) PyTorch 必须走官方 CPU 源。直接 `pip install torch` 会连带拉入整套
#      NVIDIA CUDA 运行时（数 GB），在无 GPU 的机器上既慢又毫无用处。
#   2) 安装顺序很关键：先装 CPU 版 torch，再装 ultralytics。
#      否则 ultralytics 会把 PyPI 的 CUDA 版 torch 当作依赖装回来。
#   3) opencv 必须用 headless 版：服务器/无桌面环境缺少 GUI 库会导入失败。
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python}"
VENV_DIR="${VENV_DIR:-.venv}"
SKIP_AI=0

# 国内镜像源。实测（2026-10）官方 PyPI 约 10 KB/s，腾讯云约 960 KB/s，
# 相差近 90 倍；装 torch 这类大包时差距尤其明显（数分钟 vs 数小时）。
# 海外网络环境可设 PIP_INDEX_URL 为官方源：
#   PIP_INDEX_URL=https://pypi.org/simple bash scripts/install.sh
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.cloud.tencent.com/pypi/simple}"
export PIP_INDEX_URL

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-ai) SKIP_AI=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

echo "[1/6] 创建虚拟环境: $VENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"

if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" || "$OSTYPE" == "cygcn" || "$OSTYPE" == "cygwin" ]]; then
  VP="$VENV_DIR/Scripts/python.exe"
else
  VP="$VENV_DIR/bin/python"
fi
echo "      解释器：$VP"
echo "      pip 源：$PIP_INDEX_URL"

echo "[2/6] 升级 pip"
"$VP" -m pip install -U pip

echo "[3/6] 安装 Web 框架与图像基础库（fastapi / uvicorn / pillow / numpy）"
# numpy 锁 <2.3：2.5.x 与 numba / pymatting（rembg 依赖链）存在 ABI 隐患，
# 且 onnxruntime 对 numpy 版本也有隐式要求，锁区间最稳。
"$VP" -m pip install -r backend/requirements.txt
"$VP" -m pip install "numpy<2.3" --index-url "$PIP_INDEX_URL"

if [[ "$SKIP_AI" -eq 1 ]]; then
  echo "[4/6] 已跳过 AI 依赖（--skip-ai）"
  echo "[5/6] 已跳过 AI 依赖"
  echo "[6/6] 安装测试依赖"
  "$VP" -m pip install httpx websocket-client
  echo ""
  echo "完成（不含 AI 能力）。抠图与识别接口会返回「模型不可用」提示，其余功能正常。"
  exit 0
fi

echo "[4/6] 安装 CPU 版 PyTorch（约 200MB，不含 CUDA 依赖）"
# 国内镜像同步的是 PyPI 上的 torch（不带 +cpu 后缀），但已剔除 nvidia-* CUDA 依赖，
# 体积约 200MB 而非数 GB，正是无 GPU 环境所需。
# 校验方式：装完后确认 site-packages 下没有 nvidia 目录。
"$VP" -m pip install torch --index-url "$PIP_INDEX_URL"
if "$VP" -c "import torch, os, sys; sys.exit(0)" 2>/dev/null; then
  NVCUDA=$("$VP" -c "
import os, sysconfig
sp = sysconfig.get_paths()['purelib']
print('yes' if os.path.isdir(os.path.join(sp, 'nvidia')) else 'no')
")
  if [ "$NVCUDA" = "yes" ]; then
    echo "      检测到 CUDA 依赖，改用官方 CPU 源重装"
    "$VP" -m pip uninstall -y torch
    "$VP" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
  else
    echo "      已安装无 CUDA 依赖的 CPU 版 torch ✔"
  fi
fi

echo "[5/6] 安装 Ultralytics(YOLO11 识别) 与 rembg(抠图) + onnxruntime"
echo "      提示：torch 已就位，pip 会复用而不再下载 CUDA 版"
# onnxruntime 版本必须锁定。实测（2026-11，Windows / Python 3.13）：
#   1.31.1 → 导入时 Segmentation fault（退出码 139）
#   1.22.0 → ImportError: DLL load failed（DLL 初始化例程失败）
#   1.20.1 → 正常 ✔
# 根因在 pybind11 绑定层（onnxruntime_pybind11_state.pyd）与 Python 3.13 的 ABI 兼容，
# 与 onnxruntime.dll 本身无关（C API 直调正常）。
ORT_VER="${ORT_VER:-1.20.1}"
"$VP" -m pip install "onnxruntime==$ORT_VER" --index-url "$PIP_INDEX_URL"

echo "      校验 onnxruntime 可导入…"
if ! "$VP" -c "import onnxruntime" 2>/dev/null; then
  echo "      onnxruntime 导入失败，回退到 1.20.1…"
  "$VP" -m pip install "onnxruntime==1.20.1" --index-url "$PIP_INDEX_URL"
  "$VP" -c "import onnxruntime; print('      onnxruntime', onnxruntime.__version__, 'OK')" \
    || { echo "[fail] onnxruntime 无法导入，请检查 Python 版本（建议 3.10~3.12）" >&2; exit 1; }
else
  echo "      onnxruntime $ORT_VER OK"
fi

"$VP" -m pip install ultralytics rembg opencv-python-headless --index-url "$PIP_INDEX_URL"

echo "[6/6] 安装测试依赖（httpx / websocket-client）"
"$VP" -m pip install httpx websocket-client

echo ""
echo "=============================================="
echo " 安装完成"
echo "=============================================="
echo ""
echo "下一步："
echo "  bash scripts/fetch_models.sh   # 预置模型权重（约 174MB）"
echo "  bash scripts/dev.sh            # 启动服务（http://127.0.0.1:8765）"
echo ""
echo "自检："
echo "  bash scripts/check_repo_size.sh"
echo "  $VP scripts/smoke_test.py"
