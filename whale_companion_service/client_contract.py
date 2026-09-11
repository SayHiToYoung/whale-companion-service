# -*- coding: utf-8 -*-
"""核心客户端 API 的正式契约：机器可读文档、稳定错误码与客户端身份。

这个模块只描述"网线上传的是什么"，不含任何陪伴逻辑。SQLite 仍是唯一权威状态，
ContextAssembler / CompanionFrame / 记忆 / 关系 / 情绪 / 主动候选算法一行未动。

三件事在这里定死，其它地方只引用不复制：

1. `CONTRACT` —— 一份 OpenAPI 3.1 文档。3.1 的 schema 对象就是 JSON Schema，
   所以同一份文件既是接口文档，也是 `validate_instance()` 能直接执行的校验规则。
   契约测试拿真实响应打这份 schema，schema 与实现因此无法各走各的。
2. `ERROR_CATALOG` —— 稳定 `code` → (HTTP 状态, 是否可重试, 可读 message)。
   客户端按 `code` 分支，按 `retryable` 决定重不重试，永远不解析人话。
3. `normalize_client()` —— 可选的客户端身份与 capabilities 声明。
   旧客户端一个新字段都不发，照样是完全合法的请求。
"""
from __future__ import annotations

from typing import Any


# 客户端契约版本。与 memory protocol v1 是两条独立的版本线：
# protocol v1 管的是桌宠上传记忆的批次格式，这里管的是所有客户端共用的对话面。
CLIENT_API_VERSION = 1
CONTRACT_PATH = "/v1/contract"

# 分页上限，服务端与客户端共用同一个来源。
MAX_PAGE_LIMIT = 500
DEFAULT_PAGE_LIMIT = 200

# 标识符长度上限，与仓库里的既有校验保持一致。
MAX_IDENTIFIER_LENGTH = 128
MAX_MESSAGE_TEXT_LENGTH = 4000
MAX_CAPABILITIES = 32

# 服务端自己支持什么。客户端可以读它来决定要不要走某条路径，
# 但服务端永远不要求客户端声明任何 capability。
SERVER_CAPABILITIES = (
    "conversation.history",        # GET /v1/conversation/messages 增量游标
    "conversation.hasMoreExact",   # 分页返回精确的 hasMoreExact 信号
    "conversation.idempotency",    # messageId 重放与冲突检测
    "proactive.deliveries",        # 主动消息的唯一落定入口
    "proactive.readonlyDecision",  # 只读评估，轮询不改状态
    "openings.claim",              # 开场原子领取
    "errors.envelope.v1",          # code / message / retryable 错误信封
    "client.identity.v1",          # 可选客户端身份与 capabilities
    "errors.retryAfter.v1",        # 可重试错误可带 retryAfterSeconds / Retry-After
    "perception.observations.v1",  # 可选外部感知输入（桌宠、日历、天气……）
)

# 客户端可以声明的 capabilities。声明未知能力不是错误——服务端原样记下，
# 未来的客户端因此不需要等服务端先发版。
KNOWN_CLIENT_CAPABILITIES = (
    "conversation.send",
    "conversation.history",
    "proactive.receive",
    "openings.claim",
    "memory.upload",
    "presence.report",
    "push.background",
)

CLIENT_KINDS = ("web", "mobile", "desktopPet", "console", "agent", "other")


# --------------------------------------------------------------------------
# 错误目录
# --------------------------------------------------------------------------

def _entry(status: int, retryable: bool, message: str, retry_after: float = 0.0) -> dict:
    """一个错误码的全部语义：HTTP 状态、值不值得重试、可读说明、建议等多久。

    `retryable` 由**这个错误自己的语义**决定，不是由状态码的区间决定。
    绝大多数 4xx 是"请求本身有问题"，重试永远不会好；但 408 Request Timeout 和
    429 Too Many Requests 讲的是"此刻不行，等一下再来"——它们是可重试的 4xx。
    这里留出这条口子，是为了将来加限流时不必推翻整套重试语义。
    """
    return {"status": status, "retryable": retryable, "message": message,
            "retryAfterSeconds": float(retry_after)}


ERROR_CATALOG: dict[str, dict] = {
    # 请求层
    "request.unauthorized": _entry(401, False, "缺少或错误的 Bearer token"),
    "request.not_found": _entry(404, False, "没有这个接口"),
    "request.invalid_body": _entry(400, False, "请求体必须是 JSON 对象"),
    "request.body_too_large": _entry(400, False, "请求体超出大小限制"),
    "request.invalid_query": _entry(400, False, "查询参数不合法"),
    "request.user_id_required": _entry(400, False, "缺少 userId"),
    "request.invalid_client": _entry(400, False, "client 身份声明不合法"),
    # 可重试的 4xx。语义是"此刻不行，等一下再来"，不是"请求有问题"。
    # 目前没有任何路径抛它们；先把语义和 Retry-After 通道定死，
    # 将来接限流或超时时客户端不必改一行重试逻辑。
    "request.timeout": _entry(408, True, "服务端等待请求体超时，可以带同一个幂等键重试", 1.0),
    "request.rate_limited": _entry(429, True, "请求太频繁，按 Retry-After 退避后重试", 5.0),
    # 对话
    "conversation.invalid_message": _entry(400, False, "对话消息字段不合法"),
    "conversation.invalid_text": _entry(400, False, "对话正文为空或超长"),
    "conversation.message_id_conflict": _entry(
        409, False, "同一个 messageId 已经用于另一段内容，换一个新的 messageId"),
    # 主动陪伴
    "proactive.invalid_delivery": _entry(400, False, "主动投递字段不合法"),
    # 开场领取
    "openings.invalid_claim": _entry(400, False, "开场领取字段不合法"),
    # 可选感知输入
    "perception.invalid_observation": _entry(400, False, "感知观察字段不合法"),
    # 通用兜底
    "bad_request": _entry(400, False, "请求不合法"),
    "conflict": _entry(409, False, "与已有状态冲突"),
    "service_unavailable": _entry(503, True, "服务暂时不可用，稍后重试"),
    "internal_error": _entry(500, True, "服务内部错误，可以重试"),
}


