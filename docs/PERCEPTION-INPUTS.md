# 可插拔感知输入（权威文档）

> 本文是「外部观察如何进入这套系统」的唯一权威说明。
> 上下文如何走到模型见 [`CONTEXT-PIPELINE.md`](CONTEXT-PIPELINE.md)；
> 客户端接入面见 [`CLIENT-CONTRACT.md`](CLIENT-CONTRACT.md)；
> 状态推进语义见 [`STATE-ADVANCEMENT.md`](STATE-ADVANCEMENT.md)。

## 范围

本阶段建立的是**通用来源状态与新鲜度模型**，不是某一个具体感知端。

本阶段**做**：协议、存储、新鲜度、来源状态、投影规则、调试轨迹、API 与契约。
本阶段**不做**：桌宠进程、Web Push、后台通知、任何具体桌宠 UI。
memory protocol v1 的字段语义一个字都没改；桌宠重新接入时仍然通过
`POST /v1/memory/batches` 提交 L1/L2/L3，感知面是另一条**可选**的、并行的输入。

## 一条观察至少要说清楚七件事

| 字段 | 含义 | 必填 |
| --- | --- | --- |
| `sourceId` | 哪个来源实例报的（某一台桌宠、某一个日历账号） | 是 |
| `sourceType` | 来源类别，决定字段白名单与默认新鲜期 | 是 |
| `observedAt` | 这件事**被看到**的时刻 | 否（缺省取 `receivedAt`） |
| `receivedAt` | 服务**收到**它的时刻 | 服务端填 |
| `expiresAt` / `freshnessSeconds` | 什么时候它不再代表此刻 | 否（按类型取默认） |
| `confidence` | 来源自己有多确定 | 否（默认 0.5） |
| `payload` / `evidenceRef` | 原始证据 | 否 |

`observedAt` 和 `receivedAt` 必须分开。一条离线攒了两小时才补传的观察，收到得很新、
看到得很旧；新鲜度只能按 `observedAt` 算，否则一次补传就能让两小时前的画面冒充此刻。
来源的钟比服务快超过 120 秒的观察直接拒收——"未来"的观察会永远显得最新鲜。

## 八条边界

1. **感知端完全可选。** 一条来源都没有时，聊天、关系、情绪线程、主动陪伴一切照旧，
   `perception_fragments([])` 返回空。
2. **原始 payload 只进存储。** 截图路径、原始采样、任意自定义字段落在
   `perception_observations.payload_json`，**没有任何一条通往模型的路**。
3. **进模型前必须由适配器投影。** 唯一通道是 `perception.perception_fragments()`：
   过期筛除 → 白名单投影 → 每来源取最新 → `ContextFragment`。
4. **`ContextAssembler` 仍是唯一组装入口。** `perception.py` 里没有 `sqlite3`、
   没有 `memory_server`、没有 responder，也不自己造帧。
5. **过期观察不进 CompanionFrame。** 片段和事实各自带 `expires_at`，组装器再筛一次；
   来源还在不在线都不能给过期观察续命。
6. **来源离线不得让她声称在实时观看。** 每条投影都带同一句指令：这是外部来源在
   `observedAt` 时刻的观察，不是此刻正在看到的画面。来源不是 `live` 时再加一句
   "它已经不在报数了"。
7. **观察不会升格成用户亲口说过的事。** 投影出来的事实 `source` 恒为 `observed`，
   来源自称什么都不改变这一点；置信度还受该类型上限压制，所以它不会在仲裁里
   压过用户亲口说的话。事实 key 自带 `perception:` 前缀，不与 `user:` 谓词抢同一个 key。
8. **来源在线 ≠ 用户在场。** `sources[].state` 描述的是**来源**，不是用户。
   感知不写 `proactive_presence`，不参与任何主动开口判断——接上一个来源，
   `GET /v1/companion/proactive` 的答案一个字都不会变。

## 来源类型与白名单

白名单之外的一切整条留在存储里。缺少本类型必备证据的整条丢弃。

| `sourceType` | 可进上下文的字段 | 必须有 | 默认新鲜期 | 置信度上限 |
| --- | --- | --- | --- | --- |
| `desktop_activity` | `app` `context` `durationSeconds` | `app` | 5 分钟 | 0.6 |
| `calendar` | `title` `startsAt` `endsAt` `location` | `title` `startsAt` | 24 小时 | 0.8 |
| `weather` | `condition` `temperatureC` `location` | `condition` | 1 小时 | 0.7 |
| `device_presence` | `device` `state` | `state` | 10 分钟 | 0.6 |

**认不出的来源类型：收下，但不投影。** 收下是为了让新来源不必等服务端先发版；
不投影是因为没有白名单就没法净化——未知不等于放行，轨迹里理由是
`unsupported_source_type`。

## 来源状态

| 状态 | 条件 |
| --- | --- |
| `live` | 最近一次收到在 120 秒内，且还有没过期的观察 |
| `idle` | 还有没过期的观察，但已经沉默超过 120 秒 |
| `offline` | 沉默超过 900 秒，或者一条没过期的观察都没有 |

同一个来源对"此刻"只能有一种说法，所以每个来源只有**最新的那条**进上下文，
被顶掉的进轨迹，理由是 `superseded`。

## 调试轨迹

`GET /v1/perception/state` 和控制台快照的 `perception` 块逐条记下每个 `observationId`
是进去了还是被丢了、以及丢它的理由。没有这条轨迹，"她为什么没提这件事"就只能靠猜。

丢弃理由：`expired` / `unsupported_source_type` / `missing_required_evidence` /
`field_not_projectable` / `payload_not_projectable` / `superseded`。

轨迹是**调试数据**：它走帧的兼容键，`model_view()` 读不到它，也不含任何原始 payload。

## API

| 接口 | 语义 |
| --- | --- |
| `POST /v1/perception/observations` | 上报一批观察。`observationId` 是幂等键，作用域 `(userId)`，重传不会多出第二条 |
| `GET /v1/perception/state` | 只读：来源状态与本轮投影/丢弃轨迹，不含原始 payload |

新增表：`perception_sources`、`perception_observations`。每个来源只保留最近 200 条
（保留数固定、按 `observedAt` 排序，所以重放不会删出第二种结果）；
一轮上下文最多读 60 条。

## 数据兼容

- 两张表都是新表，`CREATE TABLE IF NOT EXISTS`，老库直接可用，不需要迁移脚本。
- 一条观察都没有的库，行为与升级前逐字节相同。
- memory protocol v1 的字段语义不受影响；感知面和记忆批次是两条独立的输入。
- 客户端 API 只增字段不改语义，`error` 等旧客户端兼容字段一个都没删。

## 重新接入桌宠时

1. 桌宠仍然通过 `POST /v1/memory/batches` 提交 L1/L2/L3——那是**记忆**。
2. 需要"此刻在做什么"这类**易腐的观察**时，改走 `POST /v1/perception/observations`，
   `sourceType` 用 `desktop_activity`，并如实填 `observedAt`。
3. 桌宠在线与否不要报成用户在不在——那是两件事，服务端也不会这样解读。
4. 想加一个新来源类别：在 `SOURCE_TYPE_RULES` 里加白名单、必备证据、新鲜期和置信度上限，
   然后在 [`docs/PERCEPTION-INPUTS.md`](PERCEPTION-INPUTS.md) 这张表里补一行。
   在它进白名单之前，服务端会照收不误，只是不投影。
