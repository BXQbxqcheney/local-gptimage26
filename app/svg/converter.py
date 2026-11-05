"""SVG 矢量化模块：vtracer 封装（参数白名单 + 线程池隔离）。

- vtracer 是 Rust 扩展，同步阻塞，必须经 anyio.to_thread 在线程池执行，
  避免卡死事件循环（SSE/健康检查/其他任务必须始终可响应）；
- 参数经白名单钳制，非法值直接回默认，绝不透传给底层。
"""

from __future__ import annotations

import anyio
from PIL import Image

from ..config import SvgDefaults

# vtracer 参数白名单：名称 → (类型, 合法值集合或范围)
_ENUM_PARAMS: dict[str, set] = {
    "colormode": {"color", "binary"},
    "hierarchical": {"stacked", "cutout"},
    "mode": {"spline", "polygon"},
}
_INT_PARAMS: dict[str, tuple[int, int]] = {
    "filter_speckle": (1, 128),
    "color_precision": (1, 8),
    "layer_difference": (1, 256),
    "corner_threshold": (1, 179),
    "splice_threshold": (1, 90),
    "path_precision": (0, 8),
}
_FLOAT_PARAMS: dict[str, tuple[float, float]] = {
    "length_threshold": (0.5, 100.0),
}


def sanitize_params(raw: dict | None, defaults: SvgDefaults) -> dict:
    """参数清洗：只接受白名单内且在范围内的值，其余回默认。

    防御端点被恶意传入超范围参数拖垮底层 Rust 转换。
    """
    raw = raw or {}
    out: dict = {}
    for name, allowed in _ENUM_PARAMS.items():
        val = raw.get(name, getattr(defaults, name))
        out[name] = val if val in allowed else getattr(defaults, name)
    for name, (lo, hi) in _INT_PARAMS.items():
        val = raw.get(name, getattr(defaults, name))
        try:
            iv = int(val)
        except (TypeError, ValueError):
            iv = getattr(defaults, name)
        out[name] = max(lo, min(hi, iv))
    for name, (lo, hi) in _FLOAT_PARAMS.items():
        val = raw.get(name, getattr(defaults, name))
        try:
            fv = float(val)
        except (TypeError, ValueError):
            fv = getattr(defaults, name)
        out[name] = max(lo, min(hi, fv))
    return out


def _convert_sync(png_bytes: bytes, params: dict) -> str:
    """同步转换（只在线程池中执行）。

    vtracer.convert_raw_image_to_svg 接收原始图片字节与格式，
    返回 SVG 字符串（以 <?xml 开头，内含 <svg 根元素）。
    """
    import vtracer

    return vtracer.convert_raw_image_to_svg(
        png_bytes,
        "png",
        params["colormode"],
        params["hierarchical"],
        params["mode"],
        params["filter_speckle"],
        params["color_precision"],
        params["layer_difference"],
        params["corner_threshold"],
        params["length_threshold"],
        10,  # max_iterations：vtracer 默认 10，不对外暴露
        params["splice_threshold"],
        params["path_precision"],
    )


async def convert_to_svg(image: Image.Image, params: dict) -> str:
    """异步入口：图像 → SVG 字符串。

    先统一转 PNG 字节（保 RGBA），再交线程池执行 vtracer。
    vtracer 输出的 svg 根元素只有 width/height 没有 viewBox——在 HTML 里
    会被裁切而不是缩放，这里统一注入 viewBox 保证任何容器内正确缩放。
    """
    import io
    import re

    buf = io.BytesIO()
    conv = image if image.mode == "RGBA" else image.convert("RGBA")
    conv.save(buf, format="PNG")
    svg = await anyio.to_thread.run_sync(_convert_sync, buf.getvalue(), params)
    w, h = conv.size
    # 已有 viewBox 则不重复注入；否则注入并保留原 width/height（作为固有尺寸）
    if "viewBox" not in svg:
        svg = re.sub(
            r"<svg([^>]*?)\s*/?>",
            lambda m: f'<svg{m.group(1)} viewBox="0 0 {w} {h}">',
            svg,
            count=1,
        )
    return svg


def estimate_paths(svg_text: str) -> int:
    """估算 SVG 路径元素数量（元信息展示用）。"""
    return svg_text.count("<path")
