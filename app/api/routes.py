"""REST API 路由：/api/health 与 /api/v1/*。

要点：
- 可选 Bearer 鉴权（server.api_token 非空时启用；health 除外）；
- SVG 双模式：convert（上传/URL/base64 位图）与 generate（提示词→生图→转换）；
- GIF 异步任务：创建返回 202；SSE 进度流；协作式取消；
- 文件下载按 task_id 目录白名单校验，杜绝路径穿越。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from PIL import Image

from .. import __version__
from ..config import AppConfig
from ..gif.generator import (
    CancelFlag,
    GenerationCancelled,
    GifGenerator,
    GifParams,
    parse_size,
)
from ..imaging import (
    ImageFormatError,
    ImageTooLargeError,
    UnsafeUrlError,
    fetch_image_from_url,
    image_to_png_bytes,
    prepare_image,
)
from ..llm import ClientRegistry, LLMError
from ..svg import convert_to_svg, estimate_paths, sanitize_params
from ..tasks import TaskLimitError, TaskRegistry

logger = logging.getLogger("img26.api")

router = APIRouter()

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
ALLOWED_SIZES = ("512x512", "768x768", "1024x1024", "2048x2048")


# ----------------------------------------------------------------------
# 依赖注入与通用辅助
# ----------------------------------------------------------------------


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


async def require_auth(request: Request) -> None:
    """可选 Bearer 鉴权：api_token 非空时强制校验。"""
    cfg: AppConfig = request.app.state.config
    token = cfg.server.api_token
    if not token:
        return
    auth = request.headers.get("authorization") or ""
    if auth != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="缺少或错误的 Bearer Token")


async def _parse_body(
    request: Request, payload: str | None
) -> dict[str, Any]:
    """multipart 的 payload 表单域（JSON 字符串）或裸 JSON 请求体 → dict。"""
    if payload:
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="payload 不是合法 JSON") from exc
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001 —— 无 body / 非 JSON 统一报 400
        raise HTTPException(status_code=400, detail="请求体需为 JSON 或 multipart") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体需为 JSON 对象")
    return body


async def _resolve_image_bytes(
    cfg: AppConfig,
    *,
    upload: UploadFile | None = None,
    image_base64: str | None = None,
    image_url: str | None = None,
) -> bytes:
    """三种输入通道（multipart/base64/URL）统一为图片字节；通道冲突/全空报 400。"""
    channels = sum(1 for x in (upload, image_base64, image_url) if x)
    if channels != 1:
        raise HTTPException(
            status_code=400,
            detail="图片输入必须且只能提供一种：file / image_base64 / image_url",
        )
    if upload is not None:
        data = await upload.read()
    elif image_base64:
        try:
            b64 = image_base64.split(",", 1)[-1]  # 兼容 data URL 前缀
            data = base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="image_base64 解码失败") from exc
    else:
        try:
            data = await fetch_image_from_url(
                image_url or "",
                allow_private_address=cfg.svg.allow_private_address,
                timeout_seconds=cfg.svg.request_timeout_seconds,
                max_bytes=cfg.svg.max_upload_bytes,
            )
        except UnsafeUrlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ImageTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
    if len(data) > cfg.svg.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"图片体积 {len(data)} 超过上限 {cfg.svg.max_upload_bytes}",
        )
    return data


def _prepare_or_400(cfg: AppConfig, data: bytes) -> Image.Image:
    """预处理 + 错误转 HTTP；返回处理后的 PIL 图像。"""
    try:
        prepared = prepare_image(
            data,
            max_pixels=cfg.svg.max_pixels,
            downscale_long_side=cfg.svg.downscale_long_side,
            max_bytes=cfg.svg.max_upload_bytes,
        )
    except ImageFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ImageTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return prepared.image


async def _resolve_reference_png(
    cfg: AppConfig,
    *,
    upload: UploadFile | None,
    body: dict[str, Any],
    base64_key: str = "reference_image_base64",
    url_key: str = "reference_image_url",
) -> bytes | None:
    """解析可选参考图 → 统一 PNG 字节；无参考图返回 None。"""
    if upload is not None:
        data = await upload.read()
    elif body.get(base64_key) or body.get(url_key):
        data = await _resolve_image_bytes(
            cfg, image_base64=body.get(base64_key), image_url=body.get(url_key)
        )
    else:
        return None
    return image_to_png_bytes(_prepare_or_400(cfg, data))


# ----------------------------------------------------------------------
# 基础端点
# ----------------------------------------------------------------------


@router.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    cfg: AppConfig = request.app.state.config
    return {
        "status": "ok",
        "version": __version__,
        "image_provider_ready": cfg.find_image_provider() is not None,
        "planner_provider_ready": cfg.find_planner_provider() is not None,
    }


@router.get("/api/v1/config")
async def config_view(request: Request, _: None = Depends(require_auth)) -> dict[str, Any]:
    cfg: AppConfig = request.app.state.config
    return cfg.masked_view()


@router.get("/api/v1/presets")
async def presets_list(_: None = Depends(require_auth)) -> dict[str, Any]:
    """国内知名平台一键配置预设（Agnes 已实测；百炼/火山未实测，见 notes）。"""
    from ..presets import presets_view

    return {"presets": presets_view()}


@router.get("/api/v1/keys")
async def keys_view(request: Request, _: None = Depends(require_auth)) -> dict[str, Any]:
    registry: ClientRegistry = request.app.state.registry
    return {"pools": registry.pools_snapshot()}


@router.post("/api/v1/keys/reset")
async def keys_reset(request: Request, _: None = Depends(require_auth)) -> dict[str, Any]:
    """强制终止冷却：立即清零全部 Key 的冷却/故障/失败计数（用户显式操作）。"""
    registry: ClientRegistry = request.app.state.registry
    total = 0
    pools = 0
    for client in registry.all_clients():
        total += client.pool.reset_all()
        pools += 1
    return {"ok": True, "reset_keys": total, "pools": pools}


# ----------------------------------------------------------------------
# 网页端 API 配置（settings）：掩码回显 / 校验写盘 / 热重建 Key 池
# ----------------------------------------------------------------------

_settings_lock = asyncio.Lock()


@router.get("/api/v1/settings")
async def settings_view(request: Request, _: None = Depends(require_auth)) -> dict[str, Any]:
    """providers 配置回显（Key 一律掩码；前端保存时掩码原样提交即表示保留原 Key）。"""
    cfg: AppConfig = request.app.state.config
    return {
        "config_file": str(_config_path()),
        "providers": [
            {
                "name": p.name,
                "base_url": p.base_url,
                "api_keys": p.masked_keys(),  # 掩码
                "image_model": p.image_model,
                "planner_model": p.planner_model,
                "image_provider": p.image_provider,
                "planner_provider": p.planner_provider,
                "edit_style": p.edit_style,
            }
            for p in cfg.providers
        ],
    }


def _config_path() -> Path:
    import os

    return Path(os.environ.get("IMG26_CONFIG") or Path.cwd() / "config.yaml")


def _restore_masked_keys(
    submitted: list, stored: "ProviderConfig | None"
) -> list[str]:
    """把提交里的掩码值回填为已存真实 Key。

    规则：提交值与该 provider 已存 Key 的掩码一致 → 保留真实值；
    否则视为用户新输入的明文 Key。无同名 provider 时不回填。
    """
    if stored is None:
        return [str(k).strip() for k in submitted if str(k).strip()]
    masked = stored.masked_keys()
    out: list[str] = []
    for k in submitted:
        k = str(k).strip()
        if not k:
            continue
        out.append(next((real for m, real in zip(masked, stored.api_keys) if k == m), k))
    return out


@router.post("/api/v1/settings")
async def settings_save(request: Request, _: None = Depends(require_auth)) -> dict[str, Any]:
    """保存网页端 providers 配置：校验 → 掩码回填 → 写 config.yaml → 热重建客户端。

    server 段（host/port/api_token）不开放网页修改（防止经 API 关闭鉴权）。
    """
    cfg: AppConfig = request.app.state.config
    body = await _parse_body(request, None)
    raw_providers = body.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        raise HTTPException(status_code=400, detail="providers 必须是非空列表")

    async with _settings_lock:
        # 掩码回填（以当前内存中的 providers 为准）
        by_name = {p.name: p for p in cfg.providers}
        parsed: list[ProviderConfig] = []
        try:
            for i, raw in enumerate(raw_providers):
                if not isinstance(raw, dict):
                    raise HTTPException(400, f"providers[{i}] 必须是对象")
                keys = raw.get("api_keys") or []
                if not isinstance(keys, list):
                    raise HTTPException(400, f"providers[{i}].api_keys 必须是列表")
                raw = dict(raw)
                raw["api_keys"] = _restore_masked_keys(keys, by_name.get(raw.get("name")))
                parsed.append(_parse_provider_public(raw, i))
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 —— 校验异常统一转 400
            raise HTTPException(status_code=400, detail=f"配置校验失败：{exc}") from exc

        if parsed and all(
            not (p.image_provider and p.image_model) for p in parsed
        ):
            raise HTTPException(
                status_code=400,
                detail="至少一个 provider 需配置 image_model 且 image_provider 为 true",
            )

        # 写盘（失败则内存不变，保证一致性）
        try:
            from ..config import save_providers_to_yaml

            save_providers_to_yaml(cfg, parsed, _config_path())
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"config.yaml 写入失败：{exc}") from exc

        # 热更新：内存配置 + 重建客户端注册表（Key 池状态归零重开）
        cfg.providers = parsed
        request.app.state.registry = ClientRegistry(cfg)

    return {
        "ok": True,
        "config_file": str(_config_path()),
        "providers": [
            {
                "name": p.name,
                "base_url": p.base_url,
                "api_keys": p.masked_keys(),
                "image_model": p.image_model,
                "planner_model": p.planner_model,
                "image_provider": p.image_provider,
                "planner_provider": p.planner_provider,
                "edit_style": p.edit_style,
            }
            for p in cfg.providers
        ],
    }


def _parse_provider_public(raw: dict, index: int):
    """复用 config._parse_provider 的公开包装（校验与错误信息一致）。"""
    from ..config import _parse_provider

    return _parse_provider(raw, index)


@router.get("/api/v1/tasks/stats")
async def task_stats(request: Request, _: None = Depends(require_auth)) -> dict[str, Any]:
    tasks: TaskRegistry = request.app.state.tasks
    return tasks.stats()


# ----------------------------------------------------------------------
# SVG 端点（convert / generate 双模式）
# ----------------------------------------------------------------------


@router.post("/api/v1/svg")
async def svg_endpoint(
    request: Request,
    _: None = Depends(require_auth),
    file: UploadFile | None = File(default=None),
    payload: str | None = Form(default=None),
):
    """位图→SVG，或提示词→生图→SVG（全链路在工作台内完成）。

    - multipart/form-data：convert 模式 file=<位图>；generate 模式
      file=<参考图>（可选）+ payload=<参数 JSON>（{"mode":"generate","prompt":...}）
    - application/json：{"mode","prompt","image_base64","image_url",
                        "reference_image_base64","reference_image_url","size","params"}
    """
    cfg: AppConfig = request.app.state.config
    registry: ClientRegistry = request.app.state.registry
    body = await _parse_body(request, payload)
    _t0 = asyncio.get_event_loop().time()

    mode = body.get("mode") or (
        "convert"
        if (file is not None or body.get("image_base64") or body.get("image_url"))
        else None
    )
    if mode not in ("convert", "generate"):
        raise HTTPException(
            status_code=400,
            detail="mode 必须是 convert 或 generate，且需提供对应输入",
        )

    params = sanitize_params(body.get("params"), cfg.svg.defaults)

    if mode == "convert":
        data = await _resolve_image_bytes(
            cfg,
            upload=file,
            image_base64=body.get("image_base64"),
            image_url=body.get("image_url"),
        )
        img = _prepare_or_400(cfg, data)
        original_size = img.size
    else:
        # generate 模式：先经接入 API 生图（可选参考图），再矢量化
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="generate 模式必须提供 prompt")
        size = str(body.get("size") or "1024x1024")
        try:
            parse_size(size)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if cfg.find_image_provider() is None:
            raise HTTPException(
                status_code=503,
                detail="config.yaml 未配置生图 provider，generate 模式不可用",
            )
        ref_png = await _resolve_reference_png(cfg, upload=file, body=body)
        try:
            client = registry.image_client()
            if ref_png is not None:
                gen = await client.edit_image(ref_png, prompt, size)
            else:
                gen = await client.generate_image(prompt, size)
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=f"生图失败：{exc}") from exc
        if not gen.image_png:
            raise HTTPException(status_code=502, detail="生图端点未返回图像数据")
        img = _prepare_or_400(cfg, gen.image_png)
        original_size = img.size

    svg_text = await convert_to_svg(img, params)
    elapsed_ms = round((asyncio.get_event_loop().time() - _t0) * 1000)
    meta = {
        "svg_bytes": len(svg_text.encode("utf-8")),
        "paths": estimate_paths(svg_text),
        "original_size": list(original_size),
        "output_size": list(img.size),
        "downscaled": img.size != original_size,
        "mode": mode,
        "elapsed_ms": elapsed_ms,
        "oversized_warning": (
            f"SVG 体积 {len(svg_text.encode('utf-8'))} 超过 {cfg.svg.max_svg_bytes} 字节，"
            "建议调大 filter_speckle 或改用 binary 色彩模式"
            if len(svg_text.encode("utf-8")) > cfg.svg.max_svg_bytes
            else None
        ),
    }
    # generate 模式附带中间位图（AI 生成原图），前端可一键导出 PNG
    png_b64 = (
        base64.b64encode(gen.image_png).decode("ascii") if mode == "generate" and gen.image_png else None
    )
    return JSONResponse(content={"ok": True, "meta": meta, "svg": svg_text, "png_base64": png_b64})


# ----------------------------------------------------------------------
# GIF 端点（异步任务）
# ----------------------------------------------------------------------


@router.post("/api/v1/gif")
async def create_gif_task(
    request: Request,
    _: None = Depends(require_auth),
    file: UploadFile | None = File(default=None),
    payload: str | None = Form(default=None),
):
    """创建 GIF 生成任务，返回 202 + task_id。

    JSON 字段：prompt(必填)、frames、size、duration_ms、loop、background、
              reference_image_base64 / reference_image_url；
    multipart：file=参考图（可选）+ payload=参数 JSON。
    """
    cfg: AppConfig = request.app.state.config
    registry: ClientRegistry = request.app.state.registry
    tasks: TaskRegistry = request.app.state.tasks

    if cfg.find_image_provider() is None or cfg.find_planner_provider() is None:
        raise HTTPException(
            status_code=503,
            detail="config.yaml 未配置生图/规划 provider，GIF 功能不可用",
        )

    body = await _parse_body(request, payload)

    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 必填")
    frames_mode = str(body.get("frames_mode") or "fixed")
    if frames_mode not in ("fixed", "auto"):
        raise HTTPException(status_code=400, detail="frames_mode 只支持 fixed 或 auto")
    frames_raw = body.get("frames")
    if frames_raw is None:
        # auto 模式未传帧数时用上限（AI 自主设计），fixed 用默认
        frames = cfg.gif.max_frames if frames_mode == "auto" else cfg.gif.default_frames
    else:
        frames = int(frames_raw)
    if frames < 2 or frames > cfg.gif.max_frames:
        raise HTTPException(
            status_code=400, detail=f"frames 需在 [2, {cfg.gif.max_frames}] 内"
        )
    size = str(body.get("size") or cfg.gif.default_size)
    if size not in ALLOWED_SIZES:
        raise HTTPException(status_code=400, detail=f"size 仅支持 {list(ALLOWED_SIZES)}")
    duration_ms = int(body.get("duration_ms") or cfg.gif.frame_interval_ms)
    if not 20 <= duration_ms <= 5000:
        raise HTTPException(status_code=400, detail="duration_ms 需在 [20, 5000] 内")
    loop = bool(body.get("loop", cfg.gif.loop_forever))
    background = str(body.get("background") or cfg.gif.background)

    ref_png = await _resolve_reference_png(cfg, upload=file, body=body)

    try:
        task = tasks.create(
            prompt,
            {
                "frames": frames,
                "frames_mode": frames_mode,
                "size": size,
                "duration_ms": duration_ms,
                "loop": loop,
            },
        )
    except TaskLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    # 后台执行（任务产物目录隔离：outputs/tasks/{task_id}/）
    cancel_flag = CancelFlag()
    task.cancel_flag = cancel_flag
    outputs_dir = cfg.outputs_dir / "tasks" / task.id

    async def emit(level: str, message: str, data: dict) -> None:
        """任务事件：写注册表（SSE 广播 + 历史留档）。"""
        tasks.emit(task, level, message, data)

    async def runner() -> None:
        tasks.mark_running(task)
        try:
            gen = GifGenerator(registry, emit=emit)
            report = await gen.run(
                GifParams(
                    prompt=prompt,
                    frames=frames,
                    size=size,
                    duration_ms=duration_ms,
                    loop=loop,
                    background=background,
                    frames_mode=frames_mode,
                ),
                outputs_dir,
                cancel_flag,
                reference_png=ref_png,
            )
            # 帧元信息以 report 为准（含规划备注与实际尺寸）
            task.frames = [
                {
                    "index": f.index,
                    "file": f.path,
                    "actual_size": list(f.actual_size),
                    "note": f.planner_note,
                }
                for f in report.frames
            ]
            tasks.finish(
                task,
                "done",
                result={
                    "gif": f"/api/v1/files/{task.id}/{report.gif_path}",
                    "frame_files": [
                        f"/api/v1/files/{task.id}/{f.path}" for f in report.frames
                    ],
                    "loop_closed": report.loop_closed,
                    "stopped_reason": report.stopped_reason,
                    "frames_generated": len(report.frames),
                },
            )
        except GenerationCancelled:
            tasks.finish(task, "cancelled", error="用户取消")
        except LLMError as exc:
            tasks.emit(task, "error", f"任务失败：{exc}")
            tasks.finish(task, "failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 —— 兜底不吞错：未预期异常也落任务记录
            logger.exception("GIF 任务异常终止")
            tasks.emit(task, "error", f"任务异常：{exc}")
            tasks.finish(task, "failed", error=f"{type(exc).__name__}: {exc}")

    asyncio.create_task(runner())
    return JSONResponse(
        status_code=202, content={"task_id": task.id, "status": task.status}
    )


@router.get("/api/v1/tasks/{task_id}")
async def task_detail(
    task_id: str, request: Request, _: None = Depends(require_auth)
) -> dict[str, Any]:
    tasks: TaskRegistry = request.app.state.tasks
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期清理")
    return task.public_view()


@router.get("/api/v1/tasks/{task_id}/events")
async def task_events(
    task_id: str, request: Request, _: None = Depends(require_auth)
) -> StreamingResponse:
    """SSE 进度流；断线后前端应回退轮询 /tasks/{id}。"""
    tasks: TaskRegistry = request.app.state.tasks
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期清理")
    queue = tasks.subscribe(task)

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                payload = json.dumps(evt.to_dict(), ensure_ascii=False)
                yield f"data: {payload}\n\n".encode("utf-8")
                if task.status in ("done", "failed", "cancelled"):
                    break
        finally:
            tasks.unsubscribe(task, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/v1/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, request: Request, _: None = Depends(require_auth)
) -> dict[str, Any]:
    tasks: TaskRegistry = request.app.state.tasks
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期清理")
    if task.status in ("done", "failed", "cancelled"):
        return {"task_id": task_id, "status": task.status, "cancelled_now": False}
    if task.cancel_flag is not None:
        task.cancel_flag.cancelled = True
    return {"task_id": task_id, "status": "cancelling", "cancelled_now": True}


@router.get("/api/v1/files/{task_id}/{name}")
async def download_file(
    task_id: str,
    name: str,
    request: Request,
    _: None = Depends(require_auth),
) -> FileResponse:
    """任务产物下载：task_id 必须存在，name 白名单校验，路径限制在任务目录内。"""
    cfg: AppConfig = request.app.state.config
    tasks: TaskRegistry = request.app.state.tasks
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期清理")
    if not _SAFE_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="非法文件名")
    base = (cfg.outputs_dir / "tasks" / task_id).resolve()
    target = (base / name).resolve()
    if base not in target.parents:
        raise HTTPException(status_code=400, detail="路径越界")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    media = "image/gif" if name.endswith(".gif") else "image/png"
    return FileResponse(target, media_type=media, filename=name)
