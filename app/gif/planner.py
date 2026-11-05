"""视觉规划器：剧本生成、逐帧决策、首尾闭合检查。

用户指定的核心机制："每一帧的规划让一个可以读图的模型搞，适当停止"。
- plan_script()：读参考图（可选）→ 全片剧本（首帧描述/逐帧变化/闭合要求/风格锚），支持流式输出
- decide_next()：读"首帧+上一帧" → continue/stop + 下一帧提示词 + 一致性分数
- check_closure()：读首尾两帧 → 是否闭合 + 过渡帧提示词

读图性能：发给视觉模型的图统一压缩为 512px JPEG 的 Base64 Data URI
（原 1024px PNG base64 后约 2MB/张，是规划慢的主因；压缩后 ~60KB，
读图 token 与处理时间大幅下降，且规划只需看构图不需看细节）。
所有结构化输出经 jsonx 五级容错解析；解析失败由调用方降级"沿用剧本"。
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..jsonx import parse_json_robust
from ..llm import LLMError, OpenAICompatClient

SYSTEM_PROMPT = (
    "你是 GIF 动画导演与帧规划专家。你只输出合法 JSON，不输出任何解释文字。"
    "JSON 字符串值内不要出现未转义的换行与引号。"
)

# 视觉读图压缩尺寸：规划只需构图信息
VISION_MAX_SIDE = 512


def _png_to_vision_data_url(png: bytes, max_side: int = VISION_MAX_SIDE) -> str:
    """PNG 字节 → 压缩后的 JPEG Base64 Data URI（读图专用，显著提速）。"""
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    img.load()
    if img.mode != "RGB":
        img = img.convert("RGB")
    long_side = max(img.size)
    if long_side > max_side:
        ratio = max_side / long_side
        img = img.resize(
            (max(1, round(img.width * ratio)), max(1, round(img.height * ratio))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


@dataclass
class PlanScript:
    """全片剧本：链式生成的全局一致性锚点。"""

    first_frame_prompt: str = ""
    frame_changes: list[str] = field(default_factory=list)  # 第 2..N 帧每帧变化要点
    loop_closure: str = ""  # 末帧→首帧过渡要求
    style_anchor: str = ""  # 风格锚（每帧提示词常驻）
    frames_planned: int | None = None  # auto 模式下 AI 自主设计的帧数
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FrameDecision:
    """逐帧决策结果。"""

    action: str = "continue"  # continue | stop
    next_frame_prompt: str = ""
    reason: str = ""
    consistency: float = 0.8  # 0-1，generator 侧有硬阈值
    degraded: bool = False  # True = 本次为降级决策（JSON 解析失败沿用剧本）


@dataclass
class ClosureCheck:
    """首尾闭合检查结果。"""

    closed: bool = True
    transition_prompt: str = ""
    reason: str = ""


def _png_to_data_url(png: bytes) -> str:
    """PNG 字节 → data URL。"""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


class Planner:
    """视觉规划器（无状态，可并发调用）。"""

    def __init__(self, client: OpenAICompatClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # 剧本生成
    # ------------------------------------------------------------------

    async def plan_script(
        self,
        prompt: str,
        frames: int,
        reference_png: bytes | None = None,
        frames_mode: str = "fixed",
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> PlanScript:
        """生成全片剧本（流式输出）；有参考图时读图，剧本须保持参考图主体与风格。

        on_delta(chunk)：流式增量回调（剧本原始 JSON 文本逐段送达前端展示）。
        frames_mode：
        - fixed：严格按 frames 帧设计（frame_changes 共 frames-1 条）
        - auto：AI 自主设计帧数（≤ frames 上限），要求做完整连贯的动作分解、
          充分利用帧数（每个关键中间姿态占一帧），输出 frames_planned
        """
        ref_note = (
            "\n附带一张用户参考图：剧本必须保持该图的主体与风格，"
            "first_frame_prompt 应描述如何以参考图内容为起点。"
            if reference_png
            else ""
        )
        if frames_mode == "auto":
            task = f"""为一段无缝循环 GIF 制定剧本。用户意图：{prompt}{ref_note}
