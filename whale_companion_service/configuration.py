"""Environment-owned configuration for the standalone companion service."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .provider import ProviderConfig


class ConfigurationError(ValueError):
    """Raised when a service environment variable is invalid."""


def _boolean(value: str, *, name: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean")


def _integer(value: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be an integer") from None
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _number(value: str, *, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be a number") from None
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


@dataclass(frozen=True)
class ServiceConfig:
    """Complete process configuration with no dependency on a client checkout."""

    host: str
    port: int
    database: Path
    token: str
    llm_enabled: bool
    provider: ProviderConfig

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ServiceConfig":
        values = os.environ if environ is None else environ
        host = str(values.get("WHALE_SERVICE_HOST", "127.0.0.1")).strip()
        token = str(values.get("WHALE_MEMORY_TOKEN", "local-dev-token")).strip()
        database_value = str(values.get(
            "WHALE_DATABASE_PATH", Path.home() / ".dsh-whale-memory" / "memory.sqlite3",
        )).strip()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigurationError(
                "WHALE_SERVICE_HOST must be a loopback address; use an HTTPS reverse proxy for remote access"
            )
        if not token:
            raise ConfigurationError("WHALE_MEMORY_TOKEN must not be empty")
        if not database_value:
            raise ConfigurationError("WHALE_DATABASE_PATH must not be empty")
        database = Path(database_value).expanduser()

        provider = ProviderConfig(
            provider_id=str(values.get("WHALE_LLM_PROVIDER_ID", "openai-compatible")).strip(),
            name=str(values.get("WHALE_LLM_NAME", "OpenAI compatible")).strip(),
            base_url=str(values.get("WHALE_LLM_BASE_URL", "https://api.deepseek.com")).strip(),
            chat_path=str(values.get("WHALE_LLM_CHAT_PATH", "/v1/chat/completions")).strip(),
            model=str(values.get("WHALE_LLM_MODEL", "deepseek-v4-flash")).strip(),
            api_key=str(values.get("WHALE_LLM_API_KEY", "")).strip(),
            timeout=_number(str(values.get("WHALE_LLM_TIMEOUT", "60")),
                            name="WHALE_LLM_TIMEOUT", minimum=3.0, maximum=300.0),
            temperature=_number(str(values.get("WHALE_LLM_TEMPERATURE", "0.7")),
                                name="WHALE_LLM_TEMPERATURE", minimum=0.0, maximum=2.0),
            max_tokens=_integer(str(values.get("WHALE_LLM_MAX_TOKENS", "2048")),
                                name="WHALE_LLM_MAX_TOKENS", minimum=80, maximum=32768),
            verify_ssl=_boolean(str(values.get("WHALE_LLM_VERIFY_SSL", "1")),
                                name="WHALE_LLM_VERIFY_SSL"),
        )
        return cls(
            host=host,
            port=_integer(str(values.get("WHALE_SERVICE_PORT", "47821")),
                          name="WHALE_SERVICE_PORT", minimum=0, maximum=65535),
            database=database,
            token=token,
            llm_enabled=_boolean(str(values.get("WHALE_LLM_ENABLED", "1")),
                                 name="WHALE_LLM_ENABLED"),
            provider=provider,
        )
