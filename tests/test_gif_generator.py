"""导演模式链式生成测试：剧本一次成型，画图 AI 以上一帧依次绘制（mock LLM）。"""

import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image

from app.config import AppConfig, ProviderConfig
from app.gif.generator import (
    CancelFlag,
    GenerationCancelled,
    GifGenerator,
    GifParams,
    _safe_join,
    parse_size,
)
from app.llm import ClientRegistry, LLMError, LLMResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _png(w=32, h=32, color=(200, 30, 30)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@dataclass
class _ScriptedClient:
    """按调用序号返回预设结果的 mock 客户端（含 edit 提示词捕获）。"""

    chats: list[str] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)
    chat_idx: int = 0
    image_idx: int = 0
    edit_prompts: list[str] = field(default_factory=list)

    async def chat_vision(self, prompt, images_data_url=None, system=None):
        text = self.chats[min(self.chat_idx, len(self.chats) - 1)] if self.chats else "{}"
        self.chat_idx += 1
        return LLMResult(content=text, image_png=None, key_id="k", provider="t")

    async def chat_vision_stream(self, prompt, images_data_url=None, system=None, on_delta=None):
        result = await self.chat_vision(prompt, images_data_url, system)
        if on_delta is not None and result.content:
            await on_delta(result.content[: len(result.content) // 2])
            await on_delta(result.content[len(result.content) // 2 :])
        return result

    async def generate_image(self, prompt, size):
        return LLMResult(content="", image_png=self.images[self.image_idx % len(self.images)], key_id="k", provider="t")

    async def edit_image(self, image_png, prompt, size):
        self.edit_prompts.append(prompt)
        self.image_idx += 1
        return LLMResult(content="", image_png=self.images[self.image_idx % len(self.images)], key_id="k", provider="t")


def _registry(monkeypatch, client: _ScriptedClient) -> ClientRegistry:
    cfg = AppConfig(
        providers=[
            ProviderConfig(
                name="t",
                base_url="https://x/v1",
                api_keys=["sk-test"],
                image_model="i",
                planner_model="p",
            )
        ]
    )
    monkeypatch.setattr(ClientRegistry, "image_client", lambda self: client)
    monkeypatch.setattr(ClientRegistry, "planner_client", lambda self: client)
    return ClientRegistry(cfg)


SCRIPT_JSON = json.dumps(
    {
        "first_frame_prompt": "a cat",
        "frame_changes": ["wave paw up", "hold", "paw down"],
        "loop_closure": "return exactly to the first pose",
        "style_anchor": "cartoon",
    }
)


@pytest.mark.anyio
async def test_director_chain_full_run(tmp_path, monkeypatch):
    """导演链路：1 次规划 + N-1 次 edits，逐帧以上一帧为参考（核心验收）。"""
    client = _ScriptedClient(
        chats=[SCRIPT_JSON],
        images=[_png(color=(i * 41 % 255, 100, 100)) for i in range(1, 6)],
    )
    gen = GifGenerator(_registry(monkeypatch, client))
    report = await gen.run(
        GifParams(prompt="cat wave", frames=4, size="512x512", duration_ms=100, loop=True,
                  background="#FFFFFF", frames_mode="fixed"),
        tmp_path,
        CancelFlag(),
    )
    assert len(report.frames) == 4
    assert (tmp_path / "animation.gif").is_file()
    assert report.loop_closed is True
    assert "completed all 4 scripted frames" in report.stopped_reason
    # 底图 generations + 3 次 edits；每次 edits 提示词含一致性约束与风格锚
    assert client.image_idx == 3
    assert len(client.edit_prompts) == 3
    for p in client.edit_prompts:
        assert "reference image" in p and "cartoon" in p
    # 末帧提示词附加剧本闭合指令
    assert "return exactly to the first pose" in client.edit_prompts[-1]
    # 各帧提示词使用剧本对应指令
    assert "wave paw up" in client.edit_prompts[0]
    assert "hold" in client.edit_prompts[1]
    assert "paw down" in client.edit_prompts[2]


@pytest.mark.anyio
async def test_auto_frames_uses_planned_frames(tmp_path, monkeypatch):
    """auto 模式：以 AI 设计的 frames_planned 为实际帧数。"""
    auto_script = json.dumps(
        {
            "frames_planned": 5,
            "first_frame_prompt": "a cat",
            "frame_changes": ["a", "b", "c", "d"],
            "loop_closure": "back",
            "style_anchor": "cartoon",
        }
    )
    client = _ScriptedClient(
        chats=[auto_script],
        images=[_png(color=(i * 37 % 255, 100, 100)) for i in range(1, 8)],
    )
    gen = GifGenerator(_registry(monkeypatch, client))
    report = await gen.run(
        GifParams(prompt="cat wave", frames=8, size="512x512", duration_ms=100, loop=True,
                  background="#FFF", frames_mode="auto"),
        tmp_path,
        CancelFlag(),
    )
    assert len(report.frames) == 5  # 上限 8，AI 设计 5 帧
    assert client.image_idx == 4  # 4 次 edits


@pytest.mark.anyio
async def test_auto_frames_clamps_bad_planned(tmp_path, monkeypatch):
    """auto 模式：AI 声明帧数超上限时钳制。"""
    over = json.dumps(
        {
            "frames_planned": 99,
            "first_frame_prompt": "a cat",
            "frame_changes": ["a"],
            "loop_closure": "c",
            "style_anchor": "s",
        }
    )
    client = _ScriptedClient(
        chats=[over],
        images=[_png(color=(i * 53 % 255, 60, 60)) for i in range(1, 6)],
    )
    gen = GifGenerator(_registry(monkeypatch, client))
    report = await gen.run(
        GifParams(prompt="x", frames=3, size="512x512", duration_ms=100, loop=True,
                  background="#FFF", frames_mode="auto"),
        tmp_path,
        CancelFlag(),
    )
    assert len(report.frames) == 3


@pytest.mark.anyio
async def test_reference_first_frame_uses_edit(tmp_path, monkeypatch):
    """有参考图：首帧走 edits（而非 generations）。"""
    gen_calls = {"edit": 0, "generate": 0}

    class _CountClient(_ScriptedClient):
        async def edit_image(self, image_png, prompt, size):
            gen_calls["edit"] += 1
            return await super().edit_image(image_png, prompt, size)

        async def generate_image(self, prompt, size):
            gen_calls["generate"] += 1
            return await super().generate_image(prompt, size)

    client = _CountClient(chats=[SCRIPT_JSON], images=[_png(), _png(color=(9, 9, 9)), _png(color=(8, 8, 8))])
    gen = GifGenerator(_registry(monkeypatch, client))
    report = await gen.run(
        GifParams(prompt="x", frames=2, size="512x512", duration_ms=100, loop=True,
                  background="#FFF", frames_mode="fixed"),
        tmp_path,
        CancelFlag(),
        reference_png=_png(color=(1, 1, 1)),
    )
    assert len(report.frames) == 2
    assert gen_calls["edit"] == 2  # 参考图首帧 + 第 2 帧
    assert gen_calls["generate"] == 0


@pytest.mark.anyio
async def test_cancel_mid_run(tmp_path, monkeypatch):
    cancel = CancelFlag()
    cancel.cancelled = True  # 启动即取消
    client = _ScriptedClient(chats=[SCRIPT_JSON], images=[_png(), _png()])
    gen = GifGenerator(_registry(monkeypatch, client))
    with pytest.raises(GenerationCancelled):
        await gen.run(
            GifParams(prompt="x", frames=3, size="512x512", duration_ms=100, loop=True,
                      background="#FFF", frames_mode="fixed"),
            tmp_path,
            cancel,
        )


def test_parse_size():
    assert parse_size("1024x1024") == (1024, 1024)
    assert parse_size(" 512 x 512 ") == (512, 512)
    with pytest.raises(ValueError):
        parse_size("1024")
    with pytest.raises(ValueError):
        parse_size("99999x99999")


def test_safe_join_blocks_traversal(tmp_path):
    assert _safe_join(Path(tmp_path), "frame_001.png").name == "frame_001.png"
    with pytest.raises(ValueError):
        _safe_join(Path(tmp_path), "../evil.png")
    with pytest.raises(ValueError):
        _safe_join(Path(tmp_path), "a/b.png")  # 子路径不允许
