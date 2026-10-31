"""集中配置：全部来自环境变量，导入时即校验（fail fast）。"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("DATA_DIR", PROJECT_ROOT / "data")).resolve()
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", PROJECT_ROOT / "frontend")).resolve()

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
if MAX_UPLOAD_MB <= 0:
    raise ValueError("MAX_UPLOAD_MB 必须为正整数")

DEFAULT_SPEC = os.environ.get("DEFAULT_SPEC", "square_800")
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
MATTING_BACKEND = os.environ.get("MATTING_BACKEND", "u2net")

# 模型权重目录：可离线预置（scripts/fetch_models.sh），避免运行时依赖外网
MODELS_DIR = Path(os.environ.get("MODELS_DIR", PROJECT_ROOT / "models")).resolve()
MODELS_DIR.mkdir(parents=True, exist_ok=True)
# rembg 优先使用 U2NET_HOME 作为模型根目录（同时兼容新版 REMBG_HOME）
os.environ.setdefault("U2NET_HOME", str(MODELS_DIR))
os.environ.setdefault("REMBG_HOME", str(MODELS_DIR))
os.environ.setdefault("MODEL_CHECKSUM_DISABLED", "1")  # 本地预置权重不再校验 md5

UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"
RGBA_DIR = DATA_DIR / "rgba"
IMAGES_META_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "app.db"


def ensure_dirs() -> None:
    for d in (UPLOADS_DIR, OUTPUTS_DIR, RGBA_DIR, IMAGES_META_DIR):
        d.mkdir(parents=True, exist_ok=True)


def validate() -> None:
    """启动时校验配置合法性。"""
    from backend.layout import SPECS

    if DEFAULT_SPEC not in SPECS:
        raise ValueError(f"DEFAULT_SPEC 非法: {DEFAULT_SPEC}，可选 {list(SPECS)}")
