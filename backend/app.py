"""FastAPI 应用入口：路由层（不含业务逻辑，业务在 layout/matting/recognition/pricing/drafts）。"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import config, drafts as drafts_repo
from backend.auth import AuthError, AuthRepository
from backend.config import (
    DB_PATH,
    DEFAULT_SPEC,
    FRONTEND_DIR,
    IMAGES_META_DIR,
    MAX_UPLOAD_MB,
    MATTING_BACKEND,
    OUTPUTS_DIR,
    RGBA_DIR,
    UPLOADS_DIR,
)
from backend.copywriting import EXTRA_FIELDS, CopyInput, clean_extras, generate_copy
from backend.drafts import DraftRepository
from backend.layout import SPECS, LayoutOptions, LayoutReport, compose_white_bg
from backend.matting import get_backend, limit_size
from backend.market import MarketError, MarketRepository
from backend.pricing import (
    CATEGORIES,
    CONDITION_QUESTIONS,
    GRADE_MAP,
    GRADES,
    compute_grade,
    suggest_price,
)
from backend.products import STATUSES, ProductRepository
from backend.recognition import get_recognizer

# ------------------------------------------------------------------ 日志（结构化 JSON + request id）
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
logger = logging.getLogger("idle-item-lister")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "msg": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# ------------------------------------------------------------------ 类型化错误
class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def safe_unlink(path: Path) -> bool:
    """删除单个文件，失败不抛错。

    注意：部分运行环境（如 WorkBuddy 沙箱）会把 ``Path.unlink`` 改写为「移入回收站」，
    此时文件已不存在会抛出 OSError（而非 FileNotFoundError），``missing_ok=True`` 并不生效；
    回收站操作也较慢。因此统一先判断存在性并吞掉所有 OSError，避免删除接口整体 500。
    """
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError as exc:
        logger.warning("unlink_failed path=%s error=%s", path, exc)
    return False


def safe_unlink_glob(directory: Path, pattern: str) -> int:
    """批量删除匹配文件，返回删除数量。"""
    removed = 0
    if not directory.exists():
        return 0
    for f in directory.glob(pattern):
        if safe_unlink(f):
            removed += 1
    return removed


# ------------------------------------------------------------------ 图片记录存储（内存 + 落盘 JSON）
class ImageStore:
    """处理中的图片记录：原图 / RGBA 抠图结果 / 成品图 / 识别结果。"""

    def __init__(self, meta_dir: Path):
        self.meta_dir = meta_dir
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, dict] = {}

    def _path(self, image_id: str) -> Path:
        return self.meta_dir / f"{image_id}.json"

    def save(self, record: dict) -> None:
        self._records[record["id"]] = record
        self._path(record["id"]).write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, image_id: str) -> dict | None:
        if image_id in self._records:
            return self._records[image_id]
        path = self._path(image_id)
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            self._records[image_id] = record
            return record
        return None

    def drop(self, image_id: str) -> None:
        self._records.pop(image_id, None)
        safe_unlink(self._path(image_id))
        for pattern_dir in (UPLOADS_DIR, RGBA_DIR, OUTPUTS_DIR):
            safe_unlink_glob(pattern_dir, f"{image_id}*")


image_store = ImageStore(IMAGES_META_DIR)
repo = DraftRepository(DB_PATH)
auth_repo = AuthRepository(DB_PATH)
product_repo = ProductRepository(DB_PATH)
market_repo = MarketRepository(DB_PATH)


def _migrate_drafts_table() -> None:
    """幂等迁移：草稿表补充归属/发布状态/文案字段。"""
    import sqlite3

    with sqlite3.connect(DB_PATH) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
        if "owner_id" not in cols:
            conn.execute("ALTER TABLE drafts ADD COLUMN owner_id TEXT")
        if "product_id" not in cols:
            conn.execute("ALTER TABLE drafts ADD COLUMN product_id TEXT")
        if "status" not in cols:
            conn.execute("ALTER TABLE drafts ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'")
        if "description" not in cols:
            conn.execute("ALTER TABLE drafts ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "selling_points" not in cols:
            conn.execute("ALTER TABLE drafts ADD COLUMN selling_points TEXT NOT NULL DEFAULT '[]'")


def _migrate_products_table() -> None:
    """幂等迁移：商品表补充文案字段与成交信息。"""
    import sqlite3

    with sqlite3.connect(DB_PATH) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
        if "description" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "selling_points" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN selling_points TEXT NOT NULL DEFAULT '[]'")
        if "sold_to" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN sold_to TEXT")
        if "sold_at" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN sold_at TEXT")


_migrate_drafts_table()
_migrate_products_table()


# ------------------------------------------------------------------ 认证
def current_user(request: Request) -> dict | None:
    """从 Authorization 头解析当前登录用户（未登录返回 None）。"""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return auth_repo.get_user_by_token(header[7:].strip())


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise AppError(401, "UNAUTHORIZED", "请先登录后再操作")
    return user


def optional_user(request: Request) -> dict | None:
    return current_user(request)


# ------------------------------------------------------------------ 应用
@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_dirs()
    config.validate()  # fail fast：配置非法时拒绝启动
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # 后台预热模型，避免首个请求等待过久
    threading.Thread(target=_warmup, daemon=True).start()
    logger.info("startup")
    yield
    logger.info("shutdown")


app = FastAPI(title="闲置物品一键上架 API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _warmup() -> None:
    backend, err = get_backend()
    if backend is not None:
        logger.info("matting_backend_ready name=%s", backend.name)
    else:
        logger.warning("matting_backend_unavailable reason=%s", err)
    recognizer, rec_err = get_recognizer()
    if recognizer is not None:
        logger.info("recognizer_ready weights=yolo11n.pt")
    else:
        logger.warning("recognizer_unavailable reason=%s", rec_err)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
    token = request_id_ctx.set(rid)
    start = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request_failed path=%s", request.url.path)
        raise
    finally:
        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "method=%s path=%s status=%s duration_ms=%.0f",
            request.method,
            request.url.path,
            getattr(response, "status_code", 500),
            elapsed,
        )
        request_id_ctx.reset(token)
    response.headers["X-Request-Id"] = rid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


# ------------------------------------------------------------------ 静态资源
config.ensure_dirs()  # 导入期即确保目录存在，避免 StaticFiles 挂载失败
app.mount("/outputs", StaticFiles(directory=OUTPUTS_DIR), name="outputs")
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    backend, backend_err = get_backend()
    recognizer, rec_err = get_recognizer()
    return {
        "status": "ok",
        "matting": {"backend": MATTING_BACKEND, "initialized": backend is not None, "error": backend_err},
        "recognition": {"initialized": recognizer is not None, "error": rec_err},
    }


# ------------------------------------------------------------------ 元信息
@app.get("/api/meta")
def meta():
    groups: dict[str, list[dict]] = {}
    for cid, c in CATEGORIES.items():
        groups.setdefault(c["group"], []).append(
            {"id": cid, "label": c["label"], "base_price": c["base_price"]}
        )
    backend, backend_err = get_backend()
    recognizer, rec_err = get_recognizer()
    return {
        "specs": [{"id": k, **v} for k, v in SPECS.items()],
        "default_spec": DEFAULT_SPEC,
        "category_groups": [{"group": g, "items": items} for g, items in groups.items()],
        "grades": GRADES,
        "condition_questions": CONDITION_QUESTIONS,
        "copywriting_fields": EXTRA_FIELDS,
        "limits": {"max_upload_mb": MAX_UPLOAD_MB},
        "services": {
            "matting": {
                "requested": MATTING_BACKEND,
                "name": backend.name if backend else None,
                "initialized": backend is not None,
                "error": backend_err,
            },
            "recognition": {
                "initialized": recognizer is not None,
                "error": rec_err,
            },
        },
    }


# ------------------------------------------------------------------ 图片处理（任务1/2/3）
def _open_image(raw: bytes) -> "Any":
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(raw))
    image = ImageOps.exif_transpose(image)  # 处理手机照片方向
    return image.convert("RGB")


def _apply_crop(image: "Any", box: dict) -> "Any":
    x, y, w, h = (float(box.get(k, 0)) for k in ("x", "y", "w", "h"))
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x and 0 < h <= 1 - y):
        raise ValueError("裁剪区域超出图片范围")
    W, H = image.size
    return image.crop((round(x * W), round(y * H), round((x + w) * W), round((y + h) * H)))


def _recognize(image: "Any") -> dict | None:
    recognizer, rec_err = get_recognizer()
    if recognizer is None:
        logger.warning("recognition_skipped reason=%s", rec_err)
        return None
    try:
        result = recognizer.recognize(image)
    except Exception as exc:  # noqa: BLE001 - 识别失败不阻塞主流程（兜底）
        logger.warning("recognition_failed error=%s", exc)
        return None
    return {
        "available": result.available,
        "primary": vars(result.primary) if result.primary else None,
        "detections": [vars(d) for d in result.detections],
        "category_id": result.category_id,
        "reason": result.reason,
    }


@app.post("/api/process")
def process_image(
    file: UploadFile = File(...),
    fine_edge: bool = Query(False, description="alpha matting 精细边缘（毛发/透明物体）"),
    crop: str | None = Query(None, description='归一化裁剪框 JSON，如 {"x":0.1,"y":0.1,"w":0.8,"h":0.8}'),
):
    """完整管线：上传 → 主体分割 → 白底图排版 → 物品识别。"""
    if not (file.content_type or "").startswith("image/"):
        raise AppError(400, "BAD_FILE_TYPE", "仅支持图片文件（JPG/PNG/WebP）")
    raw = file.file.read()
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise AppError(413, "FILE_TOO_LARGE", f"图片超过 {MAX_UPLOAD_MB}MB 限制")

    try:
        image = _open_image(raw)
    except Exception:
        raise AppError(400, "BAD_IMAGE", "无法解析图片，请更换文件")

    if crop:
        try:
            image = _apply_crop(image, json.loads(crop))
        except ValueError as exc:
            raise AppError(400, "BAD_CROP", str(exc)) from exc
        except Exception:
            raise AppError(400, "BAD_CROP", "裁剪参数非法")
    image = limit_size(image)

    image_id = uuid.uuid4().hex[:12]
    original_path = UPLOADS_DIR / f"{image_id}.jpg"
    image.save(original_path, quality=92)

    # 任务1：主体分割与背景移除
    backend, matting_err = get_backend()
    if backend is None:
        raise AppError(503, "MATTING_UNAVAILABLE", matting_err or "抠图服务不可用")
    try:
        rgba = backend.remove_background(image, fine_edge=fine_edge)
    except Exception as exc:  # noqa: BLE001
        logger.exception("matting_failed")
        raise AppError(500, "MATTING_FAILED", f"抠图失败: {exc}") from exc
    rgba_path = RGBA_DIR / f"{image_id}.png"
    rgba.save(rgba_path)

    # 任务2：白底商品图排版
    options = LayoutOptions(spec=DEFAULT_SPEC)
    try:
        result, report = compose_white_bg(rgba, options)
    except ValueError as exc:
        raise AppError(400, "BAD_LAYOUT_PARAMS", str(exc)) from exc
    result_path = OUTPUTS_DIR / f"{image_id}_{DEFAULT_SPEC}.png"
    result.save(result_path)

    # 任务3：物品识别与类目推断
    recognition = _recognize(image)

    record = {
        "id": image_id,
        "original_path": str(original_path),
        "rgba_path": str(rgba_path),
        "result_path": str(result_path),
        "spec": DEFAULT_SPEC,
        "layout_options": vars(options),
        "recognition": recognition,
        "fine_edge": fine_edge,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    image_store.save(record)

    return {
        "image_id": image_id,
        "original_url": f"/uploads/{original_path.name}",
        "result_url": f"/outputs/{result_path.name}",
        "spec": DEFAULT_SPEC,
        "report": report.to_dict(),
        "recognition": recognition,
        "matting_backend": backend.name,
        "matting_warning": matting_err,
    }


class LayoutParams(BaseModel):
    image_id: str
    spec: str = DEFAULT_SPEC
    fill_ratio: float = Field(0.85, ge=0.3, le=0.98)
    anchor: str = "center"
    shadow: bool = True


@app.post("/api/layout")
def relayout(params: LayoutParams):
    """用不同规格/占比/锚点重新排版（商品图排版引擎入口）。"""
    record = image_store.get(params.image_id)
    if not record:
        raise AppError(404, "IMAGE_NOT_FOUND", "图片不存在或已过期，请重新上传")
    if params.spec not in SPECS:
        raise AppError(400, "BAD_SPEC", f"未知规格 {params.spec}")
    if params.anchor not in ("center", "bottom"):
        raise AppError(400, "BAD_ANCHOR", "锚点仅支持 center / bottom")

    from PIL import Image

    rgba = Image.open(record["rgba_path"])
    options = LayoutOptions(
        spec=params.spec,
        fill_ratio=params.fill_ratio,
        anchor=params.anchor,
        shadow=params.shadow,
    )
    try:
        result, report = compose_white_bg(rgba, options)
    except ValueError as exc:
        raise AppError(400, "BAD_LAYOUT_PARAMS", str(exc)) from exc
    result_path = OUTPUTS_DIR / f"{params.image_id}_{params.spec}.png"
    result.save(result_path)

    record["result_path"] = str(result_path)
    record["spec"] = params.spec
    record["layout_options"] = vars(options)
    image_store.save(record)

    return {
        "image_id": params.image_id,
        "result_url": f"/outputs/{result_path.name}",
        "spec": params.spec,
        "report": report.to_dict(),
    }


@app.post("/api/reprocess")
def reprocess(image_id: str = Query(...), fine_edge: bool = True):
    """用精细边缘模式重新抠图（处理毛发/透明物体），并重新排版。"""
    record = image_store.get(image_id)
    if not record:
        raise AppError(404, "IMAGE_NOT_FOUND", "图片不存在或已过期，请重新上传")
    backend, _ = get_backend()
    if backend is None:
        raise AppError(503, "MATTING_UNAVAILABLE", "抠图服务不可用")

    from PIL import Image

    original = Image.open(record["original_path"])
    rgba = backend.remove_background(original, fine_edge=fine_edge)
    rgba_path = RGBA_DIR / f"{image_id}.png"
    rgba.save(rgba_path)

    options = LayoutOptions(**record["layout_options"])
    result, report = compose_white_bg(rgba, options)
    result_path = OUTPUTS_DIR / f"{image_id}_{options.spec}.png"
    result.save(result_path)

    record["rgba_path"] = str(rgba_path)
    record["result_path"] = str(result_path)
    record["fine_edge"] = fine_edge
    image_store.save(record)

    return {
        "image_id": image_id,
        "result_url": f"/outputs/{result_path.name}",
        "report": report.to_dict(),
    }


# ------------------------------------------------------------------ 成色与定价（任务5/6）
class ConditionParams(BaseModel):
    answers: dict[str, str]


@app.post("/api/condition")
def condition(params: ConditionParams):
    return compute_grade(params.answers)


class PricingParams(BaseModel):
    category_id: str
    grade_id: str


@app.post("/api/pricing")
def pricing(params: PricingParams):
    try:
        return suggest_price(params.category_id, params.grade_id)
    except ValueError as exc:
        raise AppError(400, "BAD_PRICING_PARAMS", str(exc)) from exc


# ------------------------------------------------------------------ 文案生成（任务4）
class CopywritingParams(BaseModel):
    category_id: str
    grade_id: str | None = None
    condition_answers: dict[str, str] = {}
    extras: dict[str, str] = {}
    image_id: str | None = None      # 传入则自动带上该次识别的结果
    price: float | None = Field(None, gt=0)


@app.post("/api/copywriting")
def copywriting(params: CopywritingParams):
    """依据识别结果 + 成色问卷 + 用户填写的少量信息，生成结构化标题与卖点描述。"""
    if params.category_id not in CATEGORIES:
        raise AppError(400, "BAD_CATEGORY", "类目不存在")

    answers = params.condition_answers or {}
    grade_info = _grade_info(params.grade_id, answers)

    # 识别结果：优先取本次上传的记录，其次取该类目的识别建议
    recognized_label = None
    recognized_confidence = None
    if params.image_id:
        record = image_store.get(params.image_id)
        recognition = (record or {}).get("recognition") or {}
        primary = recognition.get("primary") or {}
        recognized_label = primary.get("label_zh")
        recognized_confidence = primary.get("confidence")

    copy_in = CopyInput(
        category_id=params.category_id,
        grade_id=grade_info["grade_id"],
        answers=answers,
        recognized_label=recognized_label,
        recognized_confidence=recognized_confidence,
        extras=clean_extras(params.extras),
        price=params.price,
    )
    result = generate_copy(copy_in)
    result["grade_label"] = grade_info["grade_label"]
    logger.info(
        "copywriting_generated category=%s grade=%s engine=%s titles=%d",
        params.category_id, grade_info["grade_id"], result["engine"], len(result["titles"]),
    )
    return result


# ------------------------------------------------------------------ 草稿管理（任务9）
def _public_draft(draft: dict) -> dict:
    """把服务端文件路径转换为可访问的 URL，避免泄露本地文件系统路径。"""
    if not draft:
        return draft
    out = dict(draft)
    for key, prefix in (("image_path", "/outputs"), ("original_path", "/uploads")):
        rel = out.get(key)
        if rel:
            out[key] = f"{prefix}/{Path(rel).name}"
    return out


class DraftCreate(BaseModel):
    image_id: str
    category_id: str
    condition_answers: dict[str, str] = {}
    grade_id: str | None = None
    price: float | None = Field(None, gt=0)
    title: str = ""
    notes: str = ""
    description: str = ""
    selling_points: list = []


class DraftUpdate(BaseModel):
    title: str | None = None
    category_id: str | None = None
    condition_answers: dict[str, str] | None = None
    grade_id: str | None = None
    price: float | None = Field(None, gt=0)
    notes: str | None = None
    description: str | None = None
    selling_points: list | None = None


def _grade_info(grade_id: str | None, answers: dict) -> dict:
    if grade_id and grade_id in GRADE_MAP:
        grade = GRADE_MAP[grade_id]
        return {"grade_id": grade["id"], "grade_label": grade["label"],
                "multiplier": grade["multiplier"], "score": None, "missing": []}
    return compute_grade(answers)


@app.post("/api/drafts", status_code=201)
def create_draft(params: DraftCreate, request: Request):
    record = image_store.get(params.image_id)
    if not record:
        raise AppError(404, "IMAGE_NOT_FOUND", "图片不存在或已过期，请重新上传")
    if params.category_id not in CATEGORIES:
        raise AppError(400, "BAD_CATEGORY", "类目不存在")

    grade_info = _grade_info(params.grade_id, params.condition_answers)
    suggestion = suggest_price(params.category_id, grade_info["grade_id"])
    recognition = record.get("recognition") or {}
    primary = recognition.get("primary") or {}
    owner = optional_user(request)

    draft = repo.create({
        "title": params.title,
        "category_id": params.category_id,
        "category_label": CATEGORIES[params.category_id]["label"],
        "recognized_label": primary.get("label_zh"),
        "recognized_confidence": primary.get("confidence"),
        "condition_grade": grade_info["grade_id"],
        "condition_grade_label": grade_info["grade_label"],
        "condition_answers": params.condition_answers,
        "price": params.price if params.price else suggestion["suggested_mid"],
        "price_low": suggestion["suggested_low"],
        "price_high": suggestion["suggested_high"],
        "image_path": record["result_path"],
        "original_path": record["original_path"],
        "spec": record["spec"],
        "notes": params.notes,
        "description": params.description,
        "selling_points": params.selling_points,
        "owner_id": owner["id"] if owner else None,
        "status": "draft",
    })
    logger.info("draft_created id=%s category=%s grade=%s owner=%s",
                draft["id"], params.category_id, grade_info["grade_id"], owner["id"] if owner else "-")
    return _public_draft(draft)


@app.get("/api/drafts")
def list_drafts(request: Request):
    user = optional_user(request)
    items = repo.list()
    if user:
        # 登录用户：优先展示自己的草稿，同时保留未归属的历史草稿
        own = [d for d in items if d.get("owner_id") == user["id"]]
        shared = [d for d in items if not d.get("owner_id")]
        items = own + shared
    return {"items": [_public_draft(d) for d in items]}


@app.get("/api/drafts/{draft_id}")
def get_draft(draft_id: str):
    draft = repo.get(draft_id)
    if not draft:
        raise AppError(404, "DRAFT_NOT_FOUND", "草稿不存在")
    return _public_draft(draft)


@app.put("/api/drafts/{draft_id}")
def update_draft(draft_id: str, params: DraftUpdate):
    fields = params.model_dump(exclude_unset=True)
    if "category_id" in fields and fields["category_id"]:
        if fields["category_id"] not in CATEGORIES:
            raise AppError(400, "BAD_CATEGORY", "类目不存在")
        fields["category_label"] = CATEGORIES[fields["category_id"]]["label"]
    if "grade_id" in fields and fields["grade_id"]:
        if fields["grade_id"] not in GRADE_MAP:
            raise AppError(400, "BAD_GRADE", "成色不存在")
        grade = GRADE_MAP[fields["grade_id"]]
        fields["condition_grade"] = grade["id"]
        fields["condition_grade_label"] = grade["label"]
    if "title" in fields and fields["title"] is not None:
        fields["title"] = fields["title"].strip()[:60]
    draft = repo.update(draft_id, fields)
    if not draft:
        raise AppError(404, "DRAFT_NOT_FOUND", "草稿不存在")
    logger.info("draft_updated id=%s", draft_id)
    return _public_draft(draft)


@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: str):
    draft = repo.delete(draft_id)
    if not draft:
        raise AppError(404, "DRAFT_NOT_FOUND", "草稿不存在")
    # 清理处理过程中的中间文件（RGBA / 其余规格成品图）；已发布的商品保留自己的图片副本
    image_id = Path(draft["image_path"]).stem.split("_")[0]
    if not draft.get("product_id"):
        safe_unlink_glob(RGBA_DIR, f"{image_id}*")
        safe_unlink_glob(OUTPUTS_DIR, f"{image_id}*")
        image_store.drop(image_id)
    logger.info("draft_deleted id=%s", draft_id)
    return {"deleted": draft_id, "draft": _public_draft(draft)}


# ------------------------------------------------------------------ 认证：注册 / 登录
class RegisterParams(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    password: str = Field(..., min_length=6, max_length=64)


class LoginParams(BaseModel):
    username: str
    password: str


@app.post("/api/auth/register", status_code=201)
def register(params: RegisterParams):
    try:
        user = auth_repo.create_user(params.username, params.password)
    except AuthError as exc:
        raise AppError(400, exc.code, exc.message) from exc
    token = auth_repo.create_session(user["id"])
    logger.info("user_registered username=%s", user["username"])
    return {"token": token, "user": user}


@app.post("/api/auth/login")
def login(params: LoginParams):
    row = auth_repo.get_user_by_name(params.username)
    if row is None:
        raise AppError(401, "BAD_CREDENTIALS", "用户名或密码错误")
    from backend.auth import verify_password

    if not verify_password(params.password, row["salt"], row["password_hash"]):
        raise AppError(401, "BAD_CREDENTIALS", "用户名或密码错误")
    user = {"id": row["id"], "username": row["username"]}
    token = auth_repo.create_session(user["id"])
    logger.info("user_logged_in username=%s", user["username"])
    return {"token": token, "user": user}


@app.post("/api/auth/logout")
def logout(request: Request):
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        auth_repo.delete_session(header[7:].strip())
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    user = optional_user(request)
    return {"user": user}


# ------------------------------------------------------------------ 已上架商品
class PublishParams(BaseModel):
    draft_id: str
    title: str = Field(..., min_length=1, max_length=60)
    price: float | None = Field(None, gt=0)
    notes: str = ""
    description: str = ""
    selling_points: list = []


class ProductUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=60)
    price: float | None = Field(None, gt=0)
    status: str | None = None
    notes: str | None = None
    description: str | None = None
    selling_points: list | None = None


def _public_product(product: dict) -> dict:
    if not product:
        return product
    out = dict(product)
    for key, prefix in (("image_path", "/outputs"), ("original_path", "/uploads")):
        rel = out.get(key)
        if rel:
            out[key] = f"{prefix}/{Path(rel).name}"
    out["status_label"] = STATUSES.get(out.get("status", ""), out.get("status"))
    return out


@app.get("/api/products")
def list_products(user: dict = Depends(require_user)):
    return {"items": [_public_product(p) for p in product_repo.list_by_user(user["id"])]}


@app.post("/api/products", status_code=201)
def publish_product(params: PublishParams, user: dict = Depends(require_user)):
    """把草稿发布为商品（需登录；商品名称必填）。"""
    draft = repo.get(params.draft_id)
    if not draft:
        raise AppError(404, "DRAFT_NOT_FOUND", "草稿不存在")
    if draft.get("owner_id") and draft["owner_id"] != user["id"]:
        raise AppError(403, "FORBIDDEN", "无权发布该草稿")

    title = (params.title or draft.get("title") or "").strip()
    if not title:
        raise AppError(400, "TITLE_REQUIRED", "请填写商品名称")

    # 复制图片，避免草稿删除后商品图片失效
    from PIL import Image

    src = Path(draft["image_path"])
    if not src.exists():
        raise AppError(404, "IMAGE_NOT_FOUND", "商品图片缺失，请重新处理图片")
    product_id = uuid.uuid4().hex[:12]
    dst = OUTPUTS_DIR / f"p_{product_id}.png"
    Image.open(src).convert("RGB").save(dst)

    product = product_repo.create({
        "user_id": user["id"],
        "title": title,
        "category_id": draft["category_id"],
        "category_label": draft["category_label"],
        "condition_grade": draft["condition_grade"],
        "condition_grade_label": draft["condition_grade_label"],
        "price": params.price if params.price else draft["price"],
        "image_path": str(dst),
        "original_path": draft.get("original_path"),
        "notes": params.notes or draft.get("notes", ""),
        "description": params.description or draft.get("description", "") or "",
        "selling_points": params.selling_points or draft.get("selling_points", []) or [],
        "status": "on_sale",
        "draft_id": draft["id"],
    })
    repo.update(draft["id"], {"product_id": product["id"], "status": "published", "title": title})
    logger.info("product_published id=%s user=%s title=%s", product["id"], user["id"], title)
    return _public_product(product)


@app.get("/api/products/{product_id}")
def get_product(product_id: str, user: dict = Depends(require_user)):
    product = product_repo.get(product_id)
    if not product or product["user_id"] != user["id"]:
        raise AppError(404, "PRODUCT_NOT_FOUND", "商品不存在")
    return _public_product(product)


@app.put("/api/products/{product_id}")
def update_product(product_id: str, params: ProductUpdate, user: dict = Depends(require_user)):
    fields = params.model_dump(exclude_unset=True)
    if fields.get("status") and fields["status"] not in STATUSES:
        raise AppError(400, "BAD_STATUS", "状态取值非法")
    if fields.get("title"):
        fields["title"] = fields["title"].strip()[:60]
    product = product_repo.update(product_id, user["id"], fields)
    if not product:
        raise AppError(404, "PRODUCT_NOT_FOUND", "商品不存在")
    logger.info("product_updated id=%s", product_id)
    return _public_product(product)


@app.delete("/api/products/{product_id}")
def delete_product(product_id: str, user: dict = Depends(require_user)):
    product = product_repo.delete(product_id, user["id"])
    if not product:
        raise AppError(404, "PRODUCT_NOT_FOUND", "商品不存在")
    safe_unlink(Path(product["image_path"]))
    logger.info("product_deleted id=%s", product_id)
    return {"deleted": product_id}


# ------------------------------------------------------------------ 商城（公开浏览 + 购买）
def _public_shop_item(item: dict) -> dict:
    """商城商品序列化：图片路径转 URL，附状态文案与卖家信息。"""
    out = dict(item)
    rel = out.get("image_path")
    if rel:
        out["image_path"] = f"/outputs/{Path(rel).name}"
    out["status_label"] = STATUSES.get(out.get("status", ""), out.get("status"))
    # 不向其他用户泄露买家身份
    out.pop("last_buyer_id", None)
    out.pop("sold_to", None)
    out["description"] = out.get("description", "") or ""
    return out


@app.get("/api/shop")
def shop(
    request: Request,
    category_id: str | None = None,
    keyword: str | None = None,
    status: str | None = None,
    sort: str = "latest",
    only_bought: bool = False,
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """商城商品列表（无需登录即可浏览；登录后会标记「我买过的」「我发布的」）。"""
    viewer = optional_user(request)
    viewer_id = viewer["id"] if viewer else None
    items, total = market_repo.list_products(
        category_id=category_id,
        keyword=keyword,
        status=status,
        sort=sort,
        viewer_id=viewer_id,
        only_bought=only_bought,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [_public_shop_item(i) for i in items],
        "total": total,
        "limit": limit,
        "offset": offset,
        "logged_in": bool(viewer),
    }


@app.get("/api/shop/{product_id}")
def shop_detail(product_id: str, request: Request):
    """商城商品详情（公开）。"""
    viewer = optional_user(request)
    item = market_repo.get_public(product_id, viewer["id"] if viewer else None)
    if not item:
        raise AppError(404, "PRODUCT_NOT_FOUND", "商品不存在")
    if item.get("status") == "off_shelf":
        raise AppError(404, "PRODUCT_NOT_FOUND", "商品已下架")
    return _public_shop_item(item)


@app.post("/api/shop/{product_id}/buy")
def buy_product(product_id: str, user: dict = Depends(require_user)):
    """购买商品：需登录，不能买自己的，已售出/下架的买不了。"""
    try:
        order = market_repo.buy(product_id, user["id"])
    except MarketError as exc:
        status = 404 if exc.code == "PRODUCT_NOT_FOUND" else 409
        raise AppError(status, exc.code, exc.message) from exc
    logger.info("product_sold product=%s buyer=%s price=%s", product_id, user["id"], order["price"])
    return {"order": order, "message": "购买成功，商品已标记为已售出"}


@app.get("/api/orders")
def list_orders(request: Request, role: str = "buyer", user: dict = Depends(require_user)):
    """我的订单：role=buyer 我买到的；role=seller 我卖出的。"""
    if role not in ("buyer", "seller"):
        raise AppError(400, "BAD_ROLE", "role 只能是 buyer 或 seller")
    orders = market_repo.list_orders(user["id"], role)
    for o in orders:
        rel = o.get("image_path")
        if rel:
            o["image_path"] = f"/outputs/{Path(rel).name}"
    return {"items": orders, "role": role}

