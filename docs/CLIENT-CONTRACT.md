# 客户端契约 v1

手机 PWA、Web、控制台、未来的桌宠和任何其它客户端，都通过这一套接口使用陪伴大脑。
**不存在第二套聊天接口。** 桌宠重新接入时用的是这里的同一批端点，而不是私有通道。

机器可读版本由服务自己提供，且不需要口令：

```bash
curl http://127.0.0.1:47821/v1/contract
```

那是一份 OpenAPI 3.1 文档。3.1 的 schema 对象就是 JSON Schema，所以同一份文件既是
文档也是校验规则——`tests/test_client_contract.py` 拿真实响应打这份 schema，
文档与实现因此无法各走各的路。文档源码在
[`whale_companion_service/client_contract.py`](../whale_companion_service/client_contract.py)。

## 核心端点

| 端点 | 作用 | 鉴权 |
| --- | --- | --- |
| `GET /health` | 就绪状态与 API 能力声明 | 否 |
| `GET /v1/contract` | 这份契约文档本身 | 否 |
| `POST /v1/conversation/messages` | 写入一条消息并同轮拿到回复 | 是 |
| `GET /v1/conversation/messages` | 按游标增量读共享历史 | 是 |
| `GET /v1/companion/proactive` | 只读评估主动开口意愿 | 是 |
| `POST /v1/companion/proactive/deliveries` | 落定一条主动消息（唯一入口） | 是 |
| `POST /v1/companion/openings/claim` | 原子领取一次开场 | 是 |

`GET /v1/companion/proactive` 是**纯只读**的：轮询多少次都不改变任何状态，
也不算作"已经说过"。主动消息只能通过 `POST /v1/companion/proactive/deliveries` 落定。

## 标识符语义

四个标识符各自解决一个具体问题，作用域不同，不能互相替代。

### `messageId` —— 一次用户发言的幂等键

- 由**客户端**生成，作用域 `(userId)`。
- 同 id + 同 `(role, text)` → 重放。响应 `duplicate: true`，`assistantMessage` 是
  **同一条**回复（回复 id 是 `reply_<hash(userId, messageId)>`，确定性推导）。
  重试一百次也只有一条回复。
- 同 id + 不同内容 → `409 conversation.message_id_conflict`，`retryable: false`。
  这是"换一个新 id"的信号，不是"过会儿再试"的信号。冲突的内容不会进入历史。
- 回执丢失时**原样重放是安全的**，这正是这个键存在的理由。

### `deliveryId` —— 一次主动投递的幂等键

- 由**客户端**生成，作用域 `(userId)`（注意：不含 deviceId）。
- 重放拿回逐字节相同的结果，一条主动消息不会被记成两条。
- 换一个新的 `deliveryId` 也不会把同一条内容说第二遍：会话历史里的
  `stable_id("proactive", userId, signature)` 是队列清理之后仍然生效的最终去重底线。
- 提交前先把 `deliveryId` 存到本地，**提交成功后再删**。这样断电重启后仍能安全重放。

### `claimId` —— 一次开场领取的幂等键

- 由**客户端**生成，作用域 `(userId, deviceId)`——这一点与 `deliveryId` **不同**。
- 重放的响应会多出 `duplicateClaim: true`。
- 另一台设备用同一个 `claimId` 不算重放，因为开场是按设备领取的。

### `afterMessageSeq` —— 共享历史的排他游标

- `messageSeq` 由**服务端**分配，全用户单调递增，与写它的是哪个客户端无关。
- 所以网页、手机和未来的桌宠读到的是**同一条有序时间线**。
- `afterMessageSeq` 是**排他**的：已经见过的消息不会再来。
- 断线恢复 = 把上次的 `nextMessageSeq` 原样递回来。客户端不需要本地重放，
  也不需要按时间戳猜。
- 空页时 `nextMessageSeq` 原样回传入参。

### 两个翻页信号

| 字段 | 含义 | 给谁 |
| --- | --- | --- |
| `hasMoreExact` | **精确**：确实还存在下一条才为 true。服务端多取一条判定。 | 新客户端只看它 |
| `hasMore` | **保守**：取满一页就为 true，哪怕后面已经没有了。语义不变。 | 旧客户端 |

分界在"最后一页刚好取满"：`hasMore` 说 true，`hasMoreExact` 说 false。
只按 `hasMore` 翻页的客户端必须**同时**要求 `nextMessageSeq` 前进，否则会多翻一轮空页。
`hasMoreExact` 没有这个陷阱，一个条件就够：

```js
let cursor = 0;
for (;;) {
  const page = await fetchPage(cursor);
  render(page.messages);
  if (!page.hasMoreExact) break;
  cursor = page.nextMessageSeq;
}
```

`hasMoreExact` 是**新增**字段，旧客户端可以完全忽略它。服务端是否提供，
可以查 `GET /health` 的 `api.capabilities` 里有没有 `conversation.hasMoreExact`。

## 错误契约

所有错误响应形状统一：

```json
{
  "error": "messageId already exists with different payload",
  "code": "conversation.message_id_conflict",
  "message": "同一个 messageId 已经用于另一段内容，换一个新的 messageId",
  "retryable": false,
  "status": 409,
  "apiVersion": 1
}
```

- `error` 是**向后兼容字段**，自由文本。升级前的客户端只认它，所以永远保留。
- `code` 是稳定的机器可读标识。客户端按它分支，**不要解析 `message`**。
- `message` 是稳定的可读说明，可以直接展示。
- `retryable` 是唯一的重试依据。

完整 code 列表在 `GET /health` 的 `api.errorCodes` 里，也在契约文档的
`ErrorResponse.code` 枚举里；两者由测试锁死，不会漂移。

