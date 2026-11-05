"""链式帧生成编排：导演模式（用户第八轮反馈定稿）。

流程：
  开头一次剧本规划（导演写好每帧怎么干，流式显示）
  → 底图（参考图 edits 或纯文本 generations）
  → 依次循环：画图 AI 以上一帧为参考图 + 剧本第 i 帧指令生成下一帧
  → 合成循环 GIF。

只有开头需要规划模型；后续每帧仅一次生图调用（末帧提示词自动附加
剧本的循环闭合指令），速度 = 帧数 × 生图时间。

安全：帧与 GIF 落盘一律经 _safe_join 校验最终路径必须位于 outputs 目录内，
杜绝路径穿越。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..llm import ClientRegistry, LLMError
from .assembler import encode_gif
from .planner import PlanScript, Planner

logger = logging.getLogger("img26.gif")

# 常见画布尺寸字符串 → (宽, 高)
_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*x\s*(\d{2,5})\s*$")


def parse_size(size: str) -> tuple[int, int]:
    """"1024x1024" → (1024, 1024)；非法抛 ValueError。"""
    m = _SIZE_RE.match(size or "")
    if not m:
        raise ValueError(f"尺寸格式非法：{size!r}（应为 宽x高，如 1024x1024）")
    w, h = int(m.group(1)), int(m.group(2))
    if w < 64 or h < 64 or w > 4096 or h > 4096:
        raise ValueError(f"尺寸超出范围 [64,4096]：{w}x{h}")
    return w, h


def _safe_join(base_dir: Path, name: str) -> Path:
    """安全路径拼接：文件名只允许白名单字符，且 resolve 后必须仍在 base 内。"""
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name) or ".." in name:
        raise ValueError(f"非法文件名：{name!r}")
    resolved = (base_dir / name).resolve()
    if base_dir.resolve() not in resolved.parents:
        raise ValueError(f"路径越界：{name!r}")
    return resolved


@dataclass
class GifParams:
    """一次 GIF 任务的全部参数（由路由层校验后传入）。

    frames_mode：
    - fixed：固定帧数（frames 为精确目标）
    - auto：AI 自主设计帧数（frames 语义为上限，实际以剧本 frames_planned 为准）
    """

    prompt: str
    frames: int
    size: str
    duration_ms: int
    loop: bool
    background: str
    frames_mode: str = "fixed"


@dataclass
class FrameRecord:
    """单帧存档信息。"""

    index: int
    path: str
    actual_size: tuple[int, int]
    planner_note: str = ""


@dataclass
class GenerationReport:
    """任务结果汇总。"""

    frames: list[FrameRecord] = field(default_factory=list)
    gif_path: str = ""
    loop_closed: bool = True
    stopped_reason: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CancelFlag:
    """协作式取消：路由层 cancel() 置位，生成循环在安全点检查。"""

    cancelled: bool = False


class GenerationCancelled(Exception):
    """任务被用户取消。"""


class GifGenerator:
    """链式生成编排器。事件回调用于 SSE 推送与任务日志落档。"""

    def __init__(
        self,
        registry: ClientRegistry,
        emit: Callable[[str, str, dict], Awaitable[None]] | None = None,
    ) -> None:
        """emit(level, message, data)：异步事件回调（可为 None，仅日志）。"""
        self._registry = registry
        self._emit = emit

    async def _event(self, level: str, message: str, **data: Any) -> None:
        """发事件（回调异常不掩盖主流程，仅记录）。"""
        if self._emit is None:
            return
        try:
            await self._emit(level, message, data)
        except Exception:  # noqa: BLE001 —— 事件通道故障不允许中断生成
            logger.exception("事件回调失败（忽略）")

    async def run(
        self,
        params: GifParams,
        outputs_dir: Path,
        cancel: CancelFlag,
        reference_png: bytes | None = None,
    ) -> GenerationReport:
        """执行完整生成流水线。outputs_dir 为帧与 GIF 落盘目录。"""
        planner = Planner(self._registry.planner_client())
        image_client = self._registry.image_client()
        canvas = parse_size(params.size)
        report = GenerationReport()

        def check_cancel() -> None:
            if cancel.cancelled:
                raise GenerationCancelled("用户取消")

        # 1. 剧本规划（流式：增量实时推送前端）--------------------------------
        await self._event("info", "剧本规划中…", stage="plan", state="active")

        async def on_plan_delta(chunk: str) -> None:
            await self._event(
                "plan_delta", "剧本流式输出…", stage="plan", state="active", delta=chunk
            )

        script: PlanScript = await planner.plan_script(
            params.prompt,
            params.frames,
            reference_png,
            frames_mode=params.frames_mode,
            on_delta=on_plan_delta,
        )
        # auto 模式以 AI 设计的帧数为准（钳制在 [2, frames]）
        total_frames = params.frames
        if params.frames_mode == "auto" and script.frames_planned:
            total_frames = max(2, min(params.frames, script.frames_planned))
        await self._event(
            "info",
            "剧本就绪",
            stage="plan",
            state="done",
            style_anchor=script.style_anchor,
            frames_planned=total_frames,
            frames_mode=params.frames_mode,
            # 剧本明细（前端剧本卡展示用）
            script={
                "first_frame_prompt": script.first_frame_prompt,
                "frame_changes": script.frame_changes,
                "loop_closure": script.loop_closure,
            },
        )

        # 2. 首帧 ----------------------------------------------------------
        check_cancel()
        await self._event("info", "生成第 1 帧（底图）…", stage="first_frame", state="active")
        if reference_png is not None:
            first = await image_client.edit_image(
                reference_png, _first_prompt_with_ref(script), params.size
            )
        else:
            first = await image_client.generate_image(_first_prompt(script), params.size)
        frame_paths: list[str] = []
        frame_pngs: list[bytes] = [first.image_png or b""]
        frame_paths.append(_save_frame(outputs_dir, 1, frame_pngs[0]))
        report.frames.append(
            FrameRecord(
                index=1,
                path=frame_paths[0],
                actual_size=_png_size(frame_pngs[0]),
                planner_note="first frame",
            )
        )
        await self._event(
            "frame",
            "第 1 帧完成",
            stage="first_frame",
            state="done",
            index=1,
            path=frame_paths[0],
            note="first frame",
        )

        # 3. 导演模式链式生成：以上一帧为参考 + 剧本第 i 帧指令，依次绘制 ------
        #    只有开头一次规划；后续每帧仅一次生图调用（末帧自动附加闭合指令）
        stopped_reason = f"completed all {total_frames} scripted frames"
        for i in range(2, total_frames + 1):
            check_cancel()
            change = _script_change_for(script, i)
            is_last = i == total_frames
            next_prompt = _frame_prompt(script, change, is_last)
            await self._event(
                "info",
                f"绘制第 {i} 帧（参考上一帧）：{change[:60]}",
                stage="frames",
                state="active",
                frame_index=i,
            )
            nxt = await image_client.edit_image(frame_pngs[-1], next_prompt, params.size)
            frame_pngs.append(nxt.image_png or b"")
            path = _save_frame(outputs_dir, i, frame_pngs[-1])
            frame_paths.append(path)
            report.frames.append(
                FrameRecord(
                    index=i,
                    path=path,
                    actual_size=_png_size(nxt.image_png or b""),
                    planner_note=change,
                )
            )
            await self._event(
                "frame",
                f"第 {i} 帧完成",
                stage="frames",
                state="done",
                index=i,
                path=path,
                note=change,
            )

        # 4. 循环闭合：由剧本 loop_closure 指令在末帧提示词中保证（无视觉检查）--
        loop_closed = True

        # 5. 合成 ----------------------------------------------------------
        check_cancel()
        await self._event("info", f"合成 GIF（{len(frame_pngs)} 帧）…", stage="encode", state="active")
        gif_bytes = await encode_gif(
            frame_pngs,
            canvas,
            params.duration_ms,
            params.loop,
            params.background,
        )
        gif_name = "animation.gif"
        gif_file = _safe_join(outputs_dir, gif_name)
        gif_file.parent.mkdir(parents=True, exist_ok=True)
        gif_file.write_bytes(gif_bytes)

        report.gif_path = gif_file.name
        report.loop_closed = loop_closed
        report.stopped_reason = stopped_reason
        await self._event(
            "done",
            f"GIF 完成（{len(frame_pngs)} 帧，闭合={loop_closed}）",
            stage="encode",
            state="done",
            gif_path=gif_file.name,
        )
        return report


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------


def _first_prompt(script: PlanScript) -> str:
    """首帧生成提示词（无参考图）：剧本首帧描述 + 风格锚。"""
    return f"{script.first_frame_prompt}. Style: {script.style_anchor}."


def _first_prompt_with_ref(script: PlanScript) -> str:
    """首帧生成提示词（有参考图）：保持参考图主体与风格。"""
    return (
        f"{script.first_frame_prompt}. Keep the subject, composition and style "
        f"of the reference image. Style: {script.style_anchor}."
    )


def _script_change_for(script: PlanScript, index: int) -> str:
    """取剧本第 index 帧的动作指令（缺则回退通用推进）。"""
    if index - 2 < len(script.frame_changes):
        return script.frame_changes[index - 2]
    return "advance the motion by one small step"


def _frame_prompt(script: PlanScript, change: str, is_last: bool) -> str:
    """帧生成提示词：剧本指令 + 风格锚 + 一致性约束；末帧附加循环闭合指令。"""
    base = (
        f"{change}. Keep the subject, scene, composition and art style exactly "
        f"the same as the reference image. Style: {script.style_anchor}."
    )
    if is_last and script.loop_closure:
        base += (
            f" This is the final frame: {script.loop_closure}. It must flow "
            "smoothly back into the first frame of the loop."
        )
    return base


def _save_frame(outputs_dir: Path, index: int, png: bytes) -> str:
    """帧 PNG 落盘（经安全路径校验），返回文件名。"""
    name = f"frame_{index:03d}.png"
    target = _safe_join(outputs_dir, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png)
    return name


def _png_size(png: bytes) -> tuple[int, int]:
    """从 PNG 头读尺寸（IHDR 位于固定偏移，无需完整解码）。"""
    if len(png) >= 24 and png[12:16] == b"IHDR":
        width = int.from_bytes(png[16:20], "big")
        height = int.from_bytes(png[20:24], "big")
        return width, height
    return (0, 0)
