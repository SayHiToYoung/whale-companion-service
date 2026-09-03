"""Minimal OpenAI-compatible provider configuration used by the service."""
from __future__ import annotations

import json
import re
import ssl
from dataclasses import dataclass

try:
    import certifi
except Exception:  # pragma: no cover - the system CA store is a valid fallback
    certifi = None


@dataclass
class ProviderConfig:
    provider_id: str = "openai-compatible"
    name: str = "OpenAI compatible"
    base_url: str = "https://api.deepseek.com"
    chat_path: str = "/v1/chat/completions"
    model: str = "deepseek-v4-flash"
    api_key: str = ""
    timeout: float = 60.0
    temperature: float = 0.7
    max_tokens: int = 2048
    verify_ssl: bool = True


def make_ssl_context(verify: bool):
    if not verify:
        return ssl._create_unverified_context()
    try:
        if certifi is not None:
            return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    return ssl.create_default_context()


def normalize_chat_endpoint(base_url: str, chat_path: str = "/v1/chat/completions") -> str:
    base = str(base_url or "").strip().rstrip("/")
    path = str(chat_path or "/v1/chat/completions").strip()
    path = path if path.startswith("/") else "/" + path
    if base.endswith("/chat/completions"):
        return base
    if path == "/v1/chat/completions" and re.search(r"/v\d+$", base):
        return base + "/chat/completions"
    return base + path


def safe_error_detail(raw: str) -> str:
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return str(data["error"].get("message", "Provider 请求失败"))
    except Exception:
        pass
    return " ".join(str(raw or "").split())[:300] or "Provider 请求失败"
