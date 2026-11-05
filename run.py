"""启动入口：python run.py [--host 127.0.0.1] [--port 8266]

- 配置文件默认读取项目根 config.yaml（可用环境变量 IMG26_CONFIG 指定其他路径）；
- 未配置 provider 时服务仍可启动（SVG convert 可用，AI 功能报可读错误）；
- 默认仅绑定本机回环地址；开放局域网请先在 config.yaml 设置 api_token。
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from app import __version__
from app.config import ConfigError, load_config


class _TolerantConsoleStream:
    """UTF-8 容错控制台流：非法字符替换而非崩溃（Windows GBK 控制台防护）。"""

    def write(self, s: str) -> int:
        return sys.stderr.buffer.write(s.encode("utf-8", errors="replace"))

    def flush(self) -> None:
        sys.stderr.buffer.flush()


def setup_logging() -> None:
    """控制台 + 文件双通道日志。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(_TolerantConsoleStream()),
            logging.FileHandler("img26.log", encoding="utf-8"),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="GPT-Image-2.6 复刻服务（GIF + SVG）")
    parser.add_argument("--host", default=None, help="绑定地址（默认取配置/127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="端口（默认取配置/8266）")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    setup_logging()
    logging.getLogger("img26").info("版本 %s 启动中…", __version__)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    if host not in ("127.0.0.1", "localhost") and not cfg.server.api_token:
        logging.getLogger("img26").warning(
            "服务绑定到 %s（非回环地址）但未设置 api_token——"
            "任何同网段设备都可调用，建议配置鉴权",
            host,
        )

    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