class ApiError(Exception):
    """带稳定 code 的错误。抛它就等于选定了 HTTP 状态和 retryable。"""

    def __init__(self, code: str, detail: str = "", details: dict | None = None) -> None:
        resolved = code if code in ERROR_CATALOG else "internal_error"
        entry = ERROR_CATALOG[resolved]
        self.code = resolved
        self.status = int(entry["status"])
        self.retryable = bool(entry["retryable"])
        self.retry_after_seconds = float(entry.get("retryAfterSeconds") or 0.0)
        self.detail = str(detail or entry["message"])
        self.details = dict(details or {})
        super().__init__(self.detail)


# 既有 `ProtocolError` / `MemoryConflictError` 的原文 → 稳定 code。
# 这样做而不是去改上百处 raise：核心六个接口的错误码立刻稳定下来，
# 与本阶段无关的算法模块一行都不用碰。
_MESSAGE_CODES: dict[str, str] = {
    "invalid conversation message": "conversation.invalid_message",
    "invalid conversation text": "conversation.invalid_text",
    "messageId already exists with different payload": "conversation.message_id_conflict",
    "invalid proactive delivery": "proactive.invalid_delivery",
    "invalid opening claim": "openings.invalid_claim",
    "invalid perception observation": "perception.invalid_observation",
    "userId required": "request.user_id_required",
    "invalid JSON body": "request.invalid_body",
    "body must be an object": "request.invalid_body",
    "invalid Content-Length": "request.invalid_body",
    "invalid request size": "request.body_too_large",
}


def code_for_message(detail: str, *, fallback: str) -> str:
    """把既有错误原文映射成稳定 code；映射不到就退到该状态的通用 code。"""
    return _MESSAGE_CODES.get(str(detail or "").strip(), fallback)


def error_envelope(code: str, *, detail: str = "", details: dict | None = None) -> tuple[int, dict]:
    """构造统一错误响应。

    顶层 `error` 保留原来的自由文本字符串——现有的 PWA、调试台和 Python 客户端
    都在读它，删掉就是一次静默的破坏。新字段是叠加上去的。
    """
    resolved = code if code in ERROR_CATALOG else "internal_error"
    entry = ERROR_CATALOG[resolved]
    text = str(detail or "").strip() or str(entry["message"])
    payload = {
        "error": text,                # 向后兼容：旧客户端只认这一个字段
        "code": resolved,
        "message": str(entry["message"]),
        "retryable": bool(entry["retryable"]),
        "status": int(entry["status"]),
        "apiVersion": CLIENT_API_VERSION,
    }
    # 只有真的建议等一会儿的错误才带这个字段。旧客户端看不见它也完全正常：
    # `retryable` 仍然是唯一的重试依据，退避多久由客户端自己决定。
    if entry.get("retryAfterSeconds"):
        payload["retryAfterSeconds"] = float(entry["retryAfterSeconds"])
    if details:
        payload["details"] = dict(details)
    return int(entry["status"]), payload


def retry_after_seconds(code: str) -> float:
    """这个错误建议等多久再重试。0 表示服务端没有建议（客户端自己退避）。"""
    entry = ERROR_CATALOG.get(code if code in ERROR_CATALOG else "internal_error")
    return float(entry.get("retryAfterSeconds") or 0.0)


# --------------------------------------------------------------------------
# 客户端身份与 capabilities（全部可选）
# --------------------------------------------------------------------------

def _clean_token(value: Any, limit: int = MAX_IDENTIFIER_LENGTH) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > limit or any(ord(char) < 32 for char in text):
        raise ApiError("request.invalid_client", "client 字段不合法")
    return text


