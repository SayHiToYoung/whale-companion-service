# -*- coding: utf-8 -*-
"""第二阶段：统一客户端契约。

这里的每个测试都打真实 HTTP 服务，因为契约是网线上的东西，不是仓库方法的内部形状。
覆盖五件事：契约与实现一致、多客户端共享一份有序历史、幂等键、断线恢复、
以及在完全没有桌宠的前提下这一切照样成立。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from whale_companion_service.client_contract import (
    CLIENT_API_VERSION,
    CONTRACT,
    CONTRACT_PATH,
    ERROR_CATALOG,
    SchemaError,
    api_descriptor,
    normalize_client,
    validate_instance,
    validate_request,
    validate_response,
)
from whale_companion_service.memory_protocol import MemorySyncClient, SyncTransportError
from whale_companion_service.memory_server import MemoryApiServer, MemoryRepository


TOKEN = "contract-secret"


class Response:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.payload = payload


def call(
    port: int,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    token: str | None = TOKEN,
    headers: dict | None = None,
) -> Response:
    """一次原始 HTTP 调用，错误状态也照样把响应体带回来。"""
    request_headers = {"Accept": "application/json", **(headers or {})}
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=request_headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return Response(response.status, json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, json.loads(exc.read().decode("utf-8")))


@pytest.fixture()
def service(tmp_path: Path):
    """一台固定时钟的真实服务。固定时钟让主动陪伴的闸门行为可复现。"""
    now = datetime(2026, 3, 5, 9, tzinfo=datetime.now().astimezone().tzinfo)
    server = MemoryApiServer(
        MemoryRepository(tmp_path / "contract.sqlite3", clock=lambda: now),
        token=TOKEN, port=0,
    )
    port = server.start()
    try:
        yield server, port
    finally:
        server.stop()


def identity(client_id: str, kind: str, capabilities: list[str]) -> dict:
    return {
        "clientId": client_id, "kind": kind, "version": "1.0.0",
        "apiVersion": CLIENT_API_VERSION, "capabilities": capabilities,
    }


def send(port: int, *, user: str, device: str, message_id: str, text: str,
         client: dict | None = None) -> Response:
    body = {"userId": user, "deviceId": device, "messageId": message_id,
            "role": "user", "text": text}
    if client is not None:
        body["client"] = client
    return call(port, "POST", "/v1/conversation/messages", body=body)


def history(port: int, *, user: str, after: int = 0, limit: int = 200,
            headers: dict | None = None) -> Response:
    return call(
        port, "GET",
        f"/v1/conversation/messages?userId={user}&afterMessageSeq={after}&limit={limit}",
        headers=headers,
    )


# --------------------------------------------------------------------------
# 契约本身
# --------------------------------------------------------------------------

def test_contract_is_served_without_a_token_and_describes_the_core_endpoints(service) -> None:
    _server, port = service
    response = call(port, "GET", CONTRACT_PATH, token=None)
    assert response.status == 200
    document = response.payload
    assert document["openapi"] == "3.1.0"
    # 六个核心接口一个不少，而且都能在文档里查到自己的请求/响应形状。
    assert set(document["paths"]) >= {
        "/health", CONTRACT_PATH, "/v1/conversation/messages",
        "/v1/companion/proactive", "/v1/companion/proactive/deliveries",
        "/v1/companion/openings/claim",
    }
    assert document["paths"]["/v1/conversation/messages"].keys() >= {"get", "post"}
    # 契约里写明的四个 id 语义是这一阶段的产出，必须能在文档里读到。
    described = document["info"]["description"]
    for token in ("messageId", "deliveryId", "claimId", "afterMessageSeq"):
        assert token in described, f"契约没有说明 {token} 的语义"


def test_every_documented_path_is_actually_routed(service) -> None:
    """契约里写了的路径，服务端必须真的有。文档不能领先实现。"""
    _server, port = service
    for path, operations in CONTRACT["paths"].items():
        for method in operations:
            probe = call(port, method.upper(), path.replace("{", "").replace("}", ""),
                         body={} if method == "post" else None)
            assert probe.status != 404, f"{method.upper()} {path} 在契约里有，在服务端没有"


def test_error_catalog_and_contract_enum_cannot_drift() -> None:
    schema = CONTRACT["components"]["schemas"]["ErrorResponse"]
    assert schema["properties"]["code"]["enum"] == sorted(ERROR_CATALOG)
    # 每个 code 都要有 HTTP 状态、可重试标记和可读说明，缺一不可。
    for code, entry in ERROR_CATALOG.items():
        assert 400 <= entry["status"] <= 599, code
        assert isinstance(entry["retryable"], bool), code
        assert entry["message"].strip(), code
    # 不变式一：可不可以重试由**这个错误自己的语义**决定，不由状态码区间决定。
    # 绝大多数 4xx 是"请求本身有问题"，等再久也不会成功；但 408 Request Timeout 和
    # 429 Too Many Requests 说的是"此刻不行，等一下再来"——它们是可重试的 4xx。
    # 所以这里锁的不是"4xx 一律不可重试"，而是"可重试的 4xx 只能是这一份显式清单"。
    retryable_4xx = {code for code, entry in ERROR_CATALOG.items()
                     if entry["status"] < 500 and entry["retryable"]}
    assert retryable_4xx == {"request.timeout", "request.rate_limited"}
    assert {ERROR_CATALOG[code]["status"] for code in retryable_4xx} == {408, 429}
    # 建议退避时长只属于可重试的错误。让一个不可重试的错误说"5 秒后再来"
    # 等于请客户端做一件永远不会成功的事。
    for code, entry in ERROR_CATALOG.items():
        if entry.get("retryAfterSeconds"):
            assert entry["retryable"], f"{code} 不可重试却给了 retryAfterSeconds"
        assert float(entry.get("retryAfterSeconds") or 0.0) >= 0.0, code
    # 不变式二只针对当前目录里这两个 5xx，而不是"所有 5xx 都可重试"这条并不成立的通则
    # （501 Not Implemented、505 都是重试不会好的 5xx）。将来新增不可重试的 5xx 时，
    # 把它列进 expected 并写清理由即可，不必推翻整条断言。
    retryable_5xx = {code for code, entry in ERROR_CATALOG.items()
                     if entry["status"] >= 500 and entry["retryable"]}
    assert retryable_5xx == {"internal_error", "service_unavailable"}
    assert {code for code, entry in ERROR_CATALOG.items()
            if entry["status"] >= 500} == retryable_5xx


def test_schema_validator_rejects_what_it_should(service) -> None:
    """校验器本身要有牙齿，否则所有契约测试都只是摆设。"""
    _server, port = service
    page = history(port, user="u").payload
    validate_response("/v1/conversation/messages", "get", page)
    with pytest.raises(SchemaError):
        validate_response("/v1/conversation/messages", "get", {**page, "nextMessageSeq": "7"})
    with pytest.raises(SchemaError):
        validate_response("/v1/conversation/messages", "get",
                          {k: v for k, v in page.items() if k != "hasMore"})
    with pytest.raises(SchemaError):
        validate_instance({"type": "string", "enum": ["a"]}, "b")


def test_health_matches_contract_and_advertises_the_client_api(service) -> None:
    _server, port = service
    response = call(port, "GET", "/health", token=None)
    assert response.status == 200
    validate_response("/health", "get", response.payload)
    api = response.payload["api"]
    assert api == api_descriptor()
    assert api["version"] == CLIENT_API_VERSION
    assert api["contract"] == CONTRACT_PATH
    # 身份声明永远是可选的，这是对旧客户端的承诺，写进健康检查里让人能验。
    assert api["identityOptional"] is True
    assert set(api["errorCodes"]) == set(ERROR_CATALOG)
    validate_instance(CONTRACT["components"]["schemas"]["ApiDescriptor"], api, root=CONTRACT)


# --------------------------------------------------------------------------
# 六个核心接口的请求/响应与契约一致
# --------------------------------------------------------------------------

def test_conversation_endpoints_match_contract(service) -> None:
    _server, port = service
    body = {"userId": "u", "deviceId": "web", "messageId": "m1",
            "role": "user", "text": "今天去公园散步了", "client": identity("web-1", "web", [])}
    validate_request("/v1/conversation/messages", "post", body)
    response = call(port, "POST", "/v1/conversation/messages", body=body)
    assert response.status == 200
    validate_response("/v1/conversation/messages", "post", response.payload)
    page = history(port, user="u")
    validate_response("/v1/conversation/messages", "get", page.payload)
    assert page.payload["messages"], "刚写进去的消息必须能读回来"


def test_proactive_endpoints_match_contract(service) -> None:
    _server, port = service
    readonly = call(port, "GET", "/v1/companion/proactive?userId=u&activity=idle")
    assert readonly.status == 200
    validate_response("/v1/companion/proactive", "get", readonly.payload)

    body = {"userId": "u", "deliveryId": "d1", "deviceId": "phone", "activity": "idle",
            "client": identity("phone-1", "mobile", ["proactive.receive"])}
    validate_request("/v1/companion/proactive/deliveries", "post", body)
    committed = call(port, "POST", "/v1/companion/proactive/deliveries", body=body)
    assert committed.status == 200
    validate_response("/v1/companion/proactive/deliveries", "post", committed.payload)
    assert committed.payload["deliveryId"] == "d1"


def test_opening_claim_matches_contract(service) -> None:
    _server, port = service
    body = {"userId": "u", "deviceId": "phone", "claimId": "c1"}
    validate_request("/v1/companion/openings/claim", "post", body)
    response = call(port, "POST", "/v1/companion/openings/claim", body=body)
    assert response.status == 200
    validate_response("/v1/companion/openings/claim", "post", response.payload)


def test_readonly_proactive_polling_never_commits_anything(service) -> None:
    """只读评估必须真的只读，否则契约里"轮询不算说过"这句话就是假的。"""
    server, port = service
    for _ in range(3):
        assert call(port, "GET", "/v1/companion/proactive?userId=u").status == 200
    assert server.repository.messages("u")["messages"] == []


# --------------------------------------------------------------------------
# 幂等
# --------------------------------------------------------------------------

def test_same_message_id_retry_produces_exactly_one_reply(service) -> None:
    _server, port = service
    first = send(port, user="u", device="phone", message_id="m1", text="我今天很开心")
    assert first.payload["duplicate"] is False
    reply_id = first.payload["assistantMessage"]["messageId"]

    for _ in range(3):
        replay = send(port, user="u", device="phone", message_id="m1", text="我今天很开心")
        assert replay.status == 200
        assert replay.payload["duplicate"] is True
        # 重放不能生成第二条回复——同一个 messageId 永远对应同一条回复。
        assert replay.payload["assistantMessage"]["messageId"] == reply_id

    messages = history(port, user="u").payload["messages"]
    assert [row["messageId"] for row in messages].count("m1") == 1
    assert [row["messageId"] for row in messages].count(reply_id) == 1
    assert len(messages) == 2


def test_same_message_id_with_different_text_returns_a_stable_conflict(service) -> None:
    _server, port = service
    send(port, user="u", device="phone", message_id="m1", text="第一段内容")
    conflict = send(port, user="u", device="phone", message_id="m1", text="换了一段内容")

    assert conflict.status == 409
    validate_response("/v1/conversation/messages", "post", conflict.payload, status=409)
    assert conflict.payload["code"] == "conversation.message_id_conflict"
    # 冲突不可重试：客户端要换新 id，不是换个时间再来一次。
    assert conflict.payload["retryable"] is False
    assert conflict.payload["message"].strip()
    # 冲突不能污染历史。
    texts = [row["text"] for row in history(port, user="u").payload["messages"]]
    assert "换了一段内容" not in texts

    # 稳定的意思是：再来一次拿到的是同一个 code，不是随机的另一种说法。
    again = send(port, user="u", device="phone", message_id="m1", text="又换了一段")
    assert again.payload["code"] == conflict.payload["code"]
    assert again.status == conflict.status


def test_same_delivery_id_never_produces_a_second_proactive_message(service) -> None:
    _server, port = service
    body = {"userId": "u", "deliveryId": "d1", "deviceId": "phone", "activity": "idle"}
    first = call(port, "POST", "/v1/companion/proactive/deliveries", body=body)
    assert first.payload["assistantMessage"] is not None
    message_id = first.payload["assistantMessage"]["messageId"]

    for _ in range(3):
        replay = call(port, "POST", "/v1/companion/proactive/deliveries", body=body)
        # 重放拿回逐字节相同的结果。
        assert replay.payload == first.payload

    ids = [row["messageId"] for row in history(port, user="u").payload["messages"]]
    assert ids.count(message_id) == 1


def test_a_second_delivery_id_does_not_resend_the_same_content(service) -> None:
    """换一个 deliveryId 也不能把同一条主动说第二遍——去重的底线在会话历史里。"""
    _server, port = service
    first = call(port, "POST", "/v1/companion/proactive/deliveries",
                 body={"userId": "u", "deliveryId": "d1", "deviceId": "phone"})
    assert first.payload["assistantMessage"] is not None
    second = call(port, "POST", "/v1/companion/proactive/deliveries",
                  body={"userId": "u", "deliveryId": "d2", "deviceId": "desktop"})
    assert second.payload["assistantMessage"] is None
    assert len(history(port, user="u").payload["messages"]) == 1


def test_claim_id_replay_is_marked_and_scoped_to_one_device(service) -> None:
    _server, port = service
    body = {"userId": "u", "deviceId": "phone", "claimId": "c1"}
    first = call(port, "POST", "/v1/companion/openings/claim", body=body)
    replay = call(port, "POST", "/v1/companion/openings/claim", body=body)
    assert "duplicateClaim" not in first.payload
    assert replay.payload["duplicateClaim"] is True
    assert replay.payload["shouldSend"] == first.payload["shouldSend"]

    # claimId 的作用域是 (userId, deviceId)：另一台设备用同一个 claimId 不算重放。
    other = call(port, "POST", "/v1/companion/openings/claim",
                 body={**body, "deviceId": "desktop"})
    assert "duplicateClaim" not in other.payload


# --------------------------------------------------------------------------
# 多客户端与断线恢复
# --------------------------------------------------------------------------

def test_three_clients_read_one_identical_ordered_history(service) -> None:
    """网页、手机和未来的桌宠读的是同一条时间线，顺序一致、内容一致。"""
    _server, port = service
    send(port, user="u", device="web", message_id="w1", text="从网页说的第一句",
         client=identity("web-1", "web", ["conversation.send"]))
    send(port, user="u", device="phone", message_id="p1", text="从手机说的第二句",
         client=identity("phone-1", "mobile", ["conversation.send"]))
    call(port, "POST", "/v1/companion/proactive/deliveries",
         body={"userId": "u", "deliveryId": "d1", "deviceId": "phone",
               "client": identity("phone-1", "mobile", ["proactive.receive"])})

    views = {}
    for name, headers in (
        ("web", {"X-Whale-Client-Id": "web-1", "X-Whale-Client-Kind": "web"}),
        ("mobile", {"X-Whale-Client-Id": "phone-1", "X-Whale-Client-Kind": "mobile"}),
        # 未来的桌宠只是又一个客户端，走的是同一个接口，没有第二套聊天通道。
        ("desktopPet", {"X-Whale-Client-Id": "pet-1", "X-Whale-Client-Kind": "desktopPet"}),
        ("legacy", None),
    ):
        page = history(port, user="u", headers=headers)
        validate_response("/v1/conversation/messages", "get", page.payload)
        views[name] = [(row["messageSeq"], row["messageId"], row["role"], row["text"])
                       for row in page.payload["messages"]]

    assert len(set(map(tuple, views.values()))) == 1, "不同客户端读到了不同的历史"
    ordered = views["web"]
    assert [row[0] for row in ordered] == sorted(row[0] for row in ordered)
    # 两句用户消息按写入顺序排列，各自的回复紧随其后。
    assert [row[1] for row in ordered if row[2] == "user"] == ["w1", "p1"]
    assert [row[2] for row in ordered][:4] == ["user", "assistant", "user", "assistant"]
    assert all(row[2] == "assistant" for row in ordered[4:])
    assert all(row[3].strip() for row in ordered), "历史里不该有空消息"


def test_a_client_resumes_from_after_message_seq_after_a_disconnect(service) -> None:
    _server, port = service
    send(port, user="u", device="web", message_id="w1", text="断线之前说的话")
    before = history(port, user="u")
    cursor = before.payload["nextMessageSeq"]
    seen = {row["messageId"] for row in before.payload["messages"]}
    assert cursor > 0

    # 客户端在这段时间里是断开的，别的客户端还在写。
    send(port, user="u", device="phone", message_id="p1", text="断线期间另一台设备说的话")
    call(port, "POST", "/v1/companion/proactive/deliveries",
         body={"userId": "u", "deliveryId": "d1", "deviceId": "phone"})

    resumed = history(port, user="u", after=cursor)
    recovered = [row["messageId"] for row in resumed.payload["messages"]]
    assert recovered, "从游标续读必须拿到断线期间的消息"
    # 游标是排他的：已经见过的一条都不会再来。
    assert not (set(recovered) & seen)
    assert "p1" in recovered
    assert resumed.payload["nextMessageSeq"] > cursor

    # 续读到底之后是空页，且游标原地不动——客户端不会空转。
    tail = history(port, user="u", after=resumed.payload["nextMessageSeq"])
    assert tail.payload["messages"] == []
    assert tail.payload["nextMessageSeq"] == resumed.payload["nextMessageSeq"]
    assert tail.payload["hasMore"] is False


def test_paging_walks_the_whole_history_without_gaps_or_repeats(service) -> None:
    _server, port = service
    for index in range(6):
        send(port, user="u", device="web", message_id=f"m{index}", text=f"第 {index} 句话")

    cursor, collected, pages = 0, [], 0
    while True:
        page = history(port, user="u", after=cursor, limit=2).payload
        collected.extend(row["messageSeq"] for row in page["messages"])
        pages += 1
        if not page["messages"] or page["nextMessageSeq"] <= cursor:
            break
        cursor = page["nextMessageSeq"]
    assert pages > 1
    assert collected == sorted(collected)
    assert len(collected) == len(set(collected))
    assert collected == [row["messageSeq"] for row in history(port, user="u").payload["messages"]]


def test_two_clients_racing_the_same_message_id_still_get_one_reply(service) -> None:
    """两个客户端同时重放同一条消息（比如手机和网页共享待发箱）也只有一条回复。"""
    from concurrent.futures import ThreadPoolExecutor

    _server, port = service
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda device: send(port, user="u", device=device, message_id="shared",
                                text="同一条待发消息"),
            ["phone", "web"],
        ))
    assert all(row.status == 200 for row in results)
    reply_ids = {row.payload["assistantMessage"]["messageId"] for row in results}
    assert len(reply_ids) == 1
    assert len(history(port, user="u").payload["messages"]) == 2


# --------------------------------------------------------------------------
# 可选身份与向后兼容
# --------------------------------------------------------------------------

def test_client_identity_is_optional_in_every_form(service) -> None:
    server, port = service
    # 1) 什么都不发（旧客户端）。
    assert send(port, user="u", device="web", message_id="m1", text="一").status == 200
    # 2) 请求体里发。
    assert send(port, user="u", device="web", message_id="m2", text="二",
                client=identity("body-1", "web", ["conversation.send"])).status == 200
    # 3) 只发请求头（GET 也能带身份）。
    assert history(port, user="u", headers={
        "X-Whale-Client-Id": "header-1", "X-Whale-Client-Kind": "desktopPet",
        "X-Whale-Client-Capabilities": "conversation.history proactive.receive",
    }).status == 200

    recorded = {row["clientId"]: row for row in server.repository.client_sessions("u")}
    assert set(recorded) == {"body-1", "header-1"}, "匿名客户端不该留下记录"
    assert recorded["body-1"]["kind"] == "web"
    assert recorded["header-1"]["kind"] == "desktopPet"
    assert recorded["header-1"]["capabilities"] == ["conversation.history", "proactive.receive"]


def test_unknown_client_kind_and_capability_are_accepted_not_rejected(service) -> None:
    """未来的客户端不该被今天的枚举挡在门外。"""
    _server, port = service
    response = send(port, user="u", device="x", message_id="m1", text="来自未来的客户端",
                    client={"clientId": "future-1", "kind": "smart-fridge",
                            "capabilities": ["telepathy.v9"]})
    assert response.status == 200
    assert normalize_client({"kind": "smart-fridge"})["kind"] == "other"
    assert normalize_client({"capabilities": ["telepathy.v9"]})["capabilities"] == ["telepathy.v9"]


def test_a_malformed_client_block_is_a_stable_error_not_a_crash(service) -> None:
    _server, port = service
    response = call(port, "POST", "/v1/conversation/messages", body={
        "userId": "u", "deviceId": "web", "messageId": "m1", "role": "user",
        "text": "正文没问题", "client": {"capabilities": "not-a-list"},
    })
    assert response.status == 400
    assert response.payload["code"] == "request.invalid_client"
    assert response.payload["retryable"] is False
    validate_response("/v1/conversation/messages", "post", response.payload, status=400)
    # 请求被整体拒绝，历史不留半条。
    assert history(port, user="u").payload["messages"] == []


def test_legacy_client_sending_no_new_field_behaves_exactly_as_before(service) -> None:
    """旧客户端的完整一轮：发消息、读历史、领主动，一个新字段都不发。"""
    server, port = service
    sent = call(port, "POST", "/v1/conversation/messages", body={
        "userId": "u", "deviceId": "old-phone", "messageId": "legacy-1",
        "role": "user", "text": "我还是老版本",
    })
    assert sent.status == 200
    assert sent.payload["accepted"] is True
    assert sent.payload["assistantMessage"]["text"]

    page = call(port, "GET", "/v1/conversation/messages?userId=u&afterMessageSeq=0&limit=200")
    assert page.status == 200
    assert {"messages", "nextMessageSeq", "hasMore"} <= set(page.payload)

    delivery = call(port, "POST", "/v1/companion/proactive/deliveries",
                    body={"userId": "u", "deliveryId": "legacy-d1"})
    assert delivery.status == 200
    claim = call(port, "POST", "/v1/companion/openings/claim",
                 body={"userId": "u", "deviceId": "old-phone", "claimId": "legacy-c1"})
    assert claim.status == 200
    assert server.repository.client_sessions("u") == []


@pytest.mark.parametrize("method,path,body,token,expected_code", [
    ("GET", "/v1/conversation/messages?userId=u", None, "wrong-token", "request.unauthorized"),
    ("GET", "/v1/nope", None, TOKEN, "request.not_found"),
    ("GET", "/v1/conversation/messages", None, TOKEN, "request.user_id_required"),
    ("POST", "/v1/conversation/messages", {"userId": "u"}, TOKEN, "conversation.invalid_message"),
    ("POST", "/v1/conversation/messages",
     {"userId": "u", "deviceId": "d", "messageId": "m", "role": "user", "text": ""},
     TOKEN, "conversation.invalid_text"),
    ("POST", "/v1/companion/proactive/deliveries", {"userId": "u"}, TOKEN,
     "proactive.invalid_delivery"),
    ("POST", "/v1/companion/openings/claim", {"userId": "u"}, TOKEN, "openings.invalid_claim"),
])
def test_every_error_uses_the_same_envelope(service, method, path, body, token,
                                            expected_code) -> None:
    _server, port = service
    response = call(port, method, path, body=body, token=token)
    payload = response.payload
    validate_instance(CONTRACT["components"]["schemas"]["ErrorResponse"], payload, root=CONTRACT)
    assert payload["code"] == expected_code
    assert payload["status"] == response.status == ERROR_CATALOG[expected_code]["status"]
    assert payload["retryable"] is ERROR_CATALOG[expected_code]["retryable"]
    # 顶层 error 字符串是旧客户端唯一读得懂的字段，不能因为加了新字段就消失。
    assert isinstance(payload["error"], str) and payload["error"].strip()


def test_the_python_client_and_the_http_surface_are_the_same_contract(service) -> None:
    """`MemorySyncClient` 是核心 API 的第一方实现，不是另一套接口。"""
    _server, port = service
    client = MemorySyncClient(
        f"http://127.0.0.1:{port}", TOKEN,
        client=identity("py-1", "desktopPet", ["conversation.send", "proactive.receive"]),
    )
    assert client.health()["api"]["version"] == CLIENT_API_VERSION
    assert client.contract()["openapi"] == "3.1.0"

    posted = client.post_message(user_id="u", device_id="pet", message_id="m1",
                                 role="user", text="桌宠也走同一条路")
    validate_response("/v1/conversation/messages", "post", posted)
    assert client.post_message(user_id="u", device_id="pet", message_id="m1",
                               role="user", text="桌宠也走同一条路")["duplicate"] is True

    delivered = client.deliver_proactive(user_id="u", device_id="pet", delivery_id="d1")
    validate_response("/v1/companion/proactive/deliveries", "post", delivered)
    assert client.deliver_proactive(user_id="u", device_id="pet", delivery_id="d1") == delivered

    page = client.conversation_messages(user_id="u", after_message_seq=0)
    validate_response("/v1/conversation/messages", "get", page)
    assert not client.conversation_messages(
        user_id="u", after_message_seq=page["nextMessageSeq"])["messages"]
    validate_response("/v1/companion/proactive", "get",
                      client.proactive_decision(user_id="u"))


def test_the_python_client_surfaces_retryable_and_code(service) -> None:
    _server, port = service
    client = MemorySyncClient(f"http://127.0.0.1:{port}", TOKEN)
    client.post_message(user_id="u", device_id="pet", message_id="m1", role="user", text="第一段")
    with pytest.raises(SyncTransportError) as conflict:
        client.post_message(user_id="u", device_id="pet", message_id="m1",
                            role="user", text="第二段")
    assert conflict.value.code == "conversation.message_id_conflict"
    assert conflict.value.retryable is False
    assert conflict.value.status == 409
    # 旧调用方只读消息文本，格式必须原样保留。
    assert str(conflict.value).startswith("HTTP 409: ")

    with pytest.raises(SyncTransportError) as unauthorized:
        MemorySyncClient(f"http://127.0.0.1:{port}", "wrong").messages("u")
    assert unauthorized.value.code == "request.unauthorized"
    assert unauthorized.value.retryable is False


# --------------------------------------------------------------------------
# 无桌宠回归
# --------------------------------------------------------------------------

def test_the_whole_contract_holds_with_no_desktop_pet_anywhere(service) -> None:
    """没有桌宠、没有桌面观测、没有任何 L1 事实，六个接口照样是完整可用的。"""
    server, port = service
    health = call(port, "GET", "/health", token=None).payload
    assert health["ok"] is True and health["status"] == "ready"
    assert health["clients"]["desktopPet"] == "optional"
    assert health["optionalInputs"]["desktopObservation"]["required"] is False
    assert health["api"]["identityOptional"] is True

    sent = send(port, user="u", device="web", message_id="m1", text="只有网页在用",
                client=identity("web-only", "web", ["conversation.send"]))
    assert sent.payload["assistantMessage"]["text"]
    assert sent.payload["replySource"] in {"model", "fallback"}

    assert call(port, "GET", "/v1/companion/proactive?userId=u").status == 200
    assert call(port, "POST", "/v1/companion/proactive/deliveries",
                body={"userId": "u", "deliveryId": "d1"}).status == 200
    assert call(port, "POST", "/v1/companion/openings/claim",
                body={"userId": "u", "deviceId": "web", "claimId": "c1"}).status == 200
    assert history(port, user="u").payload["messages"]

    # 服务端确实一条桌面事实都没有——这一轮不是靠桌宠撑起来的。
    with server.repository._connect() as db:
        assert db.execute("SELECT count(*) FROM memory_events WHERE layer='L1'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memory_batches").fetchone()[0] == 0


def test_the_contract_never_makes_a_desktop_pet_a_precondition() -> None:
    """契约本身不能把桌宠写成必需品。"""
    assert "desktopPet" in CONTRACT["components"]["schemas"]["ClientIdentity"]["properties"]["kind"]["enum"]
    assert CONTRACT["components"]["schemas"]["HealthResponse"]["properties"]["clients"] \
        ["properties"]["desktopPet"]["const"] == "optional"
    for name, schema in CONTRACT["components"]["schemas"].items():
        for field in schema.get("required") or ():
            assert "desktop" not in field.lower(), f"{name}.{field} 把桌宠变成了必填"


# --------------------------------------------------------------------------
# 分页精确信号
# --------------------------------------------------------------------------

def test_has_more_exact_is_precise_where_has_more_is_conservative(service) -> None:
    """`hasMore` 保守（取满即 true），`hasMoreExact` 精确（真的还有才 true）。"""
    _server, port = service
    for index in range(6):
        send(port, user="u", device="web", message_id=f"m{index}", text=f"第 {index} 句")
    total = len(history(port, user="u").payload["messages"])
    assert total == 12  # 六句用户消息 + 六条回复

    middle = history(port, user="u", limit=4).payload
    validate_response("/v1/conversation/messages", "get", middle)
    assert len(middle["messages"]) == 4
    assert middle["hasMore"] is True and middle["hasMoreExact"] is True

    # 关键分界：最后一页刚好取满。旧字段说"可能还有"，新字段说实话。
    exact = history(port, user="u", after=total - 4, limit=4).payload
    assert len(exact["messages"]) == 4
    assert exact["hasMore"] is True, "hasMore 的保守语义必须原样保留"
    assert exact["hasMoreExact"] is False

    # 没取满和空页两种情况下两个字段一致。
    short = history(port, user="u", limit=total + 10).payload
    assert short["hasMore"] is False and short["hasMoreExact"] is False
    empty = history(port, user="u", after=total).payload
    assert empty["messages"] == []
    assert empty["hasMore"] is False and empty["hasMoreExact"] is False


def test_paging_by_has_more_exact_alone_terminates_without_an_empty_round(service) -> None:
    """只看 hasMoreExact 就能翻完，不需要客户端自己再拼游标条件。"""
    _server, port = service
    for index in range(5):
        send(port, user="u", device="web", message_id=f"m{index}", text=f"第 {index} 句")

    cursor, collected, requests = 0, [], 0
    while True:
        page = history(port, user="u", after=cursor, limit=2).payload
        requests += 1
        collected.extend(row["messageSeq"] for row in page["messages"])
        if not page["hasMoreExact"]:
            break
        cursor = page["nextMessageSeq"]
        assert requests < 50
    assert collected == [row["messageSeq"] for row in history(port, user="u").payload["messages"]]
    # 精确信号意味着最后一次请求一定带回了数据，不存在收尾的空页。
    assert requests == -(-len(collected) // 2)


def test_the_page_probe_row_never_leaks_into_the_response(service) -> None:
    """多取的那一条只用于判断，不能出现在 messages 里，也不能推进 nextMessageSeq。"""
    _server, port = service
    for index in range(4):
        send(port, user="u", device="web", message_id=f"m{index}", text=f"第 {index} 句")
    full = history(port, user="u").payload["messages"]

    page = history(port, user="u", limit=3).payload
    assert [row["messageSeq"] for row in page["messages"]] ==         [row["messageSeq"] for row in full[:3]]
    assert page["nextMessageSeq"] == full[2]["messageSeq"]
    assert page["hasMoreExact"] is True
    # 从这个游标续读，第 4 条一条不漏也一条不重。
    following = history(port, user="u", after=page["nextMessageSeq"]).payload
    assert [row["messageSeq"] for row in following["messages"]] ==         [row["messageSeq"] for row in full[3:]]


def test_the_contract_documents_both_paging_signals() -> None:
    page = CONTRACT["components"]["schemas"]["MessagePage"]
    assert set(page["required"]) == {"messages", "nextMessageSeq", "hasMore", "hasMoreExact"}
    # 旧字段仍然是必填的：不给旧客户端制造"字段消失了"的意外。
    assert "hasMore" in page["properties"] and "hasMoreExact" in page["properties"]
    assert "hasMoreExact" in CONTRACT["info"]["description"]
    assert "conversation.hasMoreExact" in api_descriptor()["capabilities"]


# --------------------------------------------------------------------------
# 错误码映射的抗漂移
# --------------------------------------------------------------------------

def test_every_mapped_error_message_is_still_raised_somewhere(service) -> None:
    """错误原文 → 稳定 code 的映射靠字符串匹配，改一个字就会静默降级为通用 code。

    这条测试把映射表和真实的 raise 点绑在一起：改了原文而忘了改映射，这里立刻红。
    """
    import whale_companion_service.client_contract as contract_module
    from pathlib import Path as _Path

    root = _Path(contract_module.__file__).parent
    sources = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if path.name != "client_contract.py"
    )
    for message in contract_module._MESSAGE_CODES:
        quoted = '"' + message + '"'
        assert quoted in sources, (
            f"映射表里的错误原文 {message!r} 已经没有任何地方在抛了，映射会静默失效"
        )


def test_an_unmapped_error_still_gets_a_usable_envelope(service) -> None:
    """映射不到时退到通用 code，而不是漏出裸字符串或 500。"""
    _server, port = service
    # "unsupported digest kind" 不在映射表里，走的是 bad_request 兜底。
    response = call(port, "GET", "/v1/memory/digests?userId=u&kind=nope")
    assert response.status == 400
    validate_instance(CONTRACT["components"]["schemas"]["ErrorResponse"],
                      response.payload, root=CONTRACT)
    assert response.payload["code"] == "bad_request"
    assert response.payload["retryable"] is False
    # 原文仍然完整传给了人，只是机器读的是 code。
    assert response.payload["error"] == "unsupported digest kind"
