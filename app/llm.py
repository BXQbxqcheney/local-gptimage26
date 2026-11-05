"""OpenAI 兼容端点客户端：视觉对话、生图、参考图编辑生图。

- 裸 httpx 实现，不依赖 openai SDK（版本耦合更少、行为更可控）；
- 每次调用从 Key 池取 Key，按结果回报（成功/429/失败）；
- 图片返回 b64_json 与 url 两种格式均适配（中转端点行为差异）；
  url 下载走 SSRF 安全通道（提供商返回的 URL 也不免检）；
- 所有异常统一转为 LLMError，向上携带可读原因（兜底不吞错）。
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from .config import AppConfig, ProviderConfig
from .imaging import UnsafeUrlError, fetch_image_bytes_sync
from .keypool import KeyPool, NoAvailableKeyError

logger = logging.getLogger("img26.llm")

CHAT_TIMEOUT = 120.0
IMAGE_TIMEOUT = 300.0
GENERIC_RETRIES = 2  # 单 Key 失败后换 Key 重试的总额外次数


class LLMError(Exception):
    """LLM 调用失败（含全部 Key 耗尽）。"""


@dataclass
class LLMResult:
    """一次成功调用的结果与所用 Key 信息。"""

    content: str
    image_png: bytes | None  # 生图接口：图像 PNG 字节
    key_id: str
    provider: str


class OpenAICompatClient:
    """面向单一 provider 的 OpenAI 兼容客户端（生图/规划共用骨架）。"""

    def __init__(self, config: AppConfig, provider: ProviderConfig) -> None:
        self._config = config
        self._provider = provider
        self._pool = KeyPool(provider.name, provider.api_keys)

    @property
    def pool(self) -> KeyPool:
        return self._pool

    async def _run_with_pool(
        self,
        *,
        build_request: Callable[[str], tuple[str, str, dict]],
        parse_response: Callable[[dict], LLMResult],
        timeout: float,
    ) -> LLMResult:
        """通用执行骨架：acquire → 请求 → 回报 Key 状态 → 解析。

        build_request(key_value) -> (method, url, kwargs)
        parse_response(payload) -> LLMResult（key_id/provider 由骨架回填）
        429/5xx/网络错误换下一个 Key 重试；总尝试次数 = Key 数 × (1+RETRIES)。
        """
        last_error: Exception | None = None
        attempts = 0
        max_attempts = max(1, len(self._pool)) * (GENERIC_RETRIES + 1)
        while attempts < max_attempts:
            attempts += 1
            try:
                key = self._pool.acquire()
            except NoAvailableKeyError as exc:
                raise LLMError(str(exc)) from exc
            method, url, kwargs = build_request(key.value)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.request(method, url, **kwargs)
                if resp.status_code == 429:
                    self._pool.report_rate_limited(key)
                    last_error = LLMError(
                        f"429 限流（provider={self._provider.name}）"
                    )
                    continue
                if resp.status_code >= 500:
                    self._pool.report_failure(key)
                    last_error = LLMError(
                        f"服务端错误 HTTP {resp.status_code}（provider={self._provider.name}）"
                    )
                    continue
                if resp.status_code >= 400:
                    # 4xx：个别 Key 可能无权限，换 Key 再试；不计入长冷却
                    self._pool.report_failure(key)
                    last_error = LLMError(
                        f"请求被拒绝 HTTP {resp.status_code}：{resp.text[:300]}"
                    )
                    continue
                payload = resp.json()
                result = parse_response(payload)
                result.key_id = key.key_id
                result.provider = self._provider.name
                self._pool.report_success(key)
                return result
            except httpx.HTTPError as exc:
                self._pool.report_failure(key)
                last_error = LLMError(f"网络错误：{exc}")
                continue
            except (ValueError, KeyError, UnsafeUrlError) as exc:
                # 响应结构/下载问题：换 Key 大概率无效，直接失败但 Key 不惩罚
                raise LLMError(f"响应处理失败：{exc}") from exc
            finally:
                try:
                    self._pool.release(key)
                except Exception:  # noqa: BLE001 —— release 兜底，绝不掩盖主流程
                    logger.exception("Key release 失败（忽略）")
        raise LLMError(
            f"多次尝试后仍失败（provider={self._provider.name}）：{last_error}"
        )

    # ------------------------------------------------------------------
    # 公开能力 1：视觉对话（规划器用）
    # ------------------------------------------------------------------

    async def chat_vision(
        self,
        prompt: str,
        images_data_url: list[str] | None = None,
        system: str | None = None,
    ) -> LLMResult:
        """多模态对话。images_data_url 为 data:image/...;base64,... 列表。"""
        model = self._provider.planner_model

        def build(key: str):
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for img_url in images_data_url or []:
                content.append(
                    {"type": "image_url", "image_url": {"url": img_url}}
                )
            messages: list[dict[str, Any]] = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": content})
            return (
                "POST",
                f"{self._provider.base_url}/chat/completions",
                {
                    "headers": {"Authorization": f"Bearer {key}"},
                    "json": {
                        "model": model,
                        "messages": messages,
                        "temperature": 0.3,
                    },
                },
            )

        def parse(payload: dict) -> LLMResult:
            try:
                text = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError(
                    f"chat 响应缺少 choices/message/content：{str(payload)[:200]}"
                ) from exc
            if isinstance(text, list):  # 个别端点返回分段内容
                text = "".join(
                    seg.get("text", "") for seg in text if isinstance(seg, dict)
                )
            return LLMResult(
                content=str(text), image_png=None, key_id="", provider=""
            )

        return await self._run_with_pool(
            build_request=build, parse_response=parse, timeout=CHAT_TIMEOUT
        )

    async def chat_vision_stream(
        self,
        prompt: str,
        images_data_url: list[str] | None = None,
        system: str | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResult:
        """流式多模态对话（OpenAI SSE）。

        on_delta(chunk)：每收到一段增量文本即回调（UI 打字机展示）。
        重试策略：流开始前失败（429/网络/结构错误）换 Key 重试；
        已有增量输出后中断则直接抛错——重发会导致前端文本重复。
        """
        model = self._provider.planner_model
        last_error: Exception | None = None
        attempts = 0
        max_attempts = max(1, len(self._pool)) * (GENERIC_RETRIES + 1)

        def _build_messages():
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for img_url in images_data_url or []:
                content.append({"type": "image_url", "image_url": {"url": img_url}})
            messages: list[dict[str, Any]] = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": content})
            return messages

        while attempts < max_attempts:
            attempts += 1
            emitted = False  # 本次尝试是否已向回调发出增量（发出后中断不重试）
            try:
                key = self._pool.acquire()
            except NoAvailableKeyError as exc:
                raise LLMError(str(exc)) from exc
            try:
                async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as client:
                    async with client.stream(
                        "POST",
                        f"{self._provider.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {key.value}"},
                        json={
                            "model": model,
                            "messages": _build_messages(),
                            "temperature": 0.3,
                            "stream": True,
                        },
                    ) as resp:
                        if resp.status_code == 429:
                            self._pool.report_rate_limited(key)
                            last_error = LLMError("429 限流")
                            continue
                        if resp.status_code >= 400:
                            self._pool.report_failure(key)
                            body = (await resp.aread()).decode("utf-8", errors="replace")
                            last_error = LLMError(
                                f"HTTP {resp.status_code}：{body[:200]}"
                            )
                            continue
                        parts: list[str] = []
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                obj = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            choices = obj.get("choices") or [{}]
                            delta = choices[0].get("delta") or {}
                            chunk = delta.get("content") or ""
                            if not chunk:
                                continue
                            emitted = True
                            parts.append(chunk)
                            if on_delta is not None:
                                try:
                                    await on_delta(chunk)
                                except Exception:  # noqa: BLE001 —— 回调失败不影响内容累积
                                    logger.exception("流式回调失败（忽略）")
                        self._pool.report_success(key)
                        return LLMResult(
                            content="".join(parts),
                            image_png=None,
                            key_id=key.key_id,
                            provider=self._provider.name,
                        )
            except httpx.HTTPError as exc:
                self._pool.report_failure(key)
                if emitted:
                    raise LLMError(f"流式中断（已有增量输出，不重试）：{exc}") from exc
                last_error = LLMError(f"流式网络错误：{exc}")
                continue
            finally:
                try:
                    self._pool.release(key)
                except Exception:  # noqa: BLE001
                    logger.exception("Key release 失败（忽略）")
        raise LLMError(
            f"流式对话多次尝试后仍失败（provider={self._provider.name}）：{last_error}"
        )

    # ------------------------------------------------------------------
    # 公开能力 2/3：生图 与 参考图编辑生图
    # ------------------------------------------------------------------

    async def generate_image(self, prompt: str, size: str) -> LLMResult:
        """文本生图（/images/generations）。

        generations 类端点（Agnes / 火山方舟）把精确尺寸映射为档位+宽高比，
        避免端点静默标准化导致输出尺寸不可预期。
        """
        model = self._provider.image_model
        tier_style = self._provider.edit_style in (
            "generations_extra_body",
            "generations_image_field",
        )

        def build(key: str):
            if tier_style:
                tier, ratio = map_size_to_tier(size)
                body = {"model": model, "prompt": prompt, "size": tier, "ratio": ratio}
            else:
                body = {"model": model, "prompt": prompt, "n": 1, "size": size}
            return (
                "POST",
                f"{self._provider.base_url}/images/generations",
                {"headers": {"Authorization": f"Bearer {key}"}, "json": body},
            )

        return await self._run_with_pool(
            build_request=build,
            parse_response=_parse_image_payload,
            timeout=IMAGE_TIMEOUT,
        )

    async def edit_image(self, image_png: bytes, prompt: str, size: str) -> LLMResult:
        """参考图编辑生图（图生图）——链式帧生成的核心。

        按 provider.edit_style 分流：
        - edits_multipart：OpenAI 官方式，POST /images/edits（multipart）
        - generations_extra_body：Agnes 式，POST /images/generations，
          参考图以 Data URI 放入 extra_body.image，b64 返回
        - generations_image_field：火山方舟式，POST /images/generations，
          参考图为顶层 image 数组 + 顶层 response_format
        """
        if self._provider.edit_style == "generations_extra_body":
            return await self._edit_image_generations(image_png, prompt, size)
        if self._provider.edit_style == "generations_image_field":
            return await self._edit_image_generations_field(image_png, prompt, size)
        return await self._edit_image_multipart(image_png, prompt, size)

    async def _edit_image_multipart(
        self, image_png: bytes, prompt: str, size: str
    ) -> LLMResult:
        """OpenAI 官方式图生图（multipart /images/edits）。"""
        model = self._provider.image_model

        def build(key: str):
            return (
                "POST",
                f"{self._provider.base_url}/images/edits",
                {
                    "headers": {"Authorization": f"Bearer {key}"},
                    "files": {"image": ("frame.png", image_png, "image/png")},
                    "data": {"model": model, "prompt": prompt, "n": "1", "size": size},
                },
            )

        return await self._run_with_pool(
            build_request=build,
            parse_response=_parse_image_payload,
            timeout=IMAGE_TIMEOUT,
        )

    async def _edit_image_generations_field(
        self, image_png: bytes, prompt: str, size: str
    ) -> LLMResult:
        """火山方舟式图生图（JSON generations，顶层 image 数组）。

        参考 Ark 官方文档：图生图在 images/generations 请求体加顶层 `image`
        字段（string 或 string[]，URL 或 Data URI）；response_format 在顶层。
        """
        import base64 as _b64

        model = self._provider.image_model
        data_uri = (
            "data:image/png;base64," + _b64.b64encode(image_png).decode("ascii")
        )

        def build(key: str):
            tier, ratio = map_size_to_tier(size)
            return (
                "POST",
                f"{self._provider.base_url}/images/generations",
                {
                    "headers": {"Authorization": f"Bearer {key}"},
                    "json": {
                        "model": model,
                        "prompt": prompt,
                        "size": tier,
                        "ratio": ratio,
                        "image": [data_uri],
                        "response_format": "b64_json",
                    },
                },
            )

        return await self._run_with_pool(
            build_request=build,
            parse_response=_parse_image_payload,
            timeout=IMAGE_TIMEOUT,
        )

    async def _edit_image_generations(
        self, image_png: bytes, prompt: str, size: str
    ) -> LLMResult:
        """Agnes 式图生图（JSON /images/generations + extra_body.image）。

        参考 Agnes 官方文档：图生图不走 /images/edits；参考图为 Data URI 数组；
        response_format 必须放 extra_body 内（顶层 response_format 会报错）。
        """
        import base64 as _b64

        model = self._provider.image_model
        data_uri = (
            "data:image/png;base64," + _b64.b64encode(image_png).decode("ascii")
        )

        def build(key: str):
            tier, ratio = map_size_to_tier(size)
            return (
                "POST",
                f"{self._provider.base_url}/images/generations",
                {
                    "headers": {"Authorization": f"Bearer {key}"},
                    "json": {
                        "model": model,
                        "prompt": prompt,
                        "size": tier,
                        "ratio": ratio,
                        "extra_body": {
                            "image": [data_uri],
                            "response_format": "b64_json",
                        },
                    },
                },
            )

        return await self._run_with_pool(
            build_request=build,
            parse_response=_parse_image_payload,
            timeout=IMAGE_TIMEOUT,
        )


_AGNES_RATIOS: list[tuple[str, float]] = [
    ("1:1", 1.0),
    ("3:4", 3 / 4),
    ("4:3", 4 / 3),
    ("16:9", 16 / 9),
    ("9:16", 9 / 16),
    ("2:3", 2 / 3),
    ("3:2", 3 / 2),
    ("21:9", 21 / 9),
]


def map_size_to_tier(size: str) -> tuple[str, str]:
    """把 "宽x高" 映射到 Agnes 的档位（1K/2K/3K/4K）+ 最近宽高比。

    Agnes 不支持任意精确尺寸（512x512 会被静默标准化到 1K），主动映射
    才能让用户的选择可预期。非 "宽x高" 格式原样返回。
    """
    import re as _re

    m = _re.match(r"^\s*(\d{2,5})\s*x\s*(\d{2,5})\s*$", size or "")
    if not m:
        return size, "1:1"
    w, h = int(m.group(1)), int(m.group(2))
    long_side = max(w, h)
    tier = "1K" if long_side <= 1536 else "2K" if long_side <= 2560 else "3K" if long_side <= 3584 else "4K"
    aspect = w / h
    ratio = min(_AGNES_RATIOS, key=lambda r: abs(aspect - r[1]))[0]
    return tier, ratio


def _parse_image_payload(payload: dict[str, Any]) -> LLMResult:
    """解析生图响应：适配 b64_json 与 url 两种返回；url 走 SSRF 安全下载。"""

    def _from_item(item: dict[str, Any]) -> bytes:
        b64 = item.get("b64_json")
        if b64:
            return base64.b64decode(b64)
        url = item.get("url")
        if url:
            # 提供商返回的 URL 同样不可免检：走带 SSRF 校验的同步安全下载
            return fetch_image_bytes_sync(
                str(url),
                allow_private_address=False,
                timeout_seconds=120,
            )
        raise ValueError(f"图片响应既无 b64_json 也无 url：{str(item)[:200]}")

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError(f"images 响应 data 为空：{str(payload)[:200]}")
    png = _from_item(data[0])
    return LLMResult(content="", image_png=png, key_id="", provider="")


class ClientRegistry:
    """按 provider 名持有客户端实例（Key 池状态全局共享）。"""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._clients: dict[str, OpenAICompatClient] = {
            p.name: OpenAICompatClient(config, p) for p in config.providers
        }

    def image_client(self) -> OpenAICompatClient:
        """生图客户端（取第一个可用生图 provider）。"""
        p = self._config.find_image_provider()
        if p is None:
            raise LLMError("未配置生图 provider（config.yaml 缺 image_model）")
        return self._clients[p.name]

    def planner_client(self) -> OpenAICompatClient:
        """规划客户端（取第一个可用规划 provider）。"""
        p = self._config.find_planner_provider()
        if p is None:
            raise LLMError("未配置视觉规划 provider（config.yaml 缺 planner_model）")
        return self._clients[p.name]

    def pools_snapshot(self) -> list[dict[str, Any]]:
        """全部 Key 池状态（脱敏）。"""
        return [
            {"provider": name, "keys": client.pool.snapshot()}
            for name, client in self._clients.items()
        ]

    def all_clients(self) -> list[OpenAICompatClient]:
        """全部客户端实例（重置冷却等全池操作用）。"""
        return list(self._clients.values())