你将作为动画导演**自主设计帧数**（上限 {frames} 帧）：
1. 把动作完整分解为连贯的关键姿态序列——每个可辨识的中间姿态占一帧；
2. 帧数上限 {frames}，请充分利用（不要偷懒只用 2-3 帧），但也不低于 2 帧；
3. 确保末帧能平滑接回首帧形成循环。
输出 JSON（仅 JSON）：
{{"frames_planned": 你设计的实际帧数（整数，2 到 {frames}）,
 "first_frame_prompt": "第 1 帧完整画面描述（英文提示词，含主体/场景/风格/构图，150 词内）",
 "frame_changes": ["第 2 帧相对前一帧的微小变化（英文，一句话）", "...共 frames_planned - 1 条..."],
 "loop_closure": "最后一帧如何平滑回到第一帧画面（英文，一句话）",
 "style_anchor": "全片统一的风格描述（英文短语，如 flat illustration, pastel palette）"}}"""
        else:
            task = f"""为一段 {frames} 帧的无缝循环 GIF 制定剧本。用户意图：{prompt}{ref_note}
输出 JSON（仅 JSON）：
{{"first_frame_prompt": "第 1 帧完整画面描述（英文提示词，含主体/场景/风格/构图，150 词内）",
 "frame_changes": ["第 2 帧相对第 1 帧的微小变化（英文，一句话）", "...共 {max(0, frames - 1)} 条..."],
 "loop_closure": "最后一帧如何平滑回到第一帧画面（英文，一句话）",
 "style_anchor": "全片统一的风格描述（英文短语，如 flat illustration, pastel palette）"}}"""

        # 参考图同样压缩为读图尺寸（规划只需构图）
        images = (
            [_png_to_vision_data_url(reference_png)] if reference_png else None
        )
        if on_delta is not None:
            result = await self._client.chat_vision_stream(
                task, images_data_url=images, system=SYSTEM_PROMPT, on_delta=on_delta
            )
        else:
            result = await self._client.chat_vision(
                task, images_data_url=images, system=SYSTEM_PROMPT
            )
        parsed = parse_json_robust(result.content)
        script = _coerce_script(parsed, frames, frames_mode)
        if script is None:
            raise LLMError(f"剧本 JSON 解析失败：{result.content[:200]}")
        return script

    # ------------------------------------------------------------------
    # 逐帧决策（读图：首帧 + 上一帧）
    # ------------------------------------------------------------------

    async def decide_next(
        self,
        user_prompt: str,
        script: PlanScript,
        first_frame_png: bytes,
        last_frame_png: bytes,
        next_index: int,
        total_frames: int,
    ) -> FrameDecision:
        """看首帧与上一帧，决定下一帧提示词或停止。"""
        script_change = (
            script.frame_changes[next_index - 1]
            if next_index - 1 < len(script.frame_changes)
            else "continue the motion naturally"
        )
        task = f"""循环 GIF 共 {total_frames} 帧，即将生成第 {next_index} 帧。
用户意图：{user_prompt}
剧本中该帧的变化要点：{script_change}
风格锚：{script.style_anchor}
第 1 张图是首帧，第 2 张图是上一帧。
评估主体/风格/构图一致性后输出 JSON（仅 JSON）：
{{"action": "continue 或 stop",
 "next_frame_prompt": "下一帧生成提示词（英文：以上一帧为基础的最小变化，保持主体与风格完全一致，60 词内）",
 "reason": "决策理由（中文一句话）",
 "consistency": 0.0}}
consistency 是 0.0 到 1.0 的一致性分数。若动画已自然演完、继续生成只会重复或劣化，选 stop。"""

        images = [
            _png_to_vision_data_url(first_frame_png),
            _png_to_vision_data_url(last_frame_png),
        ]
        result = await self._client.chat_vision(
            task, images_data_url=images, system=SYSTEM_PROMPT
        )
        parsed = parse_json_robust(result.content)
        decision = _coerce_decision(parsed)
        if decision is None:
            # 降级：沿用剧本要点（调用方计独立失败计数）
            return FrameDecision(
                action="continue",
                next_frame_prompt=_fallback_prompt(script_change, script.style_anchor),
                reason="规划器 JSON 解析失败，降级沿用剧本",
                consistency=0.8,
                degraded=True,
            )
        return decision

    # ------------------------------------------------------------------
    # 首尾闭合检查
    # ------------------------------------------------------------------

    async def check_closure(
        self, user_prompt: str, first_frame_png: bytes, last_frame_png: bytes
    ) -> ClosureCheck:
        """对比首尾帧，判断循环是否闭合；未闭合给过渡帧提示词。"""
        task = f"""循环 GIF 首尾连贯性检查。用户意图：{user_prompt}
