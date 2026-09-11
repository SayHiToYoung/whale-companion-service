# 状态推进语义（权威文档）

> 本文是「哪些入口会改变状态、以什么语义改变」的唯一权威说明。
> 上下文管线见 [`CONTEXT-PIPELINE.md`](CONTEXT-PIPELINE.md)；主动开口与关系策略见
> [`COMPANION-POLICIES.md`](COMPANION-POLICIES.md)；客户端接入面见
> [`CLIENT-CONTRACT.md`](CLIENT-CONTRACT.md)。

## 不变式

本项目**不采用**「所有 GET 禁止写库」这条机械规则。惰性初始化和基于时间的状态推进
都是陪伴该有的行为：她的一天要有个起点，故事要随时间往前走。真正要守住的是比它更强、
也更有意义的一条：

> **同一输入、同一固定时刻、同一已有状态下，重复调用结果一致，且不重复累计副作用。**

"副作用"是可数的五样东西：**事件、账本记录、故事进度、主动投递、对话消息**。
它们由 [`tests/test_state_advancement_audit.py`](../tests/test_state_advancement_audit.py)
逐项锁住，时钟一律注入并钉死——时间不走，答案就没有任何理由变。

## 五类语义

| 类别 | 含义 | 写库？ | 举例 |
| --- | --- | --- | --- |
| 纯读取 | 只把已有状态读出来 | 否 | `GET /v1/companion/affinity` |
| 惰性初始化 | 当日/首次基线的落定，由确定性种子生成 | 是（首次） | `daily_state` 的 sleep/dream |
| 时间重算 | 按此刻重算，**永不落库** | 否 | hunger、临时生活事件 |
| 状态推进 | 真实事件或时间推动状态前进 | 是 | 故事走一格 |
| 消费操作 | 把一件事标记为"已经用掉了" | 是 | 开场领取、主动投递 |

## 入口审计表

