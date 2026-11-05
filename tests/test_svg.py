"""SVG 链路测试：参数清洗、真实 vtracer 转换、输出合法性。"""

import io

import pytest
from PIL import Image

from app.config import SvgDefaults
from app.svg import convert_to_svg, estimate_paths, sanitize_params


def _test_png(w=32, h=32) -> bytes:
    img = Image.new("RGBA", (w, h), (255, 0, 0, 255))
    for x in range(w // 2, w):
        for y in range(h):
            img.putpixel((x, y), (0, 0, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_sanitize_rejects_bad_enum():
    params = sanitize_params({"colormode": "evil", "mode": "spline"}, SvgDefaults())
    assert params["colormode"] == "color"  # 回默认


def test_sanitize_clamps_ints():
    params = sanitize_params({"filter_speckle": 99999, "path_precision": -5}, SvgDefaults())
    assert params["filter_speckle"] == 128
    assert params["path_precision"] == 0


def test_sanitize_ignores_unknown_keys():
    params = sanitize_params({"rm_rf": True}, SvgDefaults())
    assert "rm_rf" not in params


@pytest.mark.anyio
async def test_convert_real_vtracer():
    img = Image.open(io.BytesIO(_test_png()))
    svg = await convert_to_svg(img, sanitize_params(None, SvgDefaults()))
    assert svg.lstrip().startswith("<?xml")
    assert "<svg" in svg
    assert estimate_paths(svg) >= 1  # 红蓝两色至少两条路径


@pytest.fixture
def anyio_backend():
    return "asyncio"