第 1 张图是首帧，第 2 张图是末帧。
输出 JSON（仅 JSON）：
{{"closed": true,
 "transition_prompt": "若未闭合：重生成末帧的提示词（英文：基于首帧画面，描述一个能平滑接回首帧的中间姿态，60 词内）；若已闭合则留空",
 "reason": "判断理由（中文一句话）"}}"""

        images = [
            _png_to_vision_data_url(first_frame_png),
            _png_to_vision_data_url(last_frame_png),
        ]
        result = await self._client.chat_vision(
            task, images_data_url=images, system=SYSTEM_PROMPT
        )
        parsed = parse_json_robust(result.content)
        if isinstance(parsed, dict):
            closed = parsed.get("closed")
            tp = parsed.get("transition_prompt")
            return ClosureCheck(
                closed=bool(closed) if isinstance(closed, bool) else True,
                transition_prompt=str(tp).strip() if isinstance(tp, str) else "",
                reason=str(parsed.get("reason") or ""),
            )
        # 解析失败：乐观视为闭合（避免无谓重生成）
        return ClosureCheck(closed=True, reason="闭合检查 JSON 解析失败，按闭合处理")


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------


def _fallback_prompt(change: str, style_anchor: str) -> str:
    """降级提示词：剧本要点 + 风格锚。"""
    return (
        f"{change}. Keep the subject, scene and style exactly the same as the "
        f"reference image. Style: {style_anchor}. Minimal motion change only."
    )


def _coerce_script(parsed: Any, frames: int, frames_mode: str = "fixed") -> PlanScript | None:
    """把解析结果整形为 PlanScript；结构不合法返回 None。"""
    if not isinstance(parsed, dict):
        return None
    first = parsed.get("first_frame_prompt")
    if not isinstance(first, str) or not first.strip():
        return None
    changes = parsed.get("frame_changes")
    if not isinstance(changes, list):
        changes = []
    changes = [
        str(c) for c in changes if isinstance(c, (str, int, float))
    ]
    frames_planned: int | None = None
    if frames_mode == "auto":
        # 自主帧数：取 AI 声明的帧数并钳制到 [2, frames]；缺省回退 changes+1
        raw_n = parsed.get("frames_planned")
        try:
            frames_planned = int(raw_n)
        except (TypeError, ValueError):
            frames_planned = None
        if frames_planned is None:
            frames_planned = min(frames, max(2, len(changes) + 1))
        frames_planned = max(2, min(frames, frames_planned))
        changes = changes[: frames_planned - 1]
    else:
        changes = changes[: max(0, frames - 1)]
    return PlanScript(
        first_frame_prompt=first.strip(),
        frame_changes=changes,
        loop_closure=str(parsed.get("loop_closure") or ""),
        style_anchor=str(parsed.get("style_anchor") or ""),
        frames_planned=frames_planned,
    )


def _coerce_decision(parsed: Any) -> FrameDecision | None:
    """把解析结果整形为 FrameDecision；结构不合法返回 None。"""
    if not isinstance(parsed, dict):
        return None
    action = parsed.get("action")
    if action not in ("continue", "stop"):
        return None
    nfp = parsed.get("next_frame_prompt")
    cons = parsed.get("consistency")
    try:
        consistency = max(0.0, min(1.0, float(cons))) if cons is not None else 0.8
    except (TypeError, ValueError):
        consistency = 0.8
    return FrameDecision(
        action=str(action),
        next_frame_prompt=nfp.strip() if isinstance(nfp, str) else "",
        reason=str(parsed.get("reason") or ""),
        consistency=consistency,
    )
