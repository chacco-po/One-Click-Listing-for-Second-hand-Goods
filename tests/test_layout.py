"""商品图排版引擎单元测试（不依赖 AI 模型，pip 装完 pillow/numpy 即可运行）

运行：python tests/test_layout.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image

from backend.layout import SPECS, LayoutOptions, compose_white_bg


def make_circle(diameter: int = 200, canvas: int = 400) -> Image.Image:
    """生成一张带红色圆形的 RGBA 测试图。"""
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    r = diameter // 2
    cx = cy = canvas // 2
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(220, 40, 40, 255))
    return img


def test_spec_sizes():
    for spec_id, spec in SPECS.items():
        out, report = compose_white_bg(make_circle(), LayoutOptions(spec=spec_id))
        assert out.size == (spec["width"], spec["height"]), spec_id
        assert out.mode == "RGB"
        # PIL 椭圆描边含端点，实际包围盒 201x201
        assert report.subject_size[0] == report.subject_size[1] == 201, report.subject_size
    print("PASS test_spec_sizes")


def test_centered_and_scaled():
    out, report = compose_white_bg(make_circle(200), LayoutOptions(spec="square_800", fill_ratio=0.85))
    # 200px 圆形 -> scale = min(680/200, 680/200) = 3.4，被 MAX_UPSCALE=3.0 限制
    assert abs(report.scale - 3.0) < 1e-6, report.scale
    # 圆心为红色，四角为白底
    arr = np.asarray(out)
    assert arr[400, 400, 0] > 180 and arr[400, 400, 1] < 90, "圆心应为红色"
    assert (arr[5, 5] == 255).all() and (arr[795, 795] == 255).all(), "四角应为白色"
    # 主体占比 ≈ π*100²*9 / 640000 ≈ 0.44
    assert 0.40 < report.subject_ratio < 0.48, report.subject_ratio
    print("PASS test_centered_and_scaled")


def test_bottom_anchor():
    out, report = compose_white_bg(
        make_circle(200), LayoutOptions(spec="square_800", anchor="bottom", shadow=False)
    )
    arr = np.asarray(out)
    # 底部锚点：圆形下缘应贴近可用区域底部 (800-(800*(1-0.85))/2 = 740)
    rows = np.where(arr[..., 1] < 200)[0]  # 绿色通道：圆形 G=40，白底 G=255
    assert 735 <= rows.max() <= 745, rows.max()
    # 上缘距顶部应明显大于下缘距底部
    assert rows.min() > 60, rows.min()
    print("PASS test_bottom_anchor")


def test_empty_alpha_fallback():
    empty = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    out, report = compose_white_bg(empty, LayoutOptions(spec="square_800"))
    assert out.size == (800, 800)
    assert (np.asarray(out) == 255).all(), "空主体应输出纯白画布"
    assert report.warnings and "未检测到有效主体" in report.warnings[0]
    print("PASS test_empty_alpha_fallback")


def test_edge_touch_warning():
    img = make_circle(200, 200)  # 圆形贴满画布
    _, report = compose_white_bg(img, LayoutOptions())
    assert any("裁切" in w for w in report.warnings), report.warnings
    print("PASS test_edge_touch_warning")


def test_small_subject_warning():
    img = make_circle(40, 400)  # 很小的主体
    _, report = compose_white_bg(img, LayoutOptions())
    assert any("占比" in w for w in report.warnings), report.warnings
    print("PASS test_small_subject_warning")


def test_invalid_params():
    try:
        compose_white_bg(make_circle(), LayoutOptions(spec="nope"))
        raise AssertionError("应当抛出 ValueError")
    except ValueError:
        pass
    try:
        compose_white_bg(make_circle(), LayoutOptions(anchor="nope"))
        raise AssertionError("应当抛出 ValueError")
    except ValueError:
        pass
    print("PASS test_invalid_params")


if __name__ == "__main__":
    test_spec_sizes()
    test_centered_and_scaled()
    test_bottom_anchor()
    test_empty_alpha_fallback()
    test_edge_touch_warning()
    test_small_subject_warning()
    test_invalid_params()
    print("\n全部排版引擎测试通过 ✔")
