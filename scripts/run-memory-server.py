#!/usr/bin/env python3
"""Run the standalone shared companion-memory service."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from whale_companion_service import (  # noqa: E402
    MemoryApiServer,
    MemoryRepository,
    ModelCompanionResponder,
    ProviderConfig,
)


def configured_responder() -> ModelCompanionResponder | None:
    config = ProviderConfig(
        provider_id=os.environ.get("WHALE_LLM_PROVIDER_ID", "openai-compatible"),
        name=os.environ.get("WHALE_LLM_NAME", "DeepSeek"),
        base_url=os.environ.get("WHALE_LLM_BASE_URL", "https://api.deepseek.com"),
        chat_path=os.environ.get("WHALE_LLM_CHAT_PATH", "/v1/chat/completions"),
        model=os.environ.get("WHALE_LLM_MODEL", "deepseek-v4-flash"),
        api_key=os.environ.get("WHALE_LLM_API_KEY", ""),
        timeout=float(os.environ.get("WHALE_LLM_TIMEOUT", "60")),
        temperature=float(os.environ.get("WHALE_LLM_TEMPERATURE", "0.7")),
        max_tokens=int(os.environ.get("WHALE_LLM_MAX_TOKENS", "2048")),
        verify_ssl=os.environ.get("WHALE_LLM_VERIFY_SSL", "1").lower()
        not in {"0", "false", "no"},
    )
    responder = ModelCompanionResponder(config)
    if responder.available:
        return responder

    # 本地一体化运行时复用桌宠已经配置并安全保存的模型设置。
    # 独立部署仍可完全通过 WHALE_LLM_* 环境变量运行。
    desktop_root = ROOT.parent / "dsh-pet-indesktop"
    if not desktop_root.is_dir():
        return None
    try:
        if str(desktop_root) not in sys.path:
            sys.path.insert(0, str(desktop_root))
        from pet.config import Config

        desktop_config = Config()
        settings = desktop_config.chat_settings()
        if not settings.enabled:
            return None
        current = settings.active_config
        provider = ProviderConfig(
            provider_id=current.provider_id,
            name=current.name,
            base_url=current.base_url,
            chat_path=current.chat_path,
            model=current.model,
            api_key=desktop_config.resolve_api_key(current),
            timeout=current.timeout,
            temperature=current.temperature,
            max_tokens=current.max_tokens,
            verify_ssl=current.verify_ssl,
        )
        desktop_responder = ModelCompanionResponder(provider)
        return desktop_responder if desktop_responder.available else None
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="小鲸/大鲸共享陪伴服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47821)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".dsh-whale-memory" / "memory.sqlite3",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("WHALE_MEMORY_TOKEN", "local-dev-token"),
    )
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("本地启动器只允许监听回环地址；公网部署请使用 HTTPS 反向代理")
    if not args.token:
        parser.error("token 不能为空")

    responder = None if args.no_llm else configured_responder()
    server = MemoryApiServer(
        MemoryRepository(args.db, responder=responder),
        token=args.token,
        host=args.host,
        port=args.port,
    )
    port = server.start()
    print(f"共享陪伴服务已启动：http://{args.host}:{port}")
    print(f"大鲸手机页：http://{args.host}:{port}/")
    print(
        "大鲸模型配置：未加载，使用严格事实型回复"
        if responder is None
        else f"大鲸模型配置：已加载 {responder.name}"
    )
    print(f"SQLite：{args.db.expanduser().resolve()}")
    print("按 Ctrl+C 停止。")

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
