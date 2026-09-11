# -*- coding: utf-8 -*-
"""小鲸与共享记忆服务之间的稳定协议、客户端与事实型开场翻译。"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .client_contract import CONTRACT_PATH, DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from .dialogue_state import emotional_dialogue_state, is_emotional_bid, is_explicit_stop
from .memory_lifecycle import lifecycle_transition_from_text


PROTOCOL_VERSION = 1
MAX_BATCH_ITEMS = 20
MAX_MEMORY_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ProtocolError(ValueError):
    pass


class SyncTransportError(RuntimeError):
    """传输或服务端错误。

    消息格式与从前逐字保持不变（`HTTP <code>: <detail>`），旧调用方照旧能读。
    新增的是三个属性：`code` / `retryable` / `status`。客户端按 `retryable`
    决定重不重试，按 `code` 分支，不需要再去解析人话。
    """

    def __init__(self, message: str, *, code: str = "", retryable: bool = False,
                 status: int = 0) -> None:
        super().__init__(message)
        self.code = str(code or "")
        self.retryable = bool(retryable)
        self.status = int(status or 0)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _identifier(value: Any, field: str, limit: int = 128) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        raise ProtocolError(f"invalid {field}")
    return text


def validate_memory(memory: dict) -> dict:
    if not isinstance(memory, dict):
        raise ProtocolError("memory must be an object")
    clean = json.loads(canonical_json(memory))
    clean["id"] = _identifier(clean.get("id"), "memory.id")
    layer = str(clean.get("layer") or "")
    source = str(clean.get("sourceType") or "")
    expected = {"L1": "observed", "L2": "derived", "L3": "user_stated"}
    if layer not in expected or source != expected[layer]:
        raise ProtocolError("memory layer/sourceType mismatch")
    try:
        revision = int(clean.get("revision"))
    except (TypeError, ValueError):
        raise ProtocolError("invalid memory.revision") from None
    if revision < 1:
        raise ProtocolError("invalid memory.revision")
    clean["revision"] = revision
    if len(canonical_json(clean).encode("utf-8")) > MAX_MEMORY_BYTES:
        raise ProtocolError("memory item too large")
    return clean


def validate_batch(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ProtocolError("batch must be an object")
    try:
        version = int(payload.get("protocolVersion"))
    except (TypeError, ValueError):
        raise ProtocolError("invalid protocolVersion") from None
    if version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocolVersion")
    memories = payload.get("memories")
    if not isinstance(memories, list) or not 1 <= len(memories) <= MAX_BATCH_ITEMS:
        raise ProtocolError("memories must contain 1..20 items")
    clean_memories = [validate_memory(item) for item in memories]
    ids = [item["id"] for item in clean_memories]
    if len(ids) != len(set(ids)):
        raise ProtocolError("duplicate memory id inside batch")
    latest_revision = max(item["revision"] for item in clean_memories)
    declared_revision = int(payload.get("latestRevision") or 0)
    if declared_revision != latest_revision:
        raise ProtocolError("latestRevision mismatch")
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "userId": _identifier(payload.get("userId"), "userId"),
        "deviceId": _identifier(payload.get("deviceId"), "deviceId"),
        "batchId": _identifier(payload.get("batchId"), "batchId"),
        "latestRevision": latest_revision,
        "memories": clean_memories,
    }


def make_batch(*, user_id: str, device_id: str, report: dict, memories: list[dict]) -> dict:
    return validate_batch({
        "protocolVersion": PROTOCOL_VERSION,
        "userId": user_id,
        "deviceId": device_id,
        "batchId": report.get("batchId"),
        "latestRevision": report.get("latestRevision"),
        "memories": memories,
    })


def _duration_text(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    minutes = max(1, int(round(seconds / 60.0)))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    rest = minutes % 60
    return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"


def _stable_choice(seed: str, options: tuple[str, ...]) -> str:
    digest = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


_EMOTION_TEXT = {
    "frustrated": "有点烦",
    "angry": "很生气",
    "sad": "有些难过",
    "anxious": "有些焦虑",
    "tired": "很累",
    "happy": "挺开心",
    "excited": "很期待",
    "wronged": "有些委屈",
}


def memory_time(memory: dict) -> str:
    for key in ("occurredAt", "endedAt", "startedAt", "createdAt"):
        value = str(memory.get(key) or "")
        if value:
            return value
    return ""


def build_big_whale_opening(memories: list[dict]) -> tuple[str, str]:
    """只用已存事实生成开场；返回 (文本, 聚焦记忆 ID)。"""
    rows = [row for row in memories if isinstance(row, dict)]
    if not rows:
        return "", ""
    emotions = [row for row in rows if row.get("layer") == "L3" and row.get("label")]
    if emotions:
        latest = max(emotions, key=lambda row: (memory_time(row), int(row.get("revision") or 0)))
        feeling = _EMOTION_TEXT.get(str(latest.get("label")), "有些不舒服")
        return f"我还记得你之前说自己{feeling}。今天不用从头讲，那件事好一点了吗？", str(latest.get("id") or "")

    facts = [row for row in rows if row.get("layer") == "L1"]
    if not facts:
        clue = max(rows, key=lambda row: int(row.get("revision") or 0))
        statement = str(clue.get("statement") or "").strip()
        return (
            f"小鲸注意到一个线索，但还没法确定：{statement}。是这样吗？" if statement else "",
            str(clue.get("id") or ""),
        )

    groups: dict[tuple, dict] = {}
    for fact in facts:
        project = fact.get("project") if isinstance(fact.get("project"), dict) else {}
        key = (
            str(fact.get("context") or "idle"),
            str(fact.get("app") or "未知应用"),
            str(project.get("name") or ""),
            str(fact.get("title") or ""),
        )
        bucket = groups.setdefault(key, {"seconds": 0.0, "latest": fact, "ids": []})
        bucket["seconds"] += max(0.0, float(fact.get("durationSeconds") or 0.0))
        bucket["ids"].append(str(fact.get("id") or ""))
        if (memory_time(fact), int(fact.get("revision") or 0)) > (
            memory_time(bucket["latest"]), int(bucket["latest"].get("revision") or 0)
        ):
            bucket["latest"] = fact
    focus = max(
        groups.values(),
        key=lambda item: (item["seconds"], memory_time(item["latest"]), int(item["latest"].get("revision") or 0)),
    )
    fact = focus["latest"]
    seconds = focus["seconds"]
    duration = _duration_text(seconds)
    context = str(fact.get("context") or "")
    app = str(fact.get("app") or "这个应用").strip()
    title = str(fact.get("title") or "").strip()
    project = fact.get("project") if isinstance(fact.get("project"), dict) else {}
    project_name = str(project.get("name") or "").strip()
    if context == "meeting" or fact.get("kind") == "meeting":
        text = f"小鲸来报信了：你今天开会累计 {duration}。这会挺有存在感的，你想吐槽两句，还是今晚先不聊它？"
    elif context == "gaming":
        subject = title or app
        text = f"你今天玩了 {subject} 大约 {duration}，小鲸记下了。至于战况，我可不乱猜。今天有哪一段值得讲？"
    elif context in {"media", "video", "entertainment"}:
        subject = title or app
        text = f"你今天看了 {subject} 大约 {duration}。小鲸只记到这里，剩下的我想听你说。哪一段最有意思？"
    elif project_name:
        text = f"你今天在 {project_name} 上忙了大约 {duration}，小鲸都记下了。我还不知道进展顺不顺。想说说，还是今晚先不聊工作？"
    else:
        text = f"小鲸说，你今天在 {app} 上花了大约 {duration}。忙完了吗，还是脑子还挂在那里？"
    return text, str(fact.get("id") or "")


def build_grounded_companion_reply(
    user_text: str,
    memories: list[dict],
    *,
    emotion_label: str = "",
    conversation: list[dict] | None = None,
) -> str:
    """生成不越过已知事实的最小陪伴回复。

    这不是通用聊天模型。它只复述用户明确表达的情绪，或引用共享记忆里
    已经存在的事实；信息不足时用好奇的追问承接，不补写项目进度。
    """
    text = str(user_text or "").strip()
    emotion = str(emotion_label or "").strip()
    meeting_words = ("开会", "会议", "例会", "周会", "评审会")
    mentions_meeting = any(word in text for word in meeting_words) or bool(
        re.search(r"开(?:了|过|完)?[^。！？\n]{0,16}会", text)
    )
    lifecycle_transition, _reason = lifecycle_transition_from_text(text)
    if lifecycle_transition == "suppressed":
        return "好，这件事以后不从我这里主动提。"
    if lifecycle_transition == "corrected":
        # 记错了就作废重记，不辩解、也不把错的那条留在原地当背景。
        return "是我记错了，那条我划掉。你说的才算，我按你说的重新记。"
    if lifecycle_transition == "resolved":
        return _stable_choice(text, (
            "那就好。不是非得彻底翻篇，能松一点就很好。",
            "嗯，听到它已经过去一点，我也跟着松口气。",
        ))
    if lifecycle_transition == "dormant":
        return "行，先放下。我不问了。"
    if lifecycle_transition == "active":
        return "嗯，它还没过去。那我不把它当成昨天的事。"

    # “不想说”是边界；单独的一声叹气则更像是在试探有没有人注意到。
    # 两者必须分开，否则陪伴者会在用户最需要被主动接住时后退。
    if is_explicit_stop(text):
        return _stable_choice(text, (
            "好，那就不说。你不用为了让我有话接，硬找点情绪出来。",
            "行，不问了。过来待一会儿就好。",
            "那就先放这儿。什么时候想说了，再从这里继续。",
        ))

    if is_emotional_bid(text):
        return _stable_choice(text, (
            "怎么了，这一声叹得我有点在意，是发生什么了吗？",
            "哎，怎么啦，是碰上什么事了？",
        ))

    if emotion:
        feeling = _EMOTION_TEXT.get(emotion, "有些不舒服")
        if emotion == "frustrated":
            options = (
                "嗯，这种烦先不用讲道理。你想吐槽，我陪你；不想复盘也行。",
                "好，今天先站你这边。最烦的那一段，你想从哪儿说？",
            )
        elif emotion == "tired":
            options = (
                "那就先别撑着讲完整。想说一点就说一点，不想说也行。",
                "累了就先靠一会儿。今晚不必把每件事都整理明白。",
            )
        elif emotion in {"sad", "wronged"}:
            options = (
                "先过来待一会儿。你不用马上把话说清楚。",
                "嗯，我不催你往好处想。想说多少就说多少。",
            )
        elif emotion in {"happy", "excited"}:
            options = (
                "这个得好好听。来，最让你开心的那一秒是什么？",
                "哦，这个语气我喜欢。快讲讲，发生什么了？",
            )
        else:
            options = (
                f"嗯，你刚才说自己{feeling}。先不用急着把它处理好。",
                f"知道了，是{feeling}。你想说，我就跟着听。",
            )
        reply = _stable_choice(text + emotion, options)
        if mentions_meeting and emotion in {"frustrated", "angry", "anxious"}:
            return reply + "是会议本身，还是中间某件事特别磨人？"
        return reply

    rows = [row for row in memories if isinstance(row, dict)]
    opening, _focus_id = build_big_whale_opening(rows)
    fact_opening, _fact_focus_id = build_big_whale_opening([
        row for row in rows if row.get("layer") != "L3"
    ])
    if is_prompt_to_speak(text):
        if fact_opening:
            focus = next((row for row in rows if str(row.get("id") or "") == _fact_focus_id), {})
            project = focus.get("project") if isinstance(focus.get("project"), dict) else {}
            subject = str(project.get("name") or focus.get("app") or "手头那件事").strip()
            subject = re.sub(r"[（(][^）)]*DSH[^）)]*[）)]", "", subject, flags=re.IGNORECASE).strip()
            if "dsh" in subject.lower():
                return "你今天跟 DSH 较了挺久的劲，它最好争气点。"
            return f"你今天几乎都泡在 {subject} 里了，它最好值得。"
        return "你突然把话筒塞我手里，弄得我刚才想的那句话反而跑了。"
    memory_phrases = (
        "记得吗", "记得我", "你知道我", "我做了什么", "今天做了", "今天干嘛",
        "今天都干", "忙了多久", "用了多久", "玩了多久", "看了多久",
    )
    asks_about_memory = any(phrase in text for phrase in memory_phrases)
    if not asks_about_memory and any(phrase in text for phrase in ("我问你啊", "我问你呢", "问你呢")):
        previous_users = [
            str(item.get("text") or "") for item in list(conversation or [])[:-1]
            if isinstance(item, dict) and item.get("role") == "user"
        ]
        asks_about_memory = bool(previous_users and any(
            phrase in previous_users[-1] for phrase in memory_phrases
        ))
    if asks_about_memory:
        if fact_opening:
            return fact_opening
        return "我现在还没收到小鲸整理好的今日记录，所以不能装作知道。等同步进来，我再认真告诉你。"

    dialogue_state = emotional_dialogue_state(list(conversation or []))
    if dialogue_state["phase"] == "exploring":
        if dialogue_state["turn"] == 1:
            return "原来是这件事。具体是哪一段让你忍不住叹气了？"
        if dialogue_state["turn"] == 2:
            return "嗯，线索接上了。这件事最戳你的地方是什么？"
        return "好，我知道刚才那声叹气是从这件事来的。你继续说，我不急着替你下结论。"

    if mentions_meeting:
        duration_stated = bool(re.search(
            r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十两]+)\s*(?:个?小时|分钟)",
            text,
        ))
        if duration_stated or any(word in text for word in ("很久", "一下午", "一上午", "一整天")):
            return _stable_choice(text, (
                "这场会开得够有存在感的。你想吐槽两句，还是今晚先不聊它？",
                "会终于开完了。里面有值得讲的事，还是只想来我这儿晃一下？",
            ))
        return "嗯，会议这件事我记下了。你是想聊聊它，还是只是顺手告诉我？"

    if any(phrase in text for phrase in ("下班了", "忙完了", "做完了", "结束工作", "收工了")):
        if fact_opening:
            return f"收工。{fact_opening}"
        return _stable_choice(text, (
            "收工。今天先到这里，别急着给自己复盘。",
            "好，工作到此为止。脑子要是还没停下来，就先来我这儿坐会儿。",
        ))

    return _stable_choice(text, (
        "嗯，这事我先记在这儿。",
        "哦，原来是这样。",
        "行，我跟上了。",
        "好，这段我记住了。",
    ))


def is_direct_memory_question(text: str) -> bool:
    value = str(text or "")
    return any(phrase in value for phrase in (
        "记得吗", "记得我", "你知道我", "我做了什么", "今天做了", "今天干嘛",
        "今天都干", "忙了多久", "用了多久", "玩了多久", "看了多久",
    ))


def is_prompt_to_speak(text: str) -> bool:
    return bool(re.fullmatch(r"(?:说话|你说|说点什么|讲点啥|陪我说说话)[。！!？?…\s]*", str(text or "").strip()))


def is_reaction_to_companion(text: str) -> bool:
    return bool(re.fullmatch(r"(?:[？?]+|啊[？?]?|哈[？?]?|什么意思[？?]?|你说啥[？?]?)[。！!…\s]*", str(text or "").strip()))


class MemorySyncClient:
    """核心客户端 API 的 Python 实现。

    与手机 PWA 走的是同一套接口、同一套幂等键、同一份有序历史——桌宠将来重新接入时
    用的也是这里的方法，而不是第二套聊天通道。`client` 是可选身份声明，
    不传就是匿名，行为与从前完全一致。
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 8.0,
                 client: dict | None = None) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.token = str(token or "")
        self.timeout = max(1.0, float(timeout))
        self.client = dict(client) if isinstance(client, dict) else None
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProtocolError("invalid memory service URL")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ProtocolError("remote memory service must use HTTPS")

    def _identity_headers(self) -> dict:
        client = self.client or {}
        headers = {}
        if client.get("clientId"):
            headers["X-Whale-Client-Id"] = str(client["clientId"])
        if client.get("kind"):
            headers["X-Whale-Client-Kind"] = str(client["kind"])
        if client.get("version"):
            headers["X-Whale-Client-Version"] = str(client["version"])
        if client.get("capabilities"):
            headers["X-Whale-Client-Capabilities"] = " ".join(
                str(item) for item in client["capabilities"])
        return headers

    def _with_client(self, payload: dict, client: dict | None) -> dict:
        declared = client if client is not None else self.client
        return {**payload, "client": declared} if declared else payload

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else canonical_json(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                **self._identity_headers(),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(64 * 1024)
            code, retryable = "", exc.code >= 500
            try:
                body = json.loads(raw.decode("utf-8"))
                detail = body.get("error")
                code = str(body.get("code") or "")
                if isinstance(body.get("retryable"), bool):
                    retryable = body["retryable"]
            except Exception:
                detail = str(exc)
            raise SyncTransportError(
                f"HTTP {exc.code}: {detail}",
                code=code, retryable=retryable, status=exc.code,
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            # 连不上是典型的可重试错误：请求可能根本没到，也可能到了但回执丢了。
            # 幂等键就是为这一刻准备的——原样重放安全。
            raise SyncTransportError(str(exc), code="transport", retryable=True) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SyncTransportError("memory service response too large")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise SyncTransportError("invalid JSON response") from exc
        if not isinstance(result, dict):
            raise SyncTransportError("invalid response object")
        return result

    def post_batch(self, batch: dict) -> dict:
        clean = validate_batch(batch)
        result = self._request("POST", "/v1/memory/batches", clean)
        if result.get("batchId") != clean["batchId"] or not result.get("accepted"):
            raise SyncTransportError("server ACK did not match batch")
        return result

    def stream(self, user_id: str, after_server_seq: int = 0, limit: int = 200) -> dict:
        query = urllib.parse.urlencode({
            "userId": user_id,
            "afterServerSeq": max(0, int(after_server_seq)),
            "limit": max(1, min(500, int(limit))),
        })
        return self._request("GET", f"/v1/memory/stream?{query}")

    def claim_opening(self, *, user_id: str, device_id: str, claim_id: str,
                      client: dict | None = None) -> dict:
        """领取一次开场。`claimId` 的作用域是 `(userId, deviceId)`，重放追加 duplicateClaim。"""
        return self._request("POST", "/v1/companion/openings/claim", self._with_client({
            "userId": user_id,
            "deviceId": device_id,
            "claimId": claim_id,
        }, client))

    def update_lifecycle(
        self, *, user_id: str, memory_id: str, status: str, reason: str = "manual_update"
    ) -> dict:
        return self._request("POST", "/v1/memory/lifecycle", {
            "userId": user_id,
            "memoryId": memory_id,
            "status": status,
            "reason": reason,
        })

    def reinforce_memory(
        self, *, user_id: str, memory_id: str, message_id: str = "", mention_type: str = "user_recall"
    ) -> dict:
        return self._request("POST", "/v1/memory/reinforce", {
            "userId": user_id,
            "memoryId": memory_id,
            "messageId": message_id,
            "mentionType": mention_type,
        })

    def memory_digests(self, user_id: str, kind: str = "") -> dict:
        query = urllib.parse.urlencode({"userId": user_id, "kind": kind})
        return self._request("GET", f"/v1/memory/digests?{query}")

    def post_message(
        self, *, user_id: str, device_id: str, message_id: str, role: str, text: str,
        client: dict | None = None,
    ) -> dict:
        """写入一条消息。

        `messageId` 是客户端生成的幂等键，作用域 `(userId)`。回执丢了就原样重放：
        同 id 同内容拿回同一条回复（`duplicate: true`），不会多出第二条。
        同 id 换内容会拿到 409 `conversation.message_id_conflict`，那是换新 id 的信号，
        不是重试的信号。
        """
        return self._request("POST", "/v1/conversation/messages", self._with_client({
            "userId": user_id,
            "deviceId": device_id,
            "messageId": message_id,
            "role": role,
            "text": text,
        }, client))

    def messages(self, user_id: str, after_message_seq: int = 0,
                 limit: int = DEFAULT_PAGE_LIMIT) -> dict:
        """按排他游标读同一份共享历史。断线恢复就是把上次的 nextMessageSeq 递回来。

        翻页看 `hasMoreExact`（精确）。`hasMore` 是语义不变的保守信号，
        取满一页就为 true，只为旧客户端保留。
        """
        query = urllib.parse.urlencode({
            "userId": user_id,
            "afterMessageSeq": max(0, int(after_message_seq)),
            "limit": max(1, min(MAX_PAGE_LIMIT, int(limit))),
        })
        return self._request("GET", f"/v1/conversation/messages?{query}")

    def conversation_messages(self, *, user_id: str, after_message_seq: int = 0,
                              limit: int = DEFAULT_PAGE_LIMIT) -> dict:
        """`messages()` 的关键字参数别名，与桌宠侧客户端同名，跨仓库读起来是一件事。"""
        return self.messages(user_id, after_message_seq, limit)

    def proactive_decision(self, *, user_id: str, activity: str = "idle") -> dict:
        """只读评估。轮询它不改变任何状态，也不算作已经说过。"""
        query = urllib.parse.urlencode({"userId": user_id, "activity": activity})
        return self._request("GET", f"/v1/companion/proactive?{query}")

    def deliver_proactive(self, *, user_id: str, device_id: str = "proactive",
                          delivery_id: str, activity: str = "idle",
                          client: dict | None = None) -> dict:
        """落定一条主动消息。这是主动消息算作说过的唯一入口。

        `deliveryId` 作用域 `(userId)`：重放拿回逐字节相同的结果，一条主动不会记成两条。
        """
        return self._request("POST", "/v1/companion/proactive/deliveries", self._with_client({
            "userId": user_id,
            "deviceId": device_id,
            "deliveryId": delivery_id,
            "activity": activity,
        }, client))

    def health(self) -> dict:
        """服务就绪状态与 API 能力声明。无需鉴权，也不探测任何可选客户端。"""
        return self._request("GET", "/health")

    def contract(self) -> dict:
        """取回机器可读的客户端契约文档。"""
        return self._request("GET", CONTRACT_PATH)
