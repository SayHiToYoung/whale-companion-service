# 上下文管线（权威文档）

> 本文是「原始事件如何变成模型上下文」的唯一权威说明。
> 与 [`DESIGN-living-companion.md`](DESIGN-living-companion.md) 冲突时以本文为准——
> 那份是设计底稿，已被本阶段取代。
> 架构全景见 [`ARCHITECTURE.md`](ARCHITECTURE.md)；主动开口与关系策略见
> [`COMPANION-POLICIES.md`](COMPANION-POLICIES.md)；可选外部观察见
> [`PERCEPTION-INPUTS.md`](PERCEPTION-INPUTS.md)；验收口径见 [`ACCEPTANCE.md`](ACCEPTANCE.md)。

## 一句话

原始事件只进存储；模块把它处理成带来源的 `ContextFragment`；`ContextAssembler`
是唯一的组装入口；模型只看到仲裁后的四个键。

## 分层

```text
①  采集与存储          桌宠/手机 → POST /v1/memory/batches → SQLite（不可变原始事件）
                       可选感知端 → POST /v1/perception/observations → SQLite（原始 payload）
                       └─ 原始事件与原始 payload 到此为止。它们永远不直接进模型。
②  模块处理层          memory / daily_life / relationship / story / boundaries / …
                       └─ 每个模块只输出 ContextFragment
③  仲裁与预算          ContextAssembler.assemble()   ← 唯一入口
④  模型可见面          CompanionFrame.model_view()   ← 只有四个键
```

### ① 采集与存储

`memory_events` 表按 `(user_id, memory_id)` 唯一存原始事件，批次幂等。
原始行里可以有任何字段——`rawEvents`、`activityLogs`、`audit`、截图路径、客户端
自定义 payload。**这些字段没有任何一条通往模型的路。**

### ② 模块处理层：只输出 ContextFragment

投影发生在 `companion_runtime/context_adapters.py`。每一层记忆只有资格声称
白名单里的字段，并且必须拿得出这层要求的证据：

| layer | 可进上下文的字段 | 必须有 | 来源自述 |
| --- | --- | --- | --- |
| L1 | `app` `context` `durationSeconds` `projectName` | `app` | `observed` |
| L2 | `statement` | `statement` | `derived` |
| L3 | `label` `quote` | `label` | `user_stated` |

白名单之外的一切整条丢弃。层与来源自述不一致的也整条丢弃——一条桌面观察
自称 `user_stated`，进上下文后会被仲裁当成用户亲口说过的话，证据强度凭空升一级。
宁可丢掉这条记忆。

可选的外部观察走同一条纪律，只是白名单按 `sourceType` 分：
`perception.perception_fragments()` 是它唯一的通道——过期筛除 → 白名单投影 →
每来源取最新 → `ContextFragment`，事实来源恒为 `observed`，置信度受该类型上限压制。
整条感知链路是**可选**的：一条来源都没有时它什么也不加。细则见
[`PERCEPTION-INPUTS.md`](PERCEPTION-INPUTS.md)。

`safe_value()` 是第二道防线：它按 `_PRIVATE_KEYS` 递归剥掉内部字段
（`raw*` `audit` `ledger` `evidence` `score` `carryWeight` `activation` `metadata` …），
并拒绝任何非 JSON、非有限的值。适配器仍然必须显式投影，防线不替代投影。

### ③ 仲裁：ContextAssembler

`context_assembler.py`。**无状态写入、无提示词、无模型调用、无消息发送**——
这四条由 `test_goal3_assembler_performs_no_state_writes_or_model_calls` 锁住。

优先级（`Priority`，决定**收录顺序**）：

| 档 | 值 | 含义 |
| --- | --- | --- |
| `BOUNDARY` | 800 | 用户划下的线 |
| `CURRENT` | 700 | 此刻这句话和当前场景 |
| `OPEN_THREAD` | 600 | 还没说完的事 |
| `MEMORY` | 500 | 记忆与长期事实 |
| `RELATIONSHIP` | 400 | 关系状态与权限 |
| `DAILY_LIFE` | 300 | 她的生活节律 |
| `PERCEPTION` | 250 | 可选外部观察（`Priority.STORY + 50`） |
| `STORY` | 200 | 共同故事线 |
| `STYLE` | 100 | 人格与表达 |

**优先级决定顺序，不决定真假。** 同 key 冲突时另有一条权威链：

```text
边界 > 置信度 > 证据来源（user_stated > confirmed > observed > derived > default）> 时间
```

一条低置信度的猜测不会因为它所属模块优先级高就变成事实。
只有**取值相同**的证据才会被合并到胜出事实的 `source_event_ids` 上——
矛盾的证据永远不挂到胜出者名下。

四条硬规则：

- **无来源不进上下文。** `source_event_ids` 为空的事实直接丢弃。
- **过期即消失。** 片段和事实各自的 `expires_at` 取更早的那个。
- **生命周期是闸门。** 只有 `active` / `confirmed` / `pending_confirmation` /
  `virtual` / `unknown` 可说；`resolved` `dormant` `suppressed` `corrected` 不进。
- **控制模块不可被反向屏蔽。** 只有 `boundaries` 和 `relationshipPolicy` 有资格约束
  别的模块，它们自己不受屏蔽——否则被约束的模块可以反过来把约束它的那个挡掉。

预算单位是 UTF-8 字节上界，与具体分词器无关。装不下时先丢低档的；
**边界和当前这句话是强制内容**，装不下就抛 `ContextBudgetError`，
由 `build_reply_frame` 转成 `generationBlocked` 走本地兜底——
宁可不生成，也不悄悄把边界丢掉。

### ④ 模型可见面

`model_view()` 只有四个键：

```json
{"version": "companion-frame-v5", "recentConversation": [], "processedFacts": [], "moduleInstructions": []}
```

`processedFacts` 每行都带完整出处：

```json
{"key": "memory:m1", "value": {"app": "VS Code", "durationSeconds": 7200, "layer": "L1"},
 "module": "memory", "source_event_ids": ["m1"], "confidence": 1.0,
 "created_at": "…", "expires_at": null, "source": "observed", "lifecycle": "active"}
```

帧上还挂着 `sharedScene` / `relationship` / `dailyLife` 等**旧投影键**，供既有调用方
和调试台读取。它们**不在 `model_view()` 里**，模型看不到。

## 唯一入口

```text
build_companion_frame()          ← 编排：收集片段 → assemble()
  └─ collect_context_fragments() ← 各模块投影
  └─ ContextAssembler.assemble() ← 唯一组装
build_reply_frame()              ← 仓库边界：预算超限转本地兜底
ensure_frame()                   ← 旧签名适配；持久化的帧重新进仲裁
```

`context_contract.py` 定义 `CompanionFrame`，`context_assembler.py` 造它，
另两个是它的入口。**其余任何模块直接造帧都会让
`test_goal3_no_module_builds_a_frame_outside_the_assembler` 失败。**

## 加一个新模块

1. 在 `companion_runtime/` 写纯函数，输出 `ContextFragment`。不写库、不调模型、不发消息。
2. 给每条事实一个稳定 `key`（同 key 意味着互斥取值）和真实的 `source_event_ids`。
3. 选一个 `Priority` 档；需要屏蔽别人就走 `boundaries` / `relationshipPolicy`，别自己造控制位。
4. 在 `collect_context_fragments()` 里接上。
5. 想主动开口就产**候选信号**交给 `proactive`，见 [`COMPANION-POLICIES.md`](COMPANION-POLICIES.md)。
