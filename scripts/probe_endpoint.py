"""端点探针：实测 OpenAI 兼容端点的三项能力（配置后运行，不虚构结论）。

用法：
    python scripts/probe_endpoint.py [--config config.yaml]

实测项（与项目依赖的三类调用一一对应）：
  1. /chat/completions 多模态读图（规划器依赖）
  2. /images/generations 文本生图（底图依赖）
  3. /images/edits 参考图编辑（链式帧生成依赖）

每项输出：通过/失败 + 返回格式（b64_json / url）+ 尺寸遵循情况。
生图响应中的 url 下载一律走 SSRF 安全通道（fetch_image_bytes_sync）。
结论可用于 README「已验证端点」清单与 config.yaml 选型。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from app.config import ConfigError, load_config  # noqa: E402
from app.imaging import UnsafeUrlError, fetch_image_bytes_sync  # noqa: E402


def _make_test_png(size: int = 64) -> bytes:
    """生成 64x64 测试图（红蓝对分），供读图与编辑探针使用。"""
    img = Image.new("RGB", (size, size), (255, 0, 0))
    for x in range(size // 2, size):
        for y in range(size):
            img.putpixel((x, y), (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def probe_chat_vision(base_url: str, key: str, model: str) -> dict:
    """探针 1：读图对话。"""
    data_url = (
        "data:image/png;base64," + base64.b64encode(_make_test_png()).decode("ascii")
    )
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "这张图左半是什么颜色？只答一个词。"},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
            },
        )
    if resp.status_code != 200:
        return {"ok": False, "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "detail": f"响应结构异常：{resp.text[:200]}"}
    return {"ok": True, "reply": str(content)[:100]}


def _parse_image_response(resp: httpx.Response) -> dict:
    """解析生图响应：b64/url 两种格式 + 实际尺寸（下载走 SSRF 安全通道）。"""
    if resp.status_code != 200:
        return {"ok": False, "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    try:
        data = resp.json()["data"][0]
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "detail": f"响应结构异常：{resp.text[:200]}"}
    png: bytes | None = None
    fmt = None
    if data.get("b64_json"):
        fmt = "b64_json"
        png = base64.b64decode(data["b64_json"])
    elif data.get("url"):
        fmt = "url"
        try:
            png = fetch_image_bytes_sync(str(data["url"]), timeout_seconds=120)
        except UnsafeUrlError as exc:
            return {"ok": False, "detail": f"图片 URL 被安全策略拒绝：{exc}"}
    if not png:
        return {"ok": False, "detail": "既无 b64_json 也无 url"}
    img = Image.open(io.BytesIO(png))
    return {"ok": True, "format": fmt, "size": f"{img.width}x{img.height}"}


async def probe_generate(base_url: str, key: str, model: str) -> dict:
    """探针 2：文本生图。"""
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{base_url}/images/generations",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "prompt": "a red circle on white background",
                "n": 1,
                "size": "512x512",
            },
        )
    return _parse_image_response(resp)


async def probe_edit(base_url: str, key: str, model: str, style: str = "edits_multipart") -> dict:
    """探针 3：参考图编辑（链式帧生成核心依赖）。

    按配置的 edit_style 实测；失败时附带另一风格的结论，方便选型。
    """
    result = await _probe_edit_style(base_url, key, model, style)
    if result.get("ok"):
        return result
    other = "generations_extra_body" if style == "edits_multipart" else "edits_multipart"
    alt = await _probe_edit_style(base_url, key, model, other)
    result["alternative"] = {**alt, "style": other}
    return result


async def _probe_edit_style(base_url: str, key: str, model: str, style: str) -> dict:
    """按单一风格实测参考图编辑。"""
    async with httpx.AsyncClient(timeout=300) as client:
        if style == "generations_extra_body":
            # Agnes 式：JSON generations + extra_body.image（Data URI），b64 返回
            data_uri = (
                "data:image/png;base64,"
                + base64.b64encode(_make_test_png()).decode("ascii")
            )
            resp = await client.post(
                f"{base_url}/images/generations",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "prompt": "make the left half yellow",
                    "size": "512x512",
                    "extra_body": {"image": [data_uri], "response_format": "b64_json"},
                },
            )
        else:
            # OpenAI 官方式：multipart /images/edits
            resp = await client.post(
                f"{base_url}/images/edits",
                headers={"Authorization": f"Bearer {key}"},
                files={"image": ("ref.png", _make_test_png(), "image/png")},
                data={
                    "model": model,
                    "prompt": "make the left half yellow",
                    "n": "1",
                    "size": "512x512",
                },
            )
    parsed = _parse_image_response(resp)
    parsed["style"] = style
    return parsed


async def main_async(config_path: str) -> int:
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2
    if not cfg.providers:
        print("config.yaml 未配置任何 provider，无法探测。")
        return 2

    for p in cfg.providers:
        print(f"\n===== provider: {p.name} ({p.base_url}) =====")
        key = p.api_keys[0]
        if p.planner_model:
            print(f"[1/3] 读图对话（{p.planner_model}）…")
            r = await probe_chat_vision(p.base_url, key, p.planner_model)
            print("  →", "通过" if r["ok"] else "失败", r)
        else:
            print("[1/3] 未配置 planner_model，跳过")
        if p.image_model:
            print(f"[2/3] 文本生图（{p.image_model}）…")
            r = await probe_generate(p.base_url, key, p.image_model)
            print("  →", "通过" if r["ok"] else "失败", r)
            print(f"[3/3] 参考图编辑（{p.image_model}，风格 {p.edit_style}）…")
            r = await probe_edit(p.base_url, key, p.image_model, p.edit_style)
            print("  →", "通过" if r["ok"] else "失败", r)
            if not r["ok"] and r.get("alternative"):
                alt = r["alternative"]
                tip = "通过" if alt.get("ok") else "也失败"
                print(f"  ↳ 备选风格 {alt.get('style')}：{tip} {alt if alt.get('ok') else ''}")
        else:
            print("[2/3][3/3] 未配置 image_model，跳过")
    print(
        "\n结论请汇总进项目 README「已验证端点」清单"
        "（端点 base_url / 返回格式 / 尺寸遵循情况）。"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenAI 兼容端点能力探针")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config.yaml）")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args.config)))
