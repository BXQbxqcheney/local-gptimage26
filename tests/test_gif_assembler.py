"""GIF 合成测试：帧归一化 + 合成产物可被 Pillow 重开且帧数正确。"""

import io

import pytest
from PIL import Image

from app.gif.assembler import encode_gif, normalize_frame


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _frame_png(w, h, color) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.anyio
async def test_normalize_frame_letterboxes():
    """尺寸不一致的帧被居中放到统一画布。"""
    img = Image.open(io.BytesIO(_frame_png(100, 50, (255, 0, 0))))
    out = normalize_frame(img, (64, 64), (255, 255, 255))
    assert out.size == (64, 64)


@pytest.mark.anyio
async def test_encode_gif_roundtrip_frames():
    """合成 → Pillow 重开：帧数与循环标志正确（M3 验收断言）。"""
    frames = [_frame_png(64, 64, (255, 0, 0)), _frame_png(64, 64, (0, 255, 0)), _frame_png(64, 64, (0, 0, 255))]
    gif = await encode_gif(frames, (64, 64), duration_ms=100, loop=True, background_hex="#FFFFFF")
    opened = Image.open(io.BytesIO(gif))
    assert opened.format == "GIF"
    assert getattr(opened, "n_frames", 1) == 3
    assert getattr(opened, "info", {}).get("loop", None) == 0  # 0 = 无限循环


@pytest.mark.anyio
async def test_encode_gif_no_loop():
    frames = [_frame_png(32, 32, (0, 0, 0)), _frame_png(32, 32, (255, 255, 255))]
    gif = await encode_gif(frames, (32, 32), duration_ms=50, loop=False)
    opened = Image.open(io.BytesIO(gif))
    assert "loop" not in opened.info or opened.info["loop"] != 0


@pytest.mark.anyio
async def test_encode_gif_mixed_sizes_normalized():
    """中转端点返回不同尺寸帧 → 全部归一化到画布后合成。"""
    frames = [_frame_png(64, 64, (255, 0, 0)), _frame_png(100, 40, (0, 255, 0))]
    gif = await encode_gif(frames, (64, 64), duration_ms=80, loop=True)
    opened = Image.open(io.BytesIO(gif))
    assert opened.size == (64, 64)
    assert getattr(opened, "n_frames", 1) == 2


@pytest.mark.anyio
async def test_encode_gif_empty_raises():
    with pytest.raises(ValueError):
        await encode_gif([], (64, 64), duration_ms=80, loop=True)