### 客户端重试规则

| 情况 | 做什么 |
| --- | --- |
| 网络错误、请求没发出去、回执丢了 | **原样重放**，带同一个幂等键 |
| `retryable: true` | 退避后原样重放；有 `retryAfterSeconds` 就按它退避 |
| `retryable: false` | **不要重试**。改请求或告诉用户 |
| `409 conversation.message_id_conflict` | 换一个新 `messageId`，别重放 |
| `401 request.unauthorized` | 让用户重新配对，别重试 |

重点：**只看 `retryable`，不要按状态码区间推断**。
绝大多数 4xx 是「请求本身有问题」，等再久也不会成功；但 408 Request Timeout 和
429 Too Many Requests 说的是「此刻不行，等一下再来」——它们是**可重试的 4xx**。
当前没有任何路径会返回这两个码；契约里先写好，是为了让客户端现在就按 `retryable`
分支，将来服务端接上限流或超时时不必再改一行重试逻辑。

可重试的错误可以带 `retryAfterSeconds`，与响应头 `Retry-After` 同值。
没有这个字段就用客户端自己的退避策略——它是建议，不是前提。

## 可选的客户端身份与 capabilities

**整块都是可选的。** 一个字段都不发的客户端是完全合法的客户端，行为与升级前逐字节相同。

请求体里发：

```json
{
  "userId": "local-user", "deviceId": "phone-a", "messageId": "user_1",
  "role": "user", "text": "你好",
  "client": {
    "clientId": "client_9f2c", "kind": "mobile", "version": "2.0.0",
    "apiVersion": 1, "capabilities": ["conversation.send", "conversation.history"]
  }
}
```

GET 请求用请求头（同名字段，请求体优先）：

```
X-Whale-Client-Id: client_9f2c
X-Whale-Client-Kind: mobile
X-Whale-Client-Version: 2.0.0
X-Whale-Api-Version: 1
X-Whale-Client-Capabilities: conversation.send conversation.history
```

规则：

- `kind` 的已知取值是 `web / mobile / desktopPet / console / agent / other`。
  未知取值**归一为 `other`，不会被拒绝**——未来的客户端不该被今天的枚举挡在门外。
- `capabilities` 是**声明式**的。服务端只记录，从不据此拒绝请求，也从不要求某个能力。
- 声明过身份的客户端出现在 `GET /v1/debug/state` 的 `clients` 里，纯观测用途：
  它不进模型上下文，不参与主动决策，不改变任何已有数据的含义。
- 没声明身份的客户端在库里**一行都不留**。

服务端自己支持什么，读 `GET /health` 的 `api.capabilities`。

## 可选的感知输入

`POST /v1/perception/observations` 和 `GET /v1/perception/state` 是**完全可选**的一面：
一个来源都不接，聊天、关系、主动陪伴一切照旧。

- `observationId` 是幂等键，作用域 `(userId)`，重传不会多出第二条观察。
- `payload` 原样落存储，**不直接进模型**；进上下文的只有按 `sourceType` 白名单
  投影出来的最小事实，来源恒为观察，不会升格成用户亲口说过的事。
- `sources[].state`（`live` / `idle` / `offline`）描述的是**来源**，不是用户：
  它不表示用户在电脑前、有空或者醒着。

细则见 [`PERCEPTION-INPUTS.md`](PERCEPTION-INPUTS.md)。

## 兼容规则

1. 顶层 `error` 字符串永不移除。
2. 新字段一律可选且有默认值；不发新字段的请求永远合法。
3. 响应只增字段不改语义；客户端必须容忍未知字段（契约里的响应对象都不是
   `additionalProperties: false`）。
4. 未知的 `client.kind` 与未知的 capability 一律接受，不报错。
5. `messageId` / `deliveryId` / `claimId` 的作用域是数据里已经固化的事实，不会改。
6. memory protocol v1（`POST /v1/memory/batches` 那条线）的已有字段语义与本契约无关，
   也不受本契约版本影响——它们是两条独立的版本线。

破坏性变更需要一个新的 `apiVersion`，并且旧版本要与新版本并存。

## 第一方客户端实现

- 手机 PWA：[`mobile/app.js`](../mobile/app.js)
- Python：`whale_companion_service.memory_protocol.MemorySyncClient`
  （`post_message` / `conversation_messages` / `deliver_proactive` /
  `proactive_decision` / `claim_opening` / `health` / `contract`）。
  错误以 `SyncTransportError` 抛出，带 `.code` / `.retryable` / `.status`。

```python
from whale_companion_service.memory_protocol import MemorySyncClient, SyncTransportError

client = MemorySyncClient(
    "http://127.0.0.1:47821", "local-dev-token",
    client={"clientId": "my-app-1", "kind": "agent", "capabilities": ["conversation.send"]},
)
try:
    result = client.post_message(
        user_id="local-user", device_id="my-app",
        message_id="turn-1", role="user", text="你好",
    )
except SyncTransportError as exc:
    if exc.retryable:
        ...  # 原样重放，幂等键保证安全
    elif exc.code == "conversation.message_id_conflict":
        ...  # 换一个新 messageId
```

## 契约测试

`tests/test_client_contract.py` 用真实 HTTP 服务锁住这份契约：契约与路由一致、
六个端点的请求与响应都过 schema、三类幂等键、多客户端读同一份有序历史、
断线从游标恢复、旧客户端零新字段仍然可用，以及在完全没有桌宠的前提下全部成立。

```bash
python -m pytest tests/test_client_contract.py -q
node --test tests/mobile_sync.test.cjs
```
