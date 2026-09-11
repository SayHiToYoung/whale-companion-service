"""Integration coverage for running the AI companion without a desktop pet."""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from whale_companion_service.cli import build_server, configured_responder
from whale_companion_service.configuration import ServiceConfig
from whale_companion_service.companion_runtime.context_assembler import ContextAssembler


def _request(port: int, path: str, *, token: str = "", payload: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers,
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _config(tmp_path: Path, **environment: str) -> ServiceConfig:
    return ServiceConfig.from_env({
        "WHALE_DATABASE_PATH": str(tmp_path / "standalone.sqlite3"),
        "WHALE_MEMORY_TOKEN": "standalone-secret",
        "WHALE_SERVICE_PORT": "0",
        "WHALE_LLM_ENABLED": "0",
        **environment,
    })


def test_standalone_fallback_chat_is_ready_and_uses_context_assembler(
    tmp_path: Path, monkeypatch,
) -> None:
    calls = 0
    original_assemble = ContextAssembler.assemble

    def observed_assemble(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_assemble(self, *args, **kwargs)

    monkeypatch.setattr(ContextAssembler, "assemble", observed_assemble)
    server = build_server(_config(tmp_path))
    port = server.start()
    try:
        health = _request(port, "/health")
        assert health["ok"] is True
        assert health["status"] == "ready"
        assert health["storage"] == {"ok": True, "kind": "sqlite"}
        assert health["chat"]["mode"] == "grounded_fallback"
        assert health["chat"]["contextAssembler"] == "required"
        assert health["clients"]["desktopPet"] == "optional"
        assert health["optionalInputs"]["desktopObservation"] == {
            "required": False,
            "whenAbsent": "no_observation_facts",
        }
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/debug/", timeout=5) as response:
            assert "共享服务与可选感知端" in response.read().decode("utf-8")

        result = _request(port, "/v1/conversation/messages", token="standalone-secret", payload={
            "userId": "standalone-user",
            "deviceId": "web-client",
            "messageId": "standalone-message-1",
            "role": "user",
            "text": "你好，今天第一次从网页来找你。",
        })
        assert result["replySource"] == "fallback"
        assert result["assistantMessage"]["role"] == "assistant"
        assert result["assistantMessage"]["text"]
        assert calls >= 1

        snapshot = _request(
            port, "/v1/debug/state?userId=standalone-user", token="standalone-secret",
        )
        # Full frames are assembled on demand in the debug snapshot; persisted
        # reply traces intentionally retain only a non-content summary.
        frame = snapshot["companionFrame"]
        assert frame["version"] == "companion-frame-v5"
        scene = next(row for row in frame["processedFacts"] if row["key"] == "scene")
        assert scene["value"]["recentObservedFactId"] == ""
        assert scene["value"]["recentApp"] == ""
        assert scene["value"]["recentContext"] == ""
        assert not any(row["key"].startswith("memory:") for row in frame["processedFacts"])
    finally:
        server.stop()


def test_standalone_llm_uses_only_service_environment_and_no_desktop_facts(
    tmp_path: Path,
) -> None:
    captured: dict = {"bodies": []}

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            request_body = json.loads(self.rfile.read(length).decode("utf-8"))
            captured["bodies"].append(request_body)
            user_text = request_body["messages"][-1]["content"]
            content = (
                "小鲸正在看着你的桌面。"
                if "桌面" in user_text
                else "我在。网页这条路已经通了。"
            )
            body = json.dumps({
                "choices": [{"message": {"content": content}}],
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    model_thread.start()
    config = _config(
        tmp_path,
        WHALE_LLM_ENABLED="1",
        WHALE_LLM_NAME="Standalone test model",
        WHALE_LLM_BASE_URL=f"http://127.0.0.1:{model_server.server_address[1]}",
        WHALE_LLM_MODEL="test-chat",
    )
    responder = configured_responder(config)
    assert responder is not None
    assert responder.name == "Standalone test model / test-chat"
    server = build_server(config)
    port = server.start()
    try:
        result = _request(port, "/v1/conversation/messages", token="standalone-secret", payload={
            "userId": "standalone-user",
            "deviceId": "web-client",
            "messageId": "standalone-model-message-1",
            "role": "user",
            "text": "我从网页来找你了。",
        })
        assert result["replySource"] == "model"
        assert result["assistantMessage"]["text"] == "我在。网页这条路已经通了。"

        messages = captured["bodies"][0]["messages"]
        assert messages[-1] == {"role": "user", "content": "我从网页来找你了。"}
        system = messages[0]["content"]
        encoded_frame = system.split("<companion_frame>", 1)[1].split("</companion_frame>", 1)[0]
        frame = json.loads(encoded_frame)
        assert frame["version"] == "companion-frame-v5"
        assert not any(row["key"].startswith("memory:") for row in frame["processedFacts"])
        scene = next(row for row in frame["processedFacts"] if row["key"] == "scene")
        assert scene["value"]["recentApp"] == ""
        assert "没有 L1 时不得声称小鲸在线" in system

        rejected = _request(port, "/v1/conversation/messages", token="standalone-secret", payload={
            "userId": "standalone-user",
            "deviceId": "web-client",
            "messageId": "standalone-model-message-2",
            "role": "user",
            "text": "你能看到我的桌面吗？",
        })
        assert rejected["replySource"] == "fallback"
        assert rejected["assistantMessage"]["text"] != "小鲸正在看着你的桌面。"
    finally:
        server.stop()
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(2)


def test_service_startup_has_no_desktop_package_fallback(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert configured_responder(config) is None

    import inspect
    import whale_companion_service.cli as cli

    source = inspect.getsource(cli)
    assert "pet.config" not in source
    assert "dsh-pet-indesktop" not in source
