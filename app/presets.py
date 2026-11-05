"""国内知名平台一键配置预设（单一真源，前端经 /api/v1/presets 拉取）。

预设值基于各平台官方公开文档整理；除 Agnes 已实测外均标注「未实测」，
接入后请跑 scripts/probe_endpoint.py 验证。模型名可能随平台更新，
预设只是起点，用户可在表单中修改。
"""

from __future__ import annotations

from typing import Any

PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "agnes": {
        "label": "Agnes（已实测）",
        "base_url": "https://api.agnes-ai.cn/v1",
        "planner_model": "agnes-2.5-flash",
        "image_model": "agnes-image-2.1-flash",
        "edit_style": "generations_extra_body",
        "notes": "官方文档确认：图生图走 generations + extra_body.image（Data URI）；"
        "免费档有 rpm 限流，多 Key 轮询可缓解。",
    },
    "dashscope": {
        "label": "阿里云百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "planner_model": "qwen-vl-max-latest",
        "image_model": "wanx2.1-t2i-turbo",
        "edit_style": "edits_multipart",
        "notes": "读图规划（qwen-vl）走 compatible-mode 可用；万相生图传统上走 "
        "DashScope 原生异步接口，经本项目的 OpenAI 兼容调用可能失败（未实测），"
        "请跑探针确认；不可用时生图功能需换支持兼容层的端点。",
    },
    "volcengine": {
        "label": "火山引擎方舟（豆包）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "planner_model": "doubao-seed-1.6-vision-250615",
        "image_model": "doubao-seedream-4-0-250828",
        "edit_style": "generations_image_field",
        "notes": "图生图在 images/generations 顶层传 image 数组（Data URI）+ 顶层 "
        "response_format（未实测）。模型名可换为你控制台的推理接入点 ID（ep-xxx）。"
        "seedream 3.0 是纯文生图，勿用于参考图模式。",
    },
}


def presets_view() -> list[dict[str, Any]]:
    """对外预设视图（id + 全部预设字段）。"""
    out = []
    for pid, p in PROVIDER_PRESETS.items():
        out.append({"id": pid, **p})
    return out
