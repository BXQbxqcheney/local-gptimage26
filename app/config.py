"""配置加载模块：读取 config.yaml，构建带校验的运行时配置对象。

设计要点：
- 所有 API Key 只能来自 config.yaml / 环境变量，绝不硬编码；
- 配置缺失给出可读的中文错误（指明缺什么、怎么补）；
- 提供 provider / 模型掩码视图，供 /api/v1/config 脱敏展示。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """配置错误：携带用户可读的中文说明。"""


# 常见粘贴错误的自动清洗规则
_KEY_STRIP_CHARS = " \t\r\n[]'\","
_URL_JUNK_SUFFIXES = (
    "/chat/completions",
    "/images/generations",
    "/images/edits",
    "/images/variations",
    "/completions",
    "/embeddings",
    "/models",
    "/responses",
)


def normalize_base_url(raw: str) -> str:
    """清洗 base_url：剥掉误填的接口路径后缀（用户常把完整端点粘进来）。"""
    url = (raw or "").strip().rstrip("/")
    changed = True
    while changed and url:
        changed = False
        low = url.lower()
        for suffix in _URL_JUNK_SUFFIXES:
            if low.endswith(suffix):
                url = url[: -len(suffix)].rstrip("/")
                changed = True
                break
    return url.rstrip("/")


def normalize_api_key(raw: str) -> str:
    """清洗 API Key：剥掉从列表/文档复制时带上的括号引号逗号等粘贴残留。"""
    k = (raw or "").strip()
    # 只剥首尾的粘贴残留字符（不碰 Key 本体）
    return k.strip(_KEY_STRIP_CHARS).strip()


@dataclass
class ProviderConfig:
    """一个 OpenAI 兼容端点（可同时或分别承担生图/规划角色）。"""

    name: str
    base_url: str
    api_keys: list[str]
    image_model: str = ""
    planner_model: str = ""
    image_provider: bool = True
    planner_provider: bool = True
    # 参考图编辑（图生图）的接口风格：
    #   edits_multipart      — OpenAI 官方式：POST /images/edits（multipart 上传参考图）
    #   generations_extra_body — Agnes 式：POST /images/generations，参考图放 extra_body.image（Data URI）
    edit_style: str = "edits_multipart"

    def masked_keys(self) -> list[str]:
        """Key 脱敏：仅保留前 4 后 4 字符。"""
        out = []
        for k in self.api_keys:
            if len(k) <= 8:
                out.append("*" * len(k))
            else:
                out.append(f"{k[:4]}...{k[-4:]}")
        return out


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8266
    api_token: str = ""
    cors_origins: list[str] = field(default_factory=lambda: ["*"])


@dataclass
class GifConfig:
    default_frames: int = 6
    max_frames: int = 12
    default_size: str = "1024x1024"
    frame_interval_ms: int = 200
    loop_forever: bool = True
    background: str = "#FFFFFF"
    max_concurrent_tasks: int = 3
    task_ttl_seconds: int = 3600


@dataclass
class SvgDefaults:
    colormode: str = "color"
    hierarchical: str = "stacked"
    mode: str = "spline"
    filter_speckle: int = 4
    color_precision: int = 6
    layer_difference: int = 16
    corner_threshold: int = 60
    length_threshold: float = 3.5
    splice_threshold: int = 45
    path_precision: int = 3


@dataclass
class SvgConfig:
    max_upload_bytes: int = 20 * 1024 * 1024
    max_pixels: int = 10_000_000
    downscale_long_side: int = 4096
    max_svg_bytes: int = 8 * 1024 * 1024
    allow_private_address: bool = False
    request_timeout_seconds: int = 15
    defaults: SvgDefaults = field(default_factory=SvgDefaults)


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    providers: list[ProviderConfig] = field(default_factory=list)
    gif: GifConfig = field(default_factory=GifConfig)
    svg: SvgConfig = field(default_factory=SvgConfig)

    # 运行时注入，不来自 YAML
    outputs_dir: Path = Path("outputs")

    def find_image_provider(self) -> ProviderConfig | None:
        """返回第一个可充当生图角色的 provider。"""
        for p in self.providers:
            if p.image_provider and p.image_model:
                return p
        return None

    def find_planner_provider(self) -> ProviderConfig | None:
        """返回第一个可充当规划角色的 provider。"""
        for p in self.providers:
            if p.planner_provider and p.planner_model:
                return p
        return None

    def masked_view(self) -> dict[str, Any]:
        """脱敏配置视图（对外 API 用）。"""
        return {
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "auth_enabled": bool(self.server.api_token),
                "cors_origins": self.server.cors_origins,
            },
            "providers": [
                {
                    "name": p.name,
                    "base_url": p.base_url,
                    "image_model": p.image_model or None,
                    "planner_model": p.planner_model or None,
                    "image_provider": p.image_provider,
                    "planner_provider": p.planner_provider,
                    "edit_style": p.edit_style,
                    "api_keys": p.masked_keys(),
                }
                for p in self.providers
            ],
            "gif": {
                "default_frames": self.gif.default_frames,
                "max_frames": self.gif.max_frames,
                "default_size": self.gif.default_size,
                "frame_interval_ms": self.gif.frame_interval_ms,
                "loop_forever": self.gif.loop_forever,
                "max_concurrent_tasks": self.gif.max_concurrent_tasks,
            },
            "svg": {
                "max_upload_bytes": self.svg.max_upload_bytes,
                "downscale_long_side": self.svg.downscale_long_side,
                "defaults": self.svg.defaults.__dict__.copy(),
            },
        }


def _require_str(value: Any, field_name: str, ctx: str) -> str:
    """校验必填字符串字段，非法即抛 ConfigError。"""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"配置错误：{ctx} 中的 {field_name} 必须是非空字符串")
    return value.strip()


def _parse_provider(raw: dict[str, Any], index: int) -> ProviderConfig:
    """解析并校验单个 provider 配置块。"""
    ctx = f"providers[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"配置错误：{ctx} 必须是映射（键值对）")
    name = _require_str(raw.get("name"), "name", ctx)
    base_url = normalize_base_url(_require_str(raw.get("base_url"), "base_url", ctx))
    keys_raw = raw.get("api_keys") or []
    if not isinstance(keys_raw, list) or not keys_raw:
        raise ConfigError(f"配置错误：{ctx} 的 api_keys 必须是非空列表（至少 1 个 Key）")
    keys = []
    for i, k in enumerate(keys_raw):
        if not isinstance(k, str) or not normalize_api_key(k):
            raise ConfigError(f"配置错误：{ctx}.api_keys[{i}] 必须是非空字符串")
        keys.append(normalize_api_key(k))
    return ProviderConfig(
        name=name,
        base_url=base_url,
        api_keys=keys,
        image_model=str(raw.get("image_model") or "").strip(),
        planner_model=str(raw.get("planner_model") or "").strip(),
        image_provider=bool(raw.get("image_provider", True)),
        planner_provider=bool(raw.get("planner_provider", True)),
        edit_style=_parse_edit_style(raw.get("edit_style"), ctx),
    )


def _parse_edit_style(raw: Any, ctx: str) -> str:
    """edit_style 白名单校验，非法回默认（兼容旧配置文件无此字段）。

    - edits_multipart：OpenAI 官方式（/images/edits multipart）
    - generations_extra_body：Agnes 式（generations + extra_body.image）
    - generations_image_field：火山方舟式（generations 顶层 image 数组 + 顶层 response_format）
    """
    if raw is None:
        return "edits_multipart"
    val = str(raw).strip()
    if val in ("edits_multipart", "generations_extra_body", "generations_image_field"):
        return val
    raise ConfigError(
        f"配置错误：{ctx}.edit_style 只支持 edits_multipart / generations_extra_body / "
        f"generations_image_field，当前值 {raw!r}"
    )


def _coerce_int(raw: dict[str, Any], key: str, default: int, lo: int, hi: int, ctx: str) -> int:
    """读取整型配置项，越界/非法回退默认值并允许后续警告。"""
    val = raw.get(key, default)
    try:
        iv = int(val)
    except (TypeError, ValueError):
        raise ConfigError(f"配置错误：{ctx}.{key} 必须是整数，当前值 {val!r}")
    if iv < lo or iv > hi:
        raise ConfigError(f"配置错误：{ctx}.{key} 超出允许范围 [{lo}, {hi}]，当前值 {iv}")
    return iv


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载并校验配置文件。

    优先级：显式 path 参数 > 环境变量 IMG26_CONFIG > 项目根 config.yaml。
    文件不存在时返回全默认配置（无 provider），由调用方决定是否警告。
    """
    if path is None:
        path = os.environ.get("IMG26_CONFIG") or Path.cwd() / "config.yaml"
    cfg_path = Path(path)

    data: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置错误：{cfg_path} 不是合法 YAML：{exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"配置错误：{cfg_path} 顶层必须是映射（键值对）")
    # 文件不存在 → 空配置继续，允许服务以“未配置 AI”状态启动（仅 SVG 本地转换可用）

    cfg = AppConfig()

    # --- server ---
    srv = data.get("server") or {}
    if not isinstance(srv, dict):
        raise ConfigError("配置错误：server 必须是映射（键值对）")
    cfg.server = ServerConfig(
        host=str(srv.get("host") or "127.0.0.1"),
        port=_coerce_int(srv, "port", 8266, 1, 65535, "server"),
        api_token=str(srv.get("api_token") or "").strip(),
        cors_origins=list(srv.get("cors_origins") or ["*"]),
    )

    # --- providers ---
    provs_raw = data.get("providers") or []
    if not isinstance(provs_raw, list):
        raise ConfigError("配置错误：providers 必须是列表")
    cfg.providers = [_parse_provider(p, i) for i, p in enumerate(provs_raw)]
    if cfg.providers:
        if cfg.find_image_provider() is None:
            raise ConfigError(
                "配置错误：没有任何 provider 可承担生图角色——"
                "请为至少一个 provider 配置 image_model 且 image_provider: true"
            )
        if cfg.find_planner_provider() is None:
            raise ConfigError(
                "配置错误：没有任何 provider 可承担视觉规划角色——"
                "请为至少一个 provider 配置 planner_model（需支持读图）且 planner_provider: true"
            )

    # --- gif ---
    gif_raw = data.get("gif") or {}
    if not isinstance(gif_raw, dict):
        raise ConfigError("配置错误：gif 必须是映射（键值对）")
    cfg.gif = GifConfig(
        default_frames=_coerce_int(gif_raw, "default_frames", 6, 2, 60, "gif"),
        max_frames=_coerce_int(gif_raw, "max_frames", 12, 2, 60, "gif"),
        default_size=str(gif_raw.get("default_size") or "1024x1024"),
        frame_interval_ms=_coerce_int(gif_raw, "frame_interval_ms", 200, 20, 5000, "gif"),
        loop_forever=bool(gif_raw.get("loop_forever", True)),
        background=str(gif_raw.get("background") or "#FFFFFF"),
        max_concurrent_tasks=_coerce_int(gif_raw, "max_concurrent_tasks", 3, 1, 32, "gif"),
        task_ttl_seconds=_coerce_int(gif_raw, "task_ttl_seconds", 3600, 60, 86400, "gif"),
    )

    # --- svg ---
    svg_raw = data.get("svg") or {}
    if not isinstance(svg_raw, dict):
        raise ConfigError("配置错误：svg 必须是映射（键值对）")
    defaults_raw = svg_raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise ConfigError("配置错误：svg.defaults 必须是映射（键值对）")
    d = SvgDefaults()
    for k in d.__dict__:
        if k in defaults_raw:
            setattr(d, k, defaults_raw[k])
    cfg.svg = SvgConfig(
        max_upload_bytes=_coerce_int(svg_raw, "max_upload_bytes", 20 * 1024 * 1024, 1024, 200 * 1024 * 1024, "svg"),
        max_pixels=_coerce_int(svg_raw, "max_pixels", 10_000_000, 1000, 100_000_000, "svg"),
        downscale_long_side=_coerce_int(svg_raw, "downscale_long_side", 4096, 256, 16384, "svg"),
        max_svg_bytes=_coerce_int(svg_raw, "max_svg_bytes", 8 * 1024 * 1024, 1024, 512 * 1024 * 1024, "svg"),
        allow_private_address=bool(svg_raw.get("allow_private_address", False)),
        request_timeout_seconds=_coerce_int(svg_raw, "request_timeout_seconds", 15, 1, 300, "svg"),
        defaults=d,
    )

    cfg.outputs_dir = Path.cwd() / "outputs"
    return cfg


def save_providers_to_yaml(
    cfg: AppConfig, providers: list[ProviderConfig], path: str | Path | None = None
) -> Path:
    """把网页端编辑的 providers 写回 config.yaml（保留 server/gif/svg 段落值）。

    注意：yaml 重写会丢失原文件注释（已在 README/API 文档注明）。
    返回写入的文件路径。
    """
    cfg_path = Path(path) if path else (
        Path(os.environ.get("IMG26_CONFIG") or Path.cwd() / "config.yaml")
    )
    existing: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            existing = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            existing = {}  # 原文件损坏时仅保留内存中的段落值重写
        if not isinstance(existing, dict):
            existing = {}

    existing["providers"] = [
        {
            "name": p.name,
            "base_url": p.base_url,
            "api_keys": p.api_keys,
            "image_model": p.image_model,
            "planner_model": p.planner_model,
            "image_provider": p.image_provider,
            "planner_provider": p.planner_provider,
            "edit_style": p.edit_style,
        }
        for p in providers
    ]

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    # 排序保持键序、中文不转义
    text = yaml.safe_dump(existing, allow_unicode=True, sort_keys=False, default_flow_style=False)
    cfg_path.write_text(text, encoding="utf-8")
    return cfg_path
