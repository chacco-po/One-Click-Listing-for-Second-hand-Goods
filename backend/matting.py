"""抠图后端（任务1：主体分割与背景移除）

可插拔设计，通过环境变量 MATTING_BACKEND 选择：
- u2net（默认）：rembg + U2Net 权重，使用宽松、CPU 可跑
- birefnet：rembg + BiRefNet，边缘细节更好，需 GPU，使用前请确认权重许可
- rmbg2.0：BriaAI RMBG-2.0，边缘质量更好，**非商用许可**（需自行准备权重，仅课堂演示）

任何后端初始化失败都会自动降级到 u2net，保证管线可用（兜底）。
"""
from __future__ import annotations

import os

from PIL import Image

MAX_INPUT_SIDE = 1600  # 超过该边长先等比缩小，保证 CPU 处理速度

LICENSE_NOTES = {
    "u2net": "rembg(MIT) + U2Net 权重，使用宽松、CPU 可跑",
    "birefnet": "BiRefNet，边缘细节好、需 GPU，使用前请确认权重许可（一般限研究/课堂演示）",
    "rmbg2.0": "RMBG-2.0（BriaAI），非商用许可，需在部署文档中标注，课堂演示不涉及商用分发",
}


class MattingError(Exception):
    """抠图失败（类型化错误）。"""


class MattingBackend:
    name = "base"
    license_note = ""

    def remove_background(self, image: Image.Image, fine_edge: bool = False) -> Image.Image:
        """移除背景，返回 RGBA 图像。fine_edge=True 时启用 alpha matting（毛发/透明边缘更精细）。"""
        raise NotImplementedError


class RembgBackend(MattingBackend):
    """基于 rembg 的抠图后端（u2net / birefnet-general 等）。"""

    def __init__(self, model: str = "u2net"):
        from rembg import new_session  # 延迟导入，未安装时给出明确错误

        self.name = f"rembg-{model}"
        self.license_note = LICENSE_NOTES.get(model, LICENSE_NOTES["u2net"])
        self._model = model
        self._session = new_session(model)  # 权重缺失时自动下载

    def remove_background(self, image: Image.Image, fine_edge: bool = False) -> Image.Image:
        from rembg import remove

        kwargs: dict = {"session": self._session}
        if fine_edge:
            # alpha matting：对毛发、透明物体边缘更精细，速度较慢
            kwargs.update(
                alpha_matting=True,
                alpha_matting_foreground_threshold=240,
                alpha_matting_background_threshold=10,
                alpha_matting_erode_size=10,
            )
        result = remove(image.convert("RGB"), **kwargs)
        if result.mode != "RGBA":
            result = result.convert("RGBA")
        return result


class OnnxAlphaBackend(MattingBackend):
    """RMBG 系列 ONNX 抠图后端（本地权重，无需外网）。

    权重放入 models/rmbg-1.4.onnx 即可启用（MATTING_BACKEND=rmbg14）。
    预处理/后处理遵循 RMBG 官方参考实现：1024×1024 归一化 → 归一化 alpha。
    """

    name = "rmbg-onnx"
    license_note = LICENSE_NOTES["rmbg2.0"]

    def __init__(self, model_path: str, size: int = 1024,
                 mean: tuple[float, float, float] = (0.5, 0.5, 0.5),
                 std: tuple[float, float, float] = (1.0, 1.0, 1.0)):
        import numpy as np
        import onnxruntime as ort

        self._np = np
        self._size = size
        self._mean = np.array(mean, dtype="float32").reshape(1, 1, 3)
        self._std = np.array(std, dtype="float32").reshape(1, 1, 3)
        self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])

    def remove_background(self, image: Image.Image, fine_edge: bool = False) -> Image.Image:
        np = self._np
        img = image.convert("RGB").resize((self._size, self._size), Image.LANCZOS)
        arr = np.asarray(img).astype("float32") / 255.0
        arr = (arr - self._mean) / self._std
        arr = arr.transpose(2, 0, 1)[None, ...]
        input_name = self._session.get_inputs()[0].name
        out = self._session.run(None, {input_name: arr})[0]
        pred = out[:, 0, :, :] if out.ndim == 4 else out
        pred = np.squeeze(pred)
        hi, lo = float(pred.max()), float(pred.min())
        pred = (pred - lo) / (hi - lo) if hi > lo else pred
        mask = Image.fromarray((pred.clip(0, 1) * 255).astype("uint8"), mode="L")
        mask = mask.resize(image.size, Image.LANCZOS)
        result = image.convert("RGBA")
        result.putalpha(mask)
        return result


def _build_backend(requested: str) -> MattingBackend:
    """按环境变量构造抠图后端；不可用时降级到 u2net。"""
    from backend.config import MODELS_DIR

    if requested in ("rmbg14", "rmbg-1.4", "rmbg2.0"):
        local = MODELS_DIR / "rmbg-1.4.onnx"
        if local.exists():
            return OnnxAlphaBackend(str(local))
        raise MattingError(
            f"未找到本地 RMBG 权重 {local}，请先执行 scripts/fetch_models.sh"
        )
    model_map = {"u2net": "u2net", "birefnet": "birefnet-general"}
    return RembgBackend(model_map.get(requested, "u2net"))


_backend: MattingBackend | None = None
_backend_error: str | None = None


def get_backend() -> tuple[MattingBackend | None, str | None]:
    """获取全局抠图后端（惰性初始化）。返回 (backend, error)。"""
    global _backend, _backend_error
    if _backend is not None or _backend_error is not None:
        return _backend, _backend_error

    requested = os.environ.get("MATTING_BACKEND", "u2net").lower()
    try:
        _backend = _build_backend(requested)
    except Exception as exc:  # noqa: BLE001 - 兜底：任何初始化失败都记录并降级
        _backend_error = f"抠图后端 {requested} 初始化失败: {exc}"
        try:
            _backend = _build_backend("u2net")
        except Exception as exc2:  # noqa: BLE001
            _backend = None
            _backend_error = f"抠图后端不可用: {exc2}"
    return _backend, _backend_error


def limit_size(image: Image.Image, max_side: int = MAX_INPUT_SIDE) -> Image.Image:
    """超过 max_side 的图片等比缩小，控制 CPU 耗时。"""
    w, h = image.size
    if max(w, h) <= max_side:
        return image
    ratio = max_side / max(w, h)
    return image.resize((round(w * ratio), round(h * ratio)), Image.LANCZOS)
