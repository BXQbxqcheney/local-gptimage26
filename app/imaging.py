"""图像处理底座：格式探测、预处理、URL 拉取（含 SSRF 防护）。

安全要点：
- 格式按 magic bytes 判定，不信任扩展名；
- URL 拉取前解析全部 A/AAAA 记录并逐一校验（防 DNS 重绑定），
  拒绝环回/私网/保留地址；仅允许 http/https；
- 限制请求体大小与超时；Pillow 打开时限制最大像素防解压炸弹。
"""

from __future__ import annotations

import asyncio
import ipaddress
import io
import socket
from dataclasses import dataclass
from typing import Any

import httpx
from PIL import Image, ImageOps

# magic bytes → Pillow 格式名（Pillow 侧仍会真实解码验证）
MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"BM", "BMP"),
    (b"RIFF", "WEBP"),  # RIFF....WEBP，需二次确认
]

ALLOWED_INPUT_FORMATS = {"PNG", "JPEG", "WEBP", "BMP", "GIF"}


class ImageFormatError(Exception):
    """输入不是受支持的位图格式。"""


class UnsafeUrlError(Exception):
    """URL 未通过 SSRF 安全校验。"""


class ImageTooLargeError(Exception):
    """图像超过大小限制。"""


def detect_format(data: bytes) -> str | None:
    """按 magic bytes 探测图片格式（不信任扩展名）。"""
    if len(data) < 12:
        return None
    for sig, fmt in MAGIC_SIGNATURES:
        if data.startswith(sig):
            if fmt == "WEBP" and data[8:12] != b"WEBP":
                continue
            return fmt
    return None


@dataclass
class PreparedImage:
    """预处理结果。"""

    image: Image.Image
    original_size: tuple[int, int]
    output_size: tuple[int, int]
    downscaled: bool
    detected_format: str | None


def prepare_image(
    data: bytes,
    *,
    max_pixels: int,
    downscale_long_side: int,
    max_bytes: int,
) -> PreparedImage:
    """解码 + 安全校验 + 归一化预处理。

    - 超过 max_bytes 直接拒绝；
    - magic bytes 探测 + Pillow 真实解码双重验证；
    - EXIF 转正、保 RGBA 透明；
    - 长边超过 downscale_long_side 时等比缩小（响应中注明）。
    """
    if len(data) > max_bytes:
        raise ImageTooLargeError(f"图片体积 {len(data)} 字节超过上限 {max_bytes} 字节")

    fmt = detect_format(data)
    if fmt is None or fmt not in ALLOWED_INPUT_FORMATS:
        raise ImageFormatError("不支持的图片格式（仅支持 PNG/JPEG/WebP/BMP/GIF 位图）")

    try:
        img = Image.open(io.BytesIO(data))
        img.load()  # 立即解码，把损坏图拦在这里
    except Exception as exc:  # noqa: BLE001 —— Pillow 异常族庞杂，统一转可读错误
        raise ImageFormatError(f"图片解码失败：{exc}") from exc

    # 防解压炸弹：像素总数超限直接拒绝
    width, height = img.size
    if width * height > max_pixels:
        raise ImageTooLargeError(
            f"图片像素 {width}x{height}={width * height} 超过上限 {max_pixels}"
        )

    original_size = (width, height)
    # EXIF 方向转正；GIF 只取首帧（动画输入不属于本端点职责）
    frame = ImageOps.exif_transpose(img)
    if frame.mode not in ("RGBA", "RGB"):
        frame = frame.convert("RGBA")

    # 长边限制：超出则等比缩小
    long_side = max(frame.size)
    if long_side > downscale_long_side:
        ratio = downscale_long_side / long_side
        new_size = (
            max(1, round(frame.size[0] * ratio)),
            max(1, round(frame.size[1] * ratio)),
        )
        frame = frame.resize(new_size, Image.LANCZOS)
        downscaled = True
    else:
        downscaled = False

    return PreparedImage(
        image=frame,
        original_size=original_size,
        output_size=frame.size,
        downscaled=downscaled,
        detected_format=fmt,
    )


def _validate_target_ip(host: str) -> None:
    """校验主机名解析出的全部 IP 是否为公网地址（拒绝环回/私网/保留段）。"""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"域名解析失败：{host}") from exc
    if not infos:
        raise UnsafeUrlError(f"域名无解析记录：{host}")
    seen: set[str] = set()
    for info in infos:
        ip = info[4][0]
        if ip in seen:
            continue
        seen.add(ip)
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError as exc:
            raise UnsafeUrlError(f"非法 IP：{ip}") from exc
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise UnsafeUrlError(f"目标地址 {ip} 属于环回/私网/保留地址，已拒绝")


