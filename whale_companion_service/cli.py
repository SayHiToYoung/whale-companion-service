"""Command-line entry point for the standalone companion service."""
from __future__ import annotations

import argparse
import signal
import time
from dataclasses import replace
from pathlib import Path

from .companion_llm import ModelCompanionResponder
from .configuration import ConfigurationError, ServiceConfig
from .memory_server import MemoryApiServer, MemoryRepository


def configured_responder(config: ServiceConfig) -> ModelCompanionResponder | None:
    """Build the responder exclusively from this service's configuration."""
    if not config.llm_enabled:
        return None
    responder = ModelCompanionResponder(config.provider)
    return responder if responder.available else None


def build_server(config: ServiceConfig) -> MemoryApiServer:
    responder = configured_responder(config)
    return MemoryApiServer(
        MemoryRepository(config.database, responder=responder),
        token=config.token,
        host=config.host,
        port=config.port,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立 AI 陪伴服务")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args(argv)

    try:
        config = ServiceConfig.from_env()
        config = replace(
            config,
            host=args.host if args.host is not None else config.host,
            port=args.port if args.port is not None else config.port,
            database=args.db.expanduser() if args.db is not None else config.database,
            token=args.token if args.token is not None else config.token,
            llm_enabled=False if args.no_llm else config.llm_enabled,
        )
        if config.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigurationError(
                "host must be a loopback address; use an HTTPS reverse proxy for remote access"
            )
        if not 0 <= config.port <= 65535:
            raise ConfigurationError("port must be between 0 and 65535")
        if not config.token:
            raise ConfigurationError("token must not be empty")
    except ConfigurationError as exc:
        parser.error(str(exc))

    server = build_server(config)
    responder = server.repository.responder
    port = server.start()
    print(f"AI 陪伴服务已启动：http://{config.host}:{port}", flush=True)
    print(f"手机/Web 页面：http://{config.host}:{port}/", flush=True)
    print(
        "对话模型：未加载，使用严格事实型回复"
        if responder is None
        else f"对话模型：已加载 {responder.name}",
        flush=True,
    )
    print(f"SQLite：{config.database.expanduser().resolve()}", flush=True)
    print("按 Ctrl+C 停止。", flush=True)

    stopped = False

    def stop(*_args) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while not stopped:
            time.sleep(0.25)
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
