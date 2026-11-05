"""规划器测试：结构化输出整形与容错降级（mock 客户端，不花真钱）。"""

import io

import pytest
from PIL import Image

from app.gif.planner import (
    ClosureCheck,
    FrameDecision,
    PlanScript,
    Planner,
    _coerce_decision,
    _coerce_script,
)
from app.llm import LLMError, LLMResult


def _tiny_png() -> bytes:
    """真实 4x4 小 PNG（规划器读图压缩需真解码）。"""
    img = Image.new("RGB", (4, 4), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeClient:
    """mock 视觉客户端：返回预设文本。"""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

    async def chat_vision(self, prompt, images_data_url=None, system=None):
        self.calls.append({"prompt": prompt, "images": images_data_url})
        return LLMResult(content=self._content, image_png=None, key_id="k", provider="t")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_coerce_script_ok():
    parsed = {
        "first_frame_prompt": "A cat waving",
        "frame_changes": ["paw up", "paw down"],
        "loop_closure": "return to start",
        "style_anchor": "flat cartoon",
    }
    s = _coerce_script(parsed, 4)
    assert isinstance(s, PlanScript)
    assert len(s.frame_changes) == 2


def test_coerce_script_bad_returns_none():
    assert _coerce_script("not a dict", 4) is None
    assert _coerce_script({"frame_changes": []}, 4) is None  # 缺首帧描述


def test_coerce_decision_clamps_consistency():
    d = _coerce_decision({"action": "continue", "consistency": 5})
    assert d.consistency == 1.0
    d2 = _coerce_decision({"action": "continue", "consistency": "abc"})
    assert d2.consistency == 0.8  # 解析失败回默认


@pytest.mark.anyio
async def test_plan_script_parses_fenced_json():
    client = _FakeClient('```json\n{"first_frame_prompt": "cat", "frame_changes": ["wave"], "loop_closure": "c", "style_anchor": "s"}\n```')
    planner = Planner(client)
    script = await planner.plan_script("a dancing cat", 2)
    assert script.first_frame_prompt == "cat"
    assert script.style_anchor == "s"


@pytest.mark.anyio
async def test_plan_script_streaming_deltas():
    """流式剧本：on_delta 收到增量，最终剧本解析正确。"""
    body = '{"first_frame_prompt": "cat", "frame_changes": ["wave"], "loop_closure": "c", "style_anchor": "s"}'

    class _StreamClient(_FakeClient):
        async def chat_vision_stream(self, prompt, images_data_url=None, system=None, on_delta=None):
            from tests.test_gif_planner import _FakeClient as _F

            result = await _F.chat_vision(self, prompt, images_data_url, system)
            if on_delta:
                await on_delta(result.content[:5])
                await on_delta(result.content[5:])
            return result

    planner = Planner(_StreamClient(body))
    chunks: list[str] = []

    async def on_delta(c: str) -> None:
        chunks.append(c)

    script = await planner.plan_script("cat dance", 2, on_delta=on_delta)
    assert script.first_frame_prompt == "cat"
    assert len(chunks) == 2
    assert "".join(chunks) == body


@pytest.mark.anyio
async def test_plan_script_invalid_json_raises():
    planner = Planner(_FakeClient("完全不是 JSON"))
    with pytest.raises(LLMError, match="剧本"):
        await planner.plan_script("x", 2)


@pytest.mark.anyio
async def test_decide_next_degrades_on_bad_json():
    planner = Planner(_FakeClient("垃圾输出"))
    decision = await planner.decide_next("p", PlanScript(first_frame_prompt="f", style_anchor="s"), _tiny_png(), _tiny_png(), 2, 4)
    assert decision.degraded is True
    assert decision.action == "continue"
    assert decision.next_frame_prompt  # 降级提示词非空


@pytest.mark.anyio
async def test_decide_next_reads_two_images():
    client = _FakeClient('{"action": "stop", "reason": "done", "consistency": 0.9}')
    planner = Planner(client)
    decision = await planner.decide_next("p", PlanScript(first_frame_prompt="f"), _tiny_png(), _tiny_png(), 3, 4)
    assert decision.action == "stop"
    # 读图上下文固定为两图（首帧+上一帧）
    assert len(client.calls[0]["images"]) == 2


@pytest.mark.anyio
async def test_check_closure_fallback_closed():
    planner = Planner(_FakeClient("不是 JSON"))
    check = await planner.check_closure("p", _tiny_png(), _tiny_png())
    assert isinstance(check, ClosureCheck)
    assert check.closed is True  # 解析失败按闭合处理
