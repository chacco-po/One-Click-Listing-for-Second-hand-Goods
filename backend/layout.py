"""商品图排版引擎（Layout Engine）

将抠图得到的 RGBA 主体，合成为符合二手平台规范的白底商品图。
纯 PIL/numpy 实现：确定性输出、不依赖任何模型，直接决定成品是否可用。

能力：
- 多平台规格（1:1 800×800 标准 / 1:1 1200×1200 高清 / 3:4 900×1200 服饰鞋包）
- 主体自动裁剪、按占比缩放、居中或底部锚点（鞋类站立展示）
- 柔和投影增强质感
- 质检报告：主体占比、缩放倍数、边缘裁切/低清告警
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageFilter

# 平台常用商品图规格
SPECS: dict[str, dict] = {
    "square_800": {"width": 800, "height": 800, "label": "1:1 方形 · 800×800（平台标准）"},
    "square_1200": {"width": 1200, "height": 1200, "label": "1:1 方形 · 1200×1200（高清）"},
    "portrait_900": {"width": 900, "height": 1200, "label": "3:4 竖版 · 900×1200（服饰鞋包）"},
}

MAX_UPSCALE = 3.0  # 允许的最大放大倍数，超过会模糊并告警
MIN_SUBJECT_RATIO = 0.12  # 主体占成品面积比低于该值视为占比过小


@dataclass
class LayoutOptions:
    """排版参数。"""

    spec: str = "square_800"
    fill_ratio: float = 0.85          # 主体最大占画布比例（0.3~0.98）
    anchor: str = "center"            # center=居中 / bottom=底部对齐
    background: str = "#FFFFFF"       # 画布背景色
    shadow: bool = True               # 柔和投影
    alpha_threshold: int = 12         # alpha 低于该值视为背景噪声


@dataclass
class LayoutReport:
    """质检报告：记录排版过程与告警，用于前端展示与兜底提示。"""

    spec: str
    size: tuple[int, int]
    subject_bbox: tuple[int, int, int, int]   # 主体在原图中的包围盒 (x0,y0,x1,y1)
    subject_size: tuple[int, int]             # 裁剪出的主体尺寸
    scale: float                              # 实际缩放倍数
    subject_ratio: float                      # 主体像素占成品面积比
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "spec": self.spec,
            "size": list(self.size),
            "subject_bbox": list(self.subject_bbox),
            "subject_size": list(self.subject_size),
            "scale": round(self.scale, 3),
            "subject_ratio": round(self.subject_ratio, 4),
            "warnings": self.warnings,
        }


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"非法背景色: {value}")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def compose_white_bg(
    foreground: Image.Image,
    options: LayoutOptions | None = None,
) -> tuple[Image.Image, LayoutReport]:
    """把 RGBA 前景合成为平台规范白底商品图。

    返回 (成品 RGB 图像, 质检报告)。
    """
    opts = options or LayoutOptions()
    if opts.spec not in SPECS:
        raise ValueError(f"未知规格 {opts.spec}，可选: {list(SPECS)}")
    if opts.anchor not in ("center", "bottom"):
        raise ValueError(f"未知锚点 {opts.anchor}，可选: center / bottom")

    spec = SPECS[opts.spec]
    tw, th = spec["width"], spec["height"]
    fill = min(max(opts.fill_ratio, 0.3), 0.98)
    bg_rgb = _hex_to_rgb(opts.background)
    warnings: list[str] = []

    rgba = foreground.convert("RGBA")
    alpha = np.asarray(rgba)[..., 3]
    mask = alpha >= opts.alpha_threshold

    # ---- 兜底：无有效主体 ----
    if not mask.any():
        warnings.append("未检测到有效主体（抠图结果全透明），已输出空白画布，请重试或手动选择类目")
        blank = Image.new("RGB", (tw, th), bg_rgb)
        return blank, LayoutReport(opts.spec, (tw, th), (0, 0, 0, 0), (0, 0), 0.0, 0.0, warnings)

    # ---- 主体包围盒并裁剪 ----
    ys, xs = np.where(mask)
    x0, y0 = int(xs.min()), int(ys.min())
    x1, y1 = int(xs.max()) + 1, int(ys.max()) + 1
    src_w, src_h = rgba.size
    if x0 <= 1 or y0 <= 1 or x1 >= src_w - 1 or y1 >= src_h - 1:
        warnings.append("主体接触到原图边缘，可能已被裁切，建议重新拍摄")

    subject = rgba.crop((x0, y0, x1, y1))
    sw, sh = subject.size

    # ---- 按占比缩放（限制最大放大倍数）----
    avail_w, avail_h = tw * fill, th * fill
    scale = min(avail_w / sw, avail_h / sh)
    if scale > MAX_UPSCALE:
        warnings.append(
            f"主体分辨率较低（{sw}×{sh}），已限制放大 {MAX_UPSCALE}x，建议上传更高清照片"
        )
        scale = MAX_UPSCALE
    new_w, new_h = max(1, round(sw * scale)), max(1, round(sh * scale))
    subject_resized = subject.resize((new_w, new_h), Image.LANCZOS)

    # ---- 摆放位置：可用区域居中；bottom 锚点用于鞋类等站立展示 ----
    px = (tw - new_w) // 2
    if opts.anchor == "bottom":
        avail_top = (th - avail_h) / 2
        py = round(avail_top + avail_h - new_h)
    else:
        py = (th - new_h) // 2

    # ---- 合成画布（含柔和投影）----
    canvas = Image.new("RGBA", (tw, th), (*bg_rgb, 255))
    if opts.shadow:
        shadow_alpha = subject_resized.split()[3].filter(
            ImageFilter.GaussianBlur(radius=max(3, int(min(new_w, new_h) * 0.02)))
        )
        shadow_layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        shadow_img = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))
        shadow_img.putalpha(shadow_alpha.point(lambda a: int(a * 0.20)))
        shadow_layer.alpha_composite(shadow_img, (px, py + max(2, int(new_h * 0.015))))
        canvas.alpha_composite(shadow_layer)
    canvas.alpha_composite(subject_resized, (px, py))
    result = canvas.convert("RGB")

    # ---- 质检报告 ----
    subject_ratio = float(mask.sum()) * scale * scale / (tw * th)
    if subject_ratio < MIN_SUBJECT_RATIO:
        warnings.append(
            f"主体占比仅 {subject_ratio:.0%}，偏小；可提高「主体占比」或重新拍摄"
        )
    if scale > 1.5:
        warnings.append(f"原图主体较小，已放大 {scale:.1f}x，清晰度可能下降")

    report = LayoutReport(
        spec=opts.spec,
        size=(tw, th),
        subject_bbox=(x0, y0, x1, y1),
        subject_size=(sw, sh),
        scale=scale,
        subject_ratio=subject_ratio,
        warnings=warnings,
    )
    return result, report