def _int_or_zero(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if 0 <= parsed <= 9999 else 0


def normalize_client(value: Any, *, headers: Any = None) -> dict:
    """把可选的客户端声明归一化成固定形状。

    没有任何一个字段是必填的。`normalize_client(None)` 返回一份空身份，
    这正是所有旧客户端走的路径——它们不发 `client`，行为与从前逐字节相同。

    请求体里的 `client` 优先于请求头；请求头存在是为了 GET 也能带身份。
    """
    source: dict = {}
    if isinstance(value, dict):
        source = value
    elif value not in (None, ""):
        raise ApiError("request.invalid_client", "client 必须是对象")

    def header(name: str) -> str:
        if headers is None:
            return ""
        try:
            return str(headers.get(name, "") or "")
        except Exception:
            return ""

    capabilities_raw = source.get("capabilities")
    if capabilities_raw is None:
        header_caps = header("X-Whale-Client-Capabilities")
        capabilities_raw = [part for part in header_caps.replace(",", " ").split() if part]
    if not isinstance(capabilities_raw, (list, tuple)):
        raise ApiError("request.invalid_client", "client.capabilities 必须是数组")
    if len(capabilities_raw) > MAX_CAPABILITIES:
        raise ApiError("request.invalid_client", "client.capabilities 太多")
    capabilities = sorted({_clean_token(item, 64) for item in capabilities_raw} - {""})

    kind = _clean_token(source.get("kind") or header("X-Whale-Client-Kind"), 32)
    if kind and kind not in CLIENT_KINDS:
        # 未知客户端类型不是错误：未来的客户端不该被今天的枚举挡在门外。
        kind = "other"

    return {
        "clientId": _clean_token(source.get("clientId") or header("X-Whale-Client-Id")),
        "kind": kind,
        "version": _clean_token(source.get("version") or header("X-Whale-Client-Version"), 64),
        "capabilities": capabilities,
        "apiVersion": _int_or_zero(source.get("apiVersion") or header("X-Whale-Api-Version")),
    }


def client_is_declared(client: dict) -> bool:
    """这次请求到底有没有声明身份。没声明就不写库，旧客户端不留痕。"""
    return bool(client.get("clientId") or client.get("kind") or client.get("capabilities"))


# --------------------------------------------------------------------------
# 极小 JSON Schema 子集校验器
# --------------------------------------------------------------------------
# 本服务运行时零第三方依赖，所以不引入 jsonschema。支持的关键字刻意只有下面这些，
# 契约文档也只用这些关键字写——校验器读不懂的东西不会悄悄通过，会直接报错。

_SUPPORTED_KEYWORDS = frozenset({
    "$ref", "type", "properties", "required", "additionalProperties", "items",
    "enum", "const", "anyOf", "minimum", "maximum", "minLength", "maxLength",
    "description", "example", "default", "title", "nullable",
})

_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


class SchemaError(AssertionError):
    """实例与契约不符。测试拿它当断言失败读。"""


def _resolve(schema: dict, root: dict) -> dict:
    seen = 0
    while "$ref" in schema:
        seen += 1
        if seen > 16:
            raise SchemaError("$ref 链过深")
        pointer = str(schema["$ref"])
        if not pointer.startswith("#/"):
            raise SchemaError(f"只支持文档内 $ref：{pointer}")
        target: Any = root
        for part in pointer[2:].split("/"):
            if not isinstance(target, dict) or part not in target:
                raise SchemaError(f"$ref 指向不存在的位置：{pointer}")
            target = target[part]
        if not isinstance(target, dict):
            raise SchemaError(f"$ref 目标不是 schema：{pointer}")
        schema = target
    return schema


def validate_instance(schema: dict, instance: Any, *, root: dict | None = None, path: str = "$") -> None:
    """按契约校验一个实例；不符就抛 `SchemaError`，信息里带出错路径。"""
    root = root if root is not None else schema
    schema = _resolve(schema, root)

    unknown = set(schema) - _SUPPORTED_KEYWORDS
    if unknown:
        raise SchemaError(f"{path}: 契约里用了校验器不支持的关键字 {sorted(unknown)}")

    if "anyOf" in schema:
        errors = []
        for index, option in enumerate(schema["anyOf"]):
            try:
                validate_instance(option, instance, root=root, path=f"{path}#anyOf[{index}]")
                break
            except SchemaError as exc:
                errors.append(str(exc))
        else:
            raise SchemaError(f"{path}: 不满足任何一个 anyOf 分支：{errors}")

    if "const" in schema and instance != schema["const"]:
        raise SchemaError(f"{path}: 期望常量 {schema['const']!r}，实际 {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{path}: {instance!r} 不在枚举 {schema['enum']} 中")

    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else list(declared)
        for name in names:
            if name not in _TYPE_CHECKS:
                raise SchemaError(f"{path}: 未知类型 {name!r}")
        if not any(_TYPE_CHECKS[name](instance) for name in names):
            raise SchemaError(f"{path}: 期望类型 {names}，实际 {type(instance).__name__}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaError(f"{path}: 字符串过短")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise SchemaError(f"{path}: 字符串过长")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(f"{path}: {instance} 小于最小值 {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaError(f"{path}: {instance} 大于最大值 {schema['maximum']}")

    if isinstance(instance, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or ():
            if name not in instance:
                raise SchemaError(f"{path}: 缺少必填字段 {name!r}")
        for name, value in instance.items():
            if name in properties:
                validate_instance(properties[name], value, root=root, path=f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                raise SchemaError(f"{path}: 出现了契约未声明的字段 {name!r}")

    if isinstance(instance, list) and "items" in schema:
        for index, value in enumerate(instance):
            validate_instance(schema["items"], value, root=root, path=f"{path}[{index}]")


def response_schema(path: str, method: str, status: int = 200) -> dict:
    """取某个接口某个状态码的响应 schema，供契约测试直接使用。"""
    operation = (CONTRACT["paths"].get(path) or {}).get(method.lower())
    if not operation:
        raise SchemaError(f"契约里没有 {method.upper()} {path}")
    response = (operation.get("responses") or {}).get(str(status))
    if not response:
        raise SchemaError(f"契约里没有 {method.upper()} {path} 的 {status} 响应")
    return response["content"]["application/json"]["schema"]


def request_schema(path: str, method: str) -> dict:
    operation = (CONTRACT["paths"].get(path) or {}).get(method.lower())
    if not operation or "requestBody" not in operation:
        raise SchemaError(f"契约里没有 {method.upper()} {path} 的请求体")
    return operation["requestBody"]["content"]["application/json"]["schema"]


def validate_response(path: str, method: str, instance: Any, status: int = 200) -> None:
    validate_instance(response_schema(path, method, status), instance, root=CONTRACT)


def validate_request(path: str, method: str, instance: Any) -> None:
    validate_instance(request_schema(path, method), instance, root=CONTRACT)


# --------------------------------------------------------------------------
# 契约文档（OpenAPI 3.1，schema 部分即 JSON Schema）
# --------------------------------------------------------------------------

def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def _json(schema: dict) -> dict:
    return {"content": {"application/json": {"schema": schema}}}


_ERROR_RESPONSES = {
    "400": {"description": "请求不合法，不要重试", **_json(_ref("ErrorResponse"))},
    "401": {"description": "鉴权失败，不要重试", **_json(_ref("ErrorResponse"))},
    # 两个**可重试**的 4xx。当前没有路径会返回它们；先写进契约，
    # 是为了让客户端现在就按 retryable 分支，而不是按状态码区间猜。
    "408": {"description": "请求超时，可以带同一个幂等键重试", **_json(_ref("ErrorResponse"))},
    "409": {"description": "幂等键冲突，换新 id 而不是重试", **_json(_ref("ErrorResponse"))},
    "429": {"description": "请求太频繁，按 Retry-After 退避后重试", **_json(_ref("ErrorResponse"))},
    "500": {"description": "服务内部错误，可以重试", **_json(_ref("ErrorResponse"))},
}

_USER_ID_PARAM = {
    "name": "userId", "in": "query", "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH},
    "description": "会话所有者。所有客户端共享同一个 userId 才能看到同一份历史。",
}

_CLIENT_HEADERS = [
    {"name": "X-Whale-Client-Id", "in": "header", "required": False,
     "schema": {"type": "string", "maxLength": MAX_IDENTIFIER_LENGTH},
     "description": "可选客户端身份。不发即匿名，行为与旧客户端完全一致。"},
    {"name": "X-Whale-Client-Kind", "in": "header", "required": False,
     "schema": {"type": "string", "enum": list(CLIENT_KINDS)},
     "description": "可选客户端类型。未知取值归一为 other，不会被拒绝。"},
    {"name": "X-Whale-Client-Version", "in": "header", "required": False,
     "schema": {"type": "string", "maxLength": 64}},
    {"name": "X-Whale-Client-Capabilities", "in": "header", "required": False,
     "schema": {"type": "string"},
     "description": "空格或逗号分隔的 capabilities 声明。服务端只记录，从不要求。"},
]

CONTRACT: dict = {
    "openapi": "3.1.0",
    "info": {
        "title": "whale-companion-service 客户端 API",
        "version": f"{CLIENT_API_VERSION}.0.0",
        "summary": "Web、手机 PWA、可选桌宠与其它客户端共用的唯一对话面。",
        "description": (
            "所有客户端共享同一套对话接口，不存在第二套聊天通道。\n\n"
            "幂等语义：\n"
            "- `messageId` 由客户端生成，作用域 `(userId)`。重试同一个 id 与同一段内容拿到同一条回复；"
            "同一个 id 配不同内容返回 409 `conversation.message_id_conflict`，不可重试。\n"
            "- `deliveryId` 由客户端生成，作用域 `(userId)`。重放拿到逐字节相同的结果，"
            "一条主动消息不会被记成两条。\n"
            "- `claimId` 由客户端生成，作用域 `(userId, deviceId)`。重放追加 `duplicateClaim: true`。\n"
            "- `afterMessageSeq` 是服务端分配的排他游标，全用户单调递增、跨客户端共享。"
            "断线重连时带上最后一次的 `nextMessageSeq` 即可续读。\n\n"
            "翻页信号：`hasMoreExact` 精确（服务端多取一条判定），新客户端只看它即可；"
            "`hasMore` 是语义不变的保守信号，取满一页就为 true，保留给旧客户端。\n\n"
            "重试规则：**只看 `retryable`，不要按状态码区间推断**。"
            "`retryable: false` 意味着换请求，不是换时间；`retryable: true` 就退避后带"
            "同一个幂等键原样重放。绝大多数 4xx 不可重试，但 408 与 429 是可重试的 4xx——"
            "它们说的是「此刻不行」而不是「请求有问题」。"
            "带 `retryAfterSeconds`（响应头 `Retry-After`）时按它退避，没有就用客户端自己的退避。"
        ),
    },
    "servers": [{"url": "http://127.0.0.1:47821", "description": "默认本机地址"}],
    "paths": {
        "/health": {
            "get": {
                "operationId": "health",
                "summary": "服务就绪状态与 API 能力声明（无需鉴权）",
                "description": "不探测、不等待任何可选客户端。桌宠缺席不影响 ok。",
                "security": [],
                "responses": {
                    "200": {"description": "就绪", **_json(_ref("HealthResponse"))},
                    "503": {"description": "存储不可用", **_json(_ref("HealthResponse"))},
                },
            },
        },
        CONTRACT_PATH: {
            "get": {
                "operationId": "contract",
                "summary": "返回这份契约文档本身（无需鉴权）",
                "security": [],
                "responses": {"200": {"description": "OpenAPI 3.1 文档",
                                      **_json({"type": "object"})}},
            },
        },
        "/v1/conversation/messages": {
            "post": {
                "operationId": "postMessage",
                "summary": "写入一条用户消息并同轮拿到回复",
                "parameters": list(_CLIENT_HEADERS),
                "requestBody": {"required": True, **_json(_ref("PostMessageRequest"))},
                "responses": {
                    "200": {"description": "已写入或重放", **_json(_ref("PostMessageResponse"))},
                    **_ERROR_RESPONSES,
                },
            },
            "get": {
                "operationId": "listMessages",
                "summary": "按游标增量读取共享对话历史",
                "parameters": [
                    _USER_ID_PARAM,
                    {"name": "afterMessageSeq", "in": "query", "required": False,
                     "schema": {"type": "integer", "minimum": 0, "default": 0},
                     "description": "排他游标。传上一次的 nextMessageSeq 续读。"},
                    {"name": "limit", "in": "query", "required": False,
                     "schema": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_LIMIT,
                                "default": DEFAULT_PAGE_LIMIT}},
                    *_CLIENT_HEADERS,
                ],
                "responses": {
                    "200": {"description": "一页历史", **_json(_ref("MessagePage"))},
                    **_ERROR_RESPONSES,
                },
            },
        },
        "/v1/companion/proactive": {
            "get": {
                "operationId": "proactiveDecision",
                "summary": "只读评估主动开口意愿",
                "description": "轮询多少次都不改变任何状态，也不算作已经说过。",
                "parameters": [
                    _USER_ID_PARAM,
                    {"name": "activity", "in": "query", "required": False,
                     "schema": {"type": "string", "default": "idle"}},
                    *_CLIENT_HEADERS,
                ],
                "responses": {
                    "200": {"description": "评估结果", **_json(_ref("ProactiveDecision"))},
                    **_ERROR_RESPONSES,
                },
            },
        },
        "/v1/companion/proactive/deliveries": {
            "post": {
                "operationId": "commitProactiveDelivery",
                "summary": "评估并落定一条主动消息（唯一落定入口）",
                "description": "同一个 deliveryId 重放拿到同一份结果，不会产生第二条主动消息。",
                "parameters": list(_CLIENT_HEADERS),
                "requestBody": {"required": True, **_json(_ref("ProactiveDeliveryRequest"))},
                "responses": {
                    "200": {"description": "已落定或重放", **_json(_ref("ProactiveDelivery"))},
                    **_ERROR_RESPONSES,
                },
            },
        },
        "/v1/perception/observations": {
            "post": {
                "operationId": "ingestPerception",
                "summary": "上报一批外部感知观察（完全可选）",
                "description": (
                    "整条链路是可选的：一个来源都不接，聊天、关系、主动陪伴一切照旧。\n"
                    "`observationId` 是幂等键，作用域 `(userId)`，重传不会多出第二条。\n"
                    "`payload` 原样落存储，**不直接进模型**：进上下文的只有按 `sourceType` "
                    "白名单投影出来的最小事实，且来源恒为观察，不会升格成用户亲口说过的事。"),
                "parameters": list(_CLIENT_HEADERS),
                "requestBody": {"required": True, **_json(_ref("PerceptionIngestRequest"))},
                "responses": {
                    "200": {"description": "已收下或去重", **_json(_ref("PerceptionIngestResponse"))},
                    **_ERROR_RESPONSES,
                },
            },
        },
        "/v1/perception/state": {
            "get": {
                "operationId": "perceptionState",
                "summary": "只读：来源状态与本轮投影/丢弃轨迹",
                "description": (
                    "轮询不改变任何状态，也不含任何原始 payload。\n"
                    "`sources[].state` 描述的是**来源**，不是用户：`live` 只表示这个来源刚刚"
                    "还在报数，不表示用户在电脑前、有空或者醒着。"),
                "parameters": [_USER_ID_PARAM, *_CLIENT_HEADERS],
                "responses": {
                    "200": {"description": "来源状态", **_json(_ref("PerceptionState"))},
                    **_ERROR_RESPONSES,
                },
            },
        },
        "/v1/companion/openings/claim": {
            "post": {
                "operationId": "claimOpening",
                "summary": "原子领取一次开场",
                "parameters": list(_CLIENT_HEADERS),
                "requestBody": {"required": True, **_json(_ref("OpeningClaimRequest"))},
                "responses": {
                    "200": {"description": "领取结果或重放", **_json(_ref("OpeningClaim"))},
                    **_ERROR_RESPONSES,
                },
            },
        },
    },
    "components": {
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer",
                           "description": "配对口令。/health 与 /v1/contract 之外全部必需。"},
        },
        "schemas": {
            "ClientIdentity": {
                "type": "object",
                "title": "可选客户端身份",
                "description": "整个对象可以不发。不发时服务端按匿名旧客户端处理。",
                "properties": {
                    "clientId": {"type": "string", "maxLength": MAX_IDENTIFIER_LENGTH,
                                 "description": "客户端安装实例的稳定标识。"},
                    "kind": {"type": "string", "enum": list(CLIENT_KINDS)},
                    "version": {"type": "string", "maxLength": 64},
                    "apiVersion": {"type": "integer", "minimum": 0, "maximum": 9999,
                                   "description": "客户端按哪一版契约实现。"},
                    "capabilities": {"type": "array", "items": {"type": "string", "maxLength": 64},
                                     "description": "声明式，非强制。服务端只记录。"},
                },
            },
            "ErrorResponse": {
                "type": "object",
                "title": "统一错误信封",
                "description": "`error` 是给旧客户端的兼容字段；新客户端读 `code` 与 `retryable`。",
                "required": ["error", "code", "message", "retryable", "status", "apiVersion"],
                "properties": {
                    "error": {"type": "string", "description": "自由文本，向后兼容保留。"},
                    "code": {"type": "string", "enum": sorted(ERROR_CATALOG)},
                    "message": {"type": "string", "description": "稳定的可读说明。"},
                    "retryable": {"type": "boolean", "description": "false 表示重试永远不会成功。"},
                    "status": {"type": "integer", "minimum": 400, "maximum": 599},
                    "apiVersion": {"type": "integer"},
                    "retryAfterSeconds": {"type": "number", "minimum": 0,
                                          "description": (
                                              "建议退避多久，与响应头 Retry-After 同值。"
                                              "只在服务端确实有建议时出现；旧客户端可以忽略它。")},
                    "details": {"type": "object"},
                },
            },
            "HealthResponse": {
                "type": "object",
                "required": ["ok", "status", "service", "protocolVersion", "storage",
                             "chat", "clients", "optionalInputs", "companion", "api"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "status": {"type": "string", "enum": ["ready", "unavailable"]},
                    "service": {"type": "string"},
                    "protocolVersion": {"type": "integer",
                                        "description": "memory protocol 版本，与客户端 API 版本无关。"},
                    "storage": {"type": "object", "required": ["ok", "kind"], "properties": {
                        "ok": {"type": "boolean"}, "kind": {"type": "string"}}},
                    "chat": {"type": "object",
                             "required": ["ok", "mode", "contextAssembler", "companionFrameVersion"],
                             "properties": {
                                 "ok": {"type": "boolean"},
                                 "mode": {"type": "string", "enum": ["model", "grounded_fallback"]},
                                 "contextAssembler": {"type": "string"},
                                 "companionFrameVersion": {"type": "string"}}},
                    "clients": {"type": "object",
                                "required": ["web", "mobile", "desktopPet", "other"],
                                "properties": {
                                    "web": {"type": "string"}, "mobile": {"type": "string"},
                                    "desktopPet": {"type": "string", "const": "optional"},
                                    "other": {"type": "string"}}},
                    "optionalInputs": {"type": "object"},
                    "companion": {"type": "object",
                                  "required": ["modelEnabled", "model", "fallback",
                                               "promptVersion", "lastReplySource", "lastReplyAt"],
                                  "properties": {
                                      "modelEnabled": {"type": "boolean"},
                                      "model": {"type": "string"},
                                      "fallback": {"type": "string"},
                                      "promptVersion": {"type": "string"},
                                      "lastReplySource": {"type": "string"},
                                      "lastReplyAt": {"type": "string"},
                                      "lastRejectionCode": {"type": "string"},
                                      "lastRejectionStage": {"type": "string"},
                                      "diagnostics": {"type": "object"}}},
                    "api": {"$ref": "#/components/schemas/ApiDescriptor"},
                },
            },
            "ApiDescriptor": {
                "type": "object",
                "required": ["version", "contract", "capabilities", "errorCodes",
                             "identityOptional", "limits"],
                "properties": {
                    "version": {"type": "integer", "const": CLIENT_API_VERSION},
                    "contract": {"type": "string", "const": CONTRACT_PATH},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "errorCodes": {"type": "array", "items": {"type": "string"}},
                    "identityOptional": {"type": "boolean", "const": True,
                                         "description": "永远为 true：身份声明不是接入前提。"},
                    "limits": {"type": "object",
                               "required": ["maxPageLimit", "defaultPageLimit",
                                            "maxMessageTextLength", "maxIdentifierLength"],
                               "properties": {
                                   "maxPageLimit": {"type": "integer"},
                                   "defaultPageLimit": {"type": "integer"},
                                   "maxMessageTextLength": {"type": "integer"},
                                   "maxIdentifierLength": {"type": "integer"}}},
                },
            },
            "ConversationMessage": {
                "type": "object",
                "required": ["messageSeq", "deviceId", "messageId", "role", "text", "createdAt"],
                "properties": {
                    "messageSeq": {"type": "integer", "minimum": 1,
                                   "description": "服务端分配的全序号，游标就走它。"},
                    "deviceId": {"type": "string"},
                    "messageId": {"type": "string"},
                    "role": {"type": "string", "enum": ["user", "assistant"]},
                    "text": {"type": "string"},
                    "createdAt": {"type": "string"},
                },
            },
            "AssistantMessage": {
                "type": ["object", "null"],
                "required": ["messageId", "role", "text", "createdAt"],
                "properties": {
                    "messageId": {"type": "string"},
                    "role": {"type": "string", "const": "assistant"},
                    "text": {"type": "string"},
                    "createdAt": {"type": "string"},
                },
            },
            "PostMessageRequest": {
                "type": "object",
                "required": ["userId", "deviceId", "messageId", "role", "text"],
                "properties": {
                    "userId": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH},
                    "deviceId": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH},
                    "messageId": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH,
                                  "description": "客户端生成的幂等键，作用域 (userId)。"},
                    "role": {"type": "string", "enum": ["user", "assistant"]},
                    "text": {"type": "string", "minLength": 1, "maxLength": MAX_MESSAGE_TEXT_LENGTH},
                    "client": {"$ref": "#/components/schemas/ClientIdentity"},
                },
            },
            "PostMessageResponse": {
                "type": "object",
                "required": ["accepted", "duplicate", "messageId", "emotionRecorded",
                             "emotionMemoryId", "lifecycleUpdates", "assistantMessage", "replySource"],
                "properties": {
                    "accepted": {"type": "boolean"},
                    "duplicate": {"type": "boolean",
                                  "description": "true 表示这是同一个 messageId 的重放，没有产生新回复。"},
                    "messageId": {"type": "string"},
                    "emotionRecorded": {"type": "boolean"},
                    "emotionMemoryId": {"type": "string"},
                    "lifecycleUpdates": {"type": "array", "items": {"type": "object"}},
                    "assistantMessage": {"$ref": "#/components/schemas/AssistantMessage"},
                    "replySource": {"type": "string", "enum": ["", "model", "fallback", "stored"]},
                },
            },
            "MessagePage": {
                "type": "object",
                "required": ["messages", "nextMessageSeq", "hasMore", "hasMoreExact"],
                "properties": {
                    "messages": {"type": "array",
                                 "items": {"$ref": "#/components/schemas/ConversationMessage"}},
                    "nextMessageSeq": {"type": "integer", "minimum": 0,
                                       "description": "下次请求的 afterMessageSeq。空页时原样回传入参。"},
                    "hasMore": {"type": "boolean",
                                "description": (
                                    "保守信号，语义不变：取满一页即为 true，哪怕后面已经没有了。"
                                    "只按它翻页的客户端要同时要求游标前进，"
                                    "否则最后一页刚好取满时会空转一轮。新客户端请改用 hasMoreExact。")},
                    "hasMoreExact": {"type": "boolean",
                                     "description": (
                                         "精确信号：确实还存在下一条时才为 true"
                                         "（服务端多取一条判定）。最后一页刚好取满时为 false。"
                                         "旧客户端可以完全忽略它。")},
                },
            },
            "ProactiveCandidate": {
                "type": "object",
                "required": ["signature", "kind", "decision"],
                "properties": {
                    "signature": {"type": "string"},
                    "kind": {"type": "string"},
                    "content": {"type": "string"},
                    "decision": {"type": "string", "enum": ["send", "defer", "drop"]},
                    "vetoReason": {"type": "string"},
                    "status": {"type": "string"},
                },
            },
            "ProactiveDecision": {
                "type": "object",
                "description": "只读评估。`selected` 非空也不代表已经发出去了。",
                "required": ["shouldSpeak", "decision", "selected", "vetoReason",
                             "checkedAt", "candidates", "permissions", "agenda", "pressure"],
                "properties": {
                    "shouldSpeak": {"type": "boolean"},
                    "decision": {"type": "string", "enum": ["send", "defer", "drop"]},
                    "selected": {"type": ["object", "null"]},
                    "vetoReason": {"type": "string"},
                    "checkedAt": {"type": "string"},
                    "candidates": {"type": "array",
                                   "items": {"$ref": "#/components/schemas/ProactiveCandidate"}},
                    "permissions": {"type": "object"},
                    "agenda": {"type": "object"},
                    "pressure": {"type": "object"},
                    "kind": {"type": "string", "description": "选中候选的展平副本，向后兼容。"},
                    "reason": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
            "ProactiveDeliveryRequest": {
                "type": "object",
                "required": ["userId", "deliveryId"],
                "properties": {
                    "userId": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH},
                    "deliveryId": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH,
                                   "description": "客户端生成的幂等键，作用域 (userId)。重放不产生第二条主动消息。"},
                    "deviceId": {"type": "string", "maxLength": MAX_IDENTIFIER_LENGTH},
                    "activity": {"type": "string", "description": "在场状态，如 idle/active/meeting/gaming/focus。"},
                    "client": {"$ref": "#/components/schemas/ClientIdentity"},
                },
            },
            "ProactiveDelivery": {
                "type": "object",
                "description": "在只读评估之上多出落定结果。",
                "required": ["shouldSpeak", "decision", "selected", "vetoReason", "checkedAt",
                             "candidates", "permissions", "agenda", "pressure",
                             "assistantMessage", "deliveryId", "committedAt"],
                "properties": {
                    "shouldSpeak": {"type": "boolean"},
                    "decision": {"type": "string", "enum": ["send", "defer", "drop"]},
                    "selected": {"type": ["object", "null"]},
                    "vetoReason": {"type": "string"},
                    "checkedAt": {"type": "string"},
                    "candidates": {"type": "array",
                                   "items": {"$ref": "#/components/schemas/ProactiveCandidate"}},
                    "permissions": {"type": "object"},
                    "agenda": {"type": "object"},
                    "pressure": {"type": "object"},
                    "assistantMessage": {"$ref": "#/components/schemas/AssistantMessage"},
                    "deliveryId": {"type": "string"},
                    "committedAt": {"type": "string"},
                    "kind": {"type": "string"},
                    "reason": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
            "PerceptionObservation": {
                "type": "object",
                "title": "一条外部观察",
                "description": "七件事必须说清楚：谁报的、哪一类、何时看到、何时收到、何时过期、多确定、证据是什么。",
                "required": ["observationId", "sourceId", "sourceType"],
                "properties": {
                    "observationId": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH,
                                      "description": "客户端生成的幂等键，作用域 (userId)。"},
                    "sourceId": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH,
                                 "description": "来源实例，如某一台桌宠、某一个日历账号。"},
                    "sourceType": {"type": "string", "minLength": 1, "maxLength": 64,
                                   "description": (
                                       "来源类别，决定字段白名单与默认新鲜期。"
                                       "未知类别会被收下但不投影进上下文——未知不等于放行。")},
                    "observedAt": {"type": "string",
                                   "description": "被看到的时刻。缺省取 receivedAt。新鲜度只按它算。"},
                    "expiresAt": {"type": "string",
                                  "description": "显式过期时刻；不给就按 freshnessSeconds 推。"},
                    "freshnessSeconds": {"type": "number", "minimum": 0,
                                         "description": "新鲜期。不给就用该 sourceType 的默认值。"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                                   "description": "来源自述置信度；投影时仍受该类型的上限压制。"},
                    "payload": {"type": "object",
                                "description": "原始证据。只进存储，不直接进模型。"},
                    "evidenceRef": {"type": "string", "maxLength": 512,
                                    "description": "指向外部证据（截图、事件 id）的引用。"},
                },
            },
            "PerceptionIngestRequest": {
                "type": "object",
                "required": ["userId", "observations"],
                "properties": {
                    "userId": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH},
                    "observations": {"type": "array",
                                     "items": {"$ref": "#/components/schemas/PerceptionObservation"}},
                    "client": {"$ref": "#/components/schemas/ClientIdentity"},
                },
            },
            "PerceptionSource": {
                "type": "object",
                "required": ["sourceId", "sourceType", "state", "lastObservedAt", "lastReceivedAt"],
                "properties": {
                    "sourceId": {"type": "string"},
                    "sourceType": {"type": "string"},
                    "state": {"type": "string", "enum": ["live", "idle", "offline"],
                              "description": "来源的状态，不是用户的状态。"},
                    "lastObservedAt": {"type": "string"},
                    "lastReceivedAt": {"type": "string"},
                    "silenceSeconds": {"type": ["number", "null"]},
                    "observationCount": {"type": "integer", "minimum": 0},
                    "freshObservations": {"type": "integer", "minimum": 0},
                },
            },
            "PerceptionTraceEntry": {
                "type": "object",
                "required": ["observationId", "sourceId", "decision"],
                "properties": {
                    "observationId": {"type": "string"},
                    "sourceId": {"type": "string"},
                    "sourceType": {"type": "string"},
                    "observedAt": {"type": "string"},
                    "decision": {"type": "string", "enum": ["projected", "dropped"]},
                    "reason": {"type": "string",
                               "description": "被丢的理由，如 expired / unsupported_source_type。"},
                    "sourceState": {"type": "string"},
                },
            },
            "PerceptionIngestResponse": {
                "type": "object",
                "required": ["accepted", "stored", "duplicates", "sources", "receivedAt"],
                "properties": {
                    "accepted": {"type": "boolean"},
                    "stored": {"type": "integer", "minimum": 0},
                    "duplicates": {"type": "integer", "minimum": 0,
                                   "description": "重传的条数。重传不会多出第二条观察。"},
                    "sources": {"type": "array", "items": {"$ref": "#/components/schemas/PerceptionSource"}},
                    "receivedAt": {"type": "string"},
                },
            },
            "PerceptionState": {
                "type": "object",
                "required": ["userId", "sources", "projected", "dropped", "fragmentCount", "checkedAt"],
                "properties": {
                    "userId": {"type": "string"},
                    "sources": {"type": "array", "items": {"$ref": "#/components/schemas/PerceptionSource"}},
                    "projected": {"type": "array", "items": {"$ref": "#/components/schemas/PerceptionTraceEntry"}},
                    "dropped": {"type": "array", "items": {"$ref": "#/components/schemas/PerceptionTraceEntry"}},
                    "fragmentCount": {"type": "integer", "minimum": 0},
                    "checkedAt": {"type": "string"},
                },
            },
            "OpeningClaimRequest": {
                "type": "object",
                "required": ["userId", "deviceId", "claimId"],
                "properties": {
                    "userId": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH},
                    "deviceId": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH},
                    "claimId": {"type": "string", "minLength": 1, "maxLength": MAX_IDENTIFIER_LENGTH,
                                "description": "客户端生成的幂等键，作用域 (userId, deviceId)。"},
                    "client": {"$ref": "#/components/schemas/ClientIdentity"},
                },
            },
            "OpeningClaim": {
                "type": "object",
                "required": ["shouldSend", "reason"],
                "properties": {
                    "shouldSend": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "duplicateClaim": {"type": "boolean", "description": "重放同一个 claimId 时出现。"},
                    "messageId": {"type": "string"},
                    "text": {"type": "string"},
                    "focusMemoryId": {"type": "string"},
                    "consumedThroughServerSeq": {"type": "integer"},
                    "latestMemory": {"type": ["object", "null"]},
                    "episodeId": {"type": "string"},
                    "episodeRevision": {"type": "integer"},
                    "episodeDate": {"type": "string"},
                    "pendingAssistant": {"type": ["object", "null"]},
                },
            },
        },
    },
    "security": [{"bearerAuth": []}],
}


def api_descriptor() -> dict:
    """`/health` 里那块 API 能力声明。客户端读它就知道能不能升级行为。"""
    return {
        "version": CLIENT_API_VERSION,
        "contract": CONTRACT_PATH,
        "capabilities": list(SERVER_CAPABILITIES),
        "errorCodes": sorted(ERROR_CATALOG),
        "identityOptional": True,
        "limits": {
            "maxPageLimit": MAX_PAGE_LIMIT,
            "defaultPageLimit": DEFAULT_PAGE_LIMIT,
            "maxMessageTextLength": MAX_MESSAGE_TEXT_LENGTH,
            "maxIdentifierLength": MAX_IDENTIFIER_LENGTH,
        },
    }
