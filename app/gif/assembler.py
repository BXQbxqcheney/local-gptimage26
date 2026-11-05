"""GIF 合成模块：帧归一化 + 调色板量化 + 循环编码。

- 帧归一化：中转端点返回尺寸常与请求不符，合成前把每帧等比缩放并
  居中粘贴到统一画布（默认请求尺寸画布，白底），杜绝错位/裁切；
- 密集像素操作全部经 anyio.to_thread 隔离，不阻塞事件循环。
"""

from __future__ import annotations

import anyio
from PIL import Image


def _hex_to_rgb(color: str, fallback: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int, int]:
    """'#RRGGBB' → (r, g, b)；非法输入回白色。"""
    c = (color or "").strip()
    if c.startswith("#") and len(c) == 7:
        try:
            return tuple(int(c[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]
        except ValueError:
            return fallback
    return fallback


def normalize_frame(
    img: Image.Image, canvas_size: tuple[int, int], background: tuple[int, int, int]
) -> Image.Image:
    """单帧归一化：等比缩放 + 居中粘贴到统一画布（RGB，因为 GIF 无 alpha）。"""
    canvas = Image.new("RGB", canvas_size, background)
    frame = img if img.mode == "RGB" else img.convert("RGB")
    # 等比缩放到画布内
    ratio = min(canvas_size[0] / frame.width, canvas_size[1] / frame.height)
    new_size = (
        max(1, round(frame.width * ratio)),
        max(1, round(frame.height * ratio)),
    )
    if new_size != frame.size:
        frame = frame.resize(new_size, Image.LANCZOS)
    # 居中粘贴
    offset = (
        (canvas_size[0] - frame.width) // 2,
        (canvas_size[1] - frame.height) // 2,
    )
    canvas.paste(frame, offset)
    return canvas


def _encode_sync(
    frames_png: list[bytes],
    canvas_size: tuple[int, int],
    duration_ms: int,
    loop: bool,
    background_hex: str,
) -> bytes:
    """同步合成（只在线程池执行）。

    Pillow 策略：首帧自适应调色板（adaptive palette），
    其余帧 save(..., save_all=True, append_images=...) 追加。
    """
    import io

    if not frames_png:
        raise ValueError("帧列表为空，无法合成 GIF")
    bg = _hex_to_rgb(background_hex)
    images: list[Image.Image] = []
    for png in frames_png:
        opened = Image.open(io.BytesIO(png))
        opened.load()
        images.append(normalize_frame(opened, canvas_size, bg))

    loop_flag = 0 if loop else 1  # Pillow: 0 = 无限循环
    # 统一量化为 P 模式以保证帧间调色板一致（256 色自适应）
    quantized = [im.quantize(colors=256, method=Image.MEDIANCUT) for im in images]
    buf = io.BytesIO()
    first, rest = quantized[0], quantized[1:]
    first.save(
        buf,
        format="GIF",
        save_all=True,
        append_images=rest,
        duration=duration_ms,
        loop=loop_flag,
        optimize=True,
        disposal=2,  # 每帧后恢复背景，避免残影
    )
    return buf.getvalue()


async def encode_gif(
    frames_png: list[bytes],
    canvas_size: tuple[int, int],
    duration_ms: int,
    loop: bool,
    background_hex: str = "#FFFFFF",
) -> bytes:
    """异步入口：帧 PNG 列表 → GIF 字节。"""
    return await anyio.to_thread.run_sync(
        _encode_sync, frames_png, canvas_size, duration_ms, loop, background_hex
    )