def _assert_public_target(url: str, allow_private_address: bool) -> httpx.URL:
    """协议与目标地址校验（字面量 IP 与域名 DNS 全记录，防 DNS 重绑定）。"""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("URL 为空")
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise UnsafeUrlError("仅允许 http/https 协议")
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise UnsafeUrlError(f"URL 非法：{exc}") from exc
    if allow_private_address:
        return parsed
    host = parsed.host
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        _validate_target_ip(host)
        return parsed
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        raise UnsafeUrlError(f"目标地址 {host} 属于环回/私网/保留地址，已拒绝")
    return parsed


def _check_payload_size(data: bytes, max_bytes: int) -> bytes:
    """下载体大小校验。"""
    if len(data) > max_bytes:
        raise ImageTooLargeError(
            f"下载内容 {len(data)} 字节超过上限 {max_bytes} 字节"
        )
    return data


MAX_REDIRECT_HOPS = 4


async def fetch_image_from_url(
    url: str,
    *,
    allow_private_address: bool,
    timeout_seconds: int,
    max_bytes: int,
) -> bytes:
    """异步拉取 URL 图片：SSRF 校验（逐跳）→ 限时限大小下载。

    手动跟随重定向并对每一跳目标重新校验，防"公网 302 → 内网"绕过。
    """
    current = _assert_public_target(url, allow_private_address)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds), follow_redirects=False, trust_env=False
    ) as client:
        for _ in range(MAX_REDIRECT_HOPS):
            try:
                resp = await client.get(str(current))
            except httpx.HTTPError as exc:
                raise UnsafeUrlError(f"下载失败：{exc}") from exc
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise UnsafeUrlError("重定向缺少 location 头")
                current = _assert_public_target(
                    str(current.join(location)), allow_private_address
                )
                continue
            if resp.status_code != 200:
                raise UnsafeUrlError(f"下载失败：HTTP {resp.status_code}")
            return _check_payload_size(resp.content, max_bytes)
    raise UnsafeUrlError(f"重定向次数超过 {MAX_REDIRECT_HOPS} 次，已放弃")


def fetch_image_bytes_sync(
    url: str,
    *,
    allow_private_address: bool = False,
    timeout_seconds: int = 60,
    max_bytes: int = 64 * 1024 * 1024,
) -> bytes:
    """同步安全下载：与异步版同规则的 SSRF 校验（逐跳）+ 大小限制。

    供生图响应中 url 形式的图片下载使用（提供商返回的 URL 也不可免检）。
    """
    current = _assert_public_target(url, allow_private_address)
    with httpx.Client(
        timeout=httpx.Timeout(timeout_seconds), follow_redirects=False, trust_env=False
    ) as client:
        for _ in range(MAX_REDIRECT_HOPS):
            try:
                resp = client.get(str(current))
            except httpx.HTTPError as exc:
                raise UnsafeUrlError(f"图片下载失败：{exc}") from exc
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise UnsafeUrlError("重定向缺少 location 头")
                current = _assert_public_target(
                    str(current.join(location)), allow_private_address
                )
                continue
            if resp.status_code != 200:
                raise UnsafeUrlError(f"图片下载失败：HTTP {resp.status_code}")
            return _check_payload_size(resp.content, max_bytes)
    raise UnsafeUrlError(f"重定向次数超过 {MAX_REDIRECT_HOPS} 次，已放弃")


def image_to_png_bytes(img: Image.Image) -> bytes:
    """Pillow 图像 → PNG 字节（保 RGBA 透明）。"""
    buf = io.BytesIO()
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    img.save(buf, format="PNG")
    return buf.getvalue()


def image_to_data_url(img: Image.Image, fmt: str = "PNG") -> str:
    """Pillow 图像 → data URL（发给视觉模型读图用）。"""
    import base64

    buf = io.BytesIO()
    conv = img if img.mode == "RGBA" else img.convert("RGBA")
    conv.save(buf, format=fmt)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def clamp_size(w: int, h: int, max_side: int = 4096) -> tuple[int, int]:
    """尺寸钳制：长边不超上限（GIF 帧画布归一化前用）。"""
    long_side = max(w, h)
    if long_side <= max_side:
        return w, h
    ratio = max_side / long_side
    return max(1, round(w * ratio)), max(1, round(h * ratio))