| 入口 | 语义 | 写库 | 重复读幂等 | 依赖真实时钟 | 可能重复副作用 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET /v1/companion/daily-state` | 惰性初始化 + 时间重算 | 是（当日持久条件） | 是 | 否（注入时钟） | 否 | 保持 |
| `GET /v1/companion/story` | 状态推进（时间驱动） | 是（`story_arcs`） | 是 | 否 | 否 | **已修**：起线顺序归一 |
| `GET /v1/companion/proactive` | 只读评估（+ 当日基线惰性初始化） | 仅 `daily_state` | 是 | 否 | 否 | **已修**：文档措辞改为诚实说法 |
| `GET /v1/companion/relationship` | 纯读取 | 否 | 是 | 否 | 否 | 保持 |
| `GET /v1/companion/affinity` | 纯读取 | 否 | 是 | 否 | 否 | 保持 |
| `GET /v1/companion/emotion` | 纯读取 | 否 | 是 | 否 | 否 | 保持 |
| `GET /v1/companion/self-timeline` | 纯读取 | 否 | 是 | 否 | 否 | 保持 |
| `GET /v1/companion/expressions` | 纯读取 | 否 | 是 | 否 | 否 | 保持 |
| `GET /v1/companion/living-config` | 纯读取（缺省值不落库） | 否 | 是 | 否 | 否 | 保持 |
| `GET /v1/persona-card` | 纯读取（无卡时返回默认，不落库） | 否 | 是 | 否 | 否 | 保持 |
| `GET /v1/profile-memories` | 纯读取 | 否 | 是 | 否 | 否 | 保持 |
| `GET /v1/memory/digests` | 纯读取 | 否 | 是 | 否 | 否 | 保持 |
| `GET /v1/memory/stream` | 纯读取 | 否 | 是 | 否 | 否 | 保持 |
| `GET /v1/conversation/messages` | 纯读取（+ 可选客户端身份观测） | 仅 `client_sessions` | 是 | 否 | 否 | 保持 |
| `GET /v1/perception/state` | 纯读取 | 否 | 是 | 否 | 否 | 保持（新增） |
| `GET /v1/debug/state`（含 CompanionFrame 构建） | 只读快照 + 当日基线惰性初始化 | 仅 `daily_state` | 是 | 否 | 否 | **已修**：改用注入时钟 |
| `POST /v1/debug/openings/regenerate` | 纯读取预览，显式 `persisted: false` | 否 | 是 | 否 | 否 | **已修**：改用注入时钟 |
| memory activation / mention（`_record_memory_mention_locked`） | 状态推进 | 是 | 是（唯一约束去重） | 否 | 否 | 保持 |
| CompanionFrame 构建（`build_reply_frame`） | 纯计算 | 否 | 是 | 否（需显式传 `now`） | 否 | 保持 |
| `POST /v1/conversation/messages` | 状态推进 | 是 | 是（`messageId` 幂等） | 否 | 否 | 保持 |
| `POST /v1/companion/proactive/deliveries` | 消费操作 | 是 | 是（`deliveryId` 幂等） | 否 | 否 | **已修**：回执独立命名空间 |
| `POST /v1/companion/openings/claim` | 消费操作 | 是 | 是（`claimId` 幂等） | 否 | 否 | 保持 |
| 服务启动 `_initialize` | 一次性迁移 | 是（仅首次） | 是 | 否 | 否 | **已修**：改为一次性迁移 |

## 已修的五个问题

### 1. 故事线顺序不稳定（高）

刚起线的 `arcs` 按模板顺序追加，从库里读回来按 `arc_id` 排。于是同一时刻先读一次和
后读一次会拿到两份顺序不同的答案，状态明明没变；重启前后同样对不上。
`story.order_arcs()` 把顺序归一，读写两条路径因此得到逐字节相同的结果。

### 2. 启动补齐每次都跑，重启会凭空造出可投递的开场（高）

每日单元由 `POST /v1/memory/batches` 建；`append_message` 提取出的 L3 情绪不属于任何单元。
但启动时那段"兼容升级前已入库的事实"的补齐对**所有**记忆一视同仁，于是重启一次，
手机就能领到一条由刚才那句聊天生成的开场——服务不重启则永远不会发。
同一份数据不该因为进程重启就多出可投递的东西。
现在它是 `schema_migrations` 记账的一次性迁移，跑过就不再跑。

### 3. `deliveryId` 与 `claimId` 共用命名空间（高）

投递回执曾经写在 `opening_claims` 上，靠写死的 `device_id='proactive'` 与开场领取区分。
两个幂等键的作用域本来就不同（`deliveryId` 是 `(userId)`，`claimId` 是 `(userId, deviceId)`），
于是一个把 `deviceId` 报成 `"proactive"` 的客户端，拿某个 `deliveryId` 当 `claimId`
就能从开场接口拿回一份投递响应，形状还对不上 `OpeningClaim` 契约。
现在回执独占 `proactive_deliveries` 表，老库里的回执在启动时搬过来，重放保证不变。

### 4. 控制台快照用墙钟而不是注入时钟（中）

`debug_snapshot` 构建 CompanionFrame 时不传 `now`，于是走真实墙钟。注入固定时钟的仿真里
连读两次会拿到两份 `created_at` 不同的帧——状态没变，快照却变了，审计因此失去意义。
`preview_opening` 的 `generatedAt`、`update_living_config` 与 `set_chronotype` 的时间戳
同样改走 `self._clock()`；`scene.build_scene` 也接受可注入的 `now`。

### 5. 只读评估的文档措辞与实际不符（中）

`GET /v1/companion/proactive` 的注释和契约都写着"不改变任何状态"，实际会惰性初始化
当日基线。这不是要去掉那次写入——它是允许且被测试锁住的惰性初始化——而是要把话说准：
它不推进任何**陪伴**状态，候选队列、发送记录、故事进度一律不动。

## 加一个新入口时

1. 先想清楚它属于上面五类中的哪一类，并在本表里加一行。
2. 惰性初始化的内容必须由确定性种子生成，同一时刻重算得到同一结果。
3. 时间重算出来的东西**永不落库**——窗口一过它就该消失，而不是留在流水里。
4. 消费操作必须有客户端生成的幂等键，并写明作用域；不要与已有幂等键共用命名空间。
5. 所有时间戳走 `self._clock()` / `self._now()`，不要直接读墙钟。
6. 在 `tests/test_state_advancement_audit.py` 的 `READ_ENTRIES` 里加上它。
