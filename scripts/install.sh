#!/usr/bin/env bash
# 一键安装：创建虚拟环境并安装全部依赖
# 用法：
#   bash scripts/install.sh          # 默认在项目根创建 .venv
#   PYTHON_BIN=<python路径> VENV_DIR=<venv路径> bash scripts/install.sh
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python}"
VENV_DIR="${VENV_DIR:-.venv}"

echo "[1/5] 创建虚拟环境: $VENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"

if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" || "$OSTYPE" == "cygwin" ]]; then
  VP="$VENV_DIR/Scripts/python.exe"
else
  VP="$VENV_DIR/bin/python"
fi

echo "[2/5] 升级 pip"
"$VP" -m pip install -U pip

echo "[3/5] 安装 Web 框架与图像基础库"
"$VP" -m pip install -r backend/requirements.txt

echo "[4/5] 安装 CPU 版 PyTorch（体积远小于 CUDA 版，课堂演示足够）"
"$VP" -m pip install torch --index-url https://download.pytorch.org/whl/cpu

echo "[5/5] 安装 Ultralytics(YOLO11 识别) 与 rembg(抠图)"
"$VP" -m pip install ultralytics rembg onnxruntime

echo "完成。首次运行 /api/process 时会自动下载模型权重（u2net.onnx / yolo11n.pt）。"
