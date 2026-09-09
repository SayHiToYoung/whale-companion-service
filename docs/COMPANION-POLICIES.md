# 陪伴策略（权威文档）

> 本文是「她什么时候开口、能开什么口、关系允许到哪一步」的唯一权威说明。
> 与 [`DESIGN-living-companion.md`](DESIGN-living-companion.md) 冲突时以本文为准。
> 上下文侧见 [`CONTEXT-PIPELINE.md`](CONTEXT-PIPELINE.md)；验收口径见 [`ACCEPTANCE.md`](ACCEPTANCE.md)。

## 一句话

任何模块都只能**造候选**；发不发由 `proactive` 这一个确定性决策器说了算；
用户划下的线压过一切，包括关系已经很近这件事。

## 谁有权发送

```text
日程 ─┐
故事 ─┤
记忆 ─┼─→ 候选（带来源/时间窗/签名）─→ proactive 闸门链 ─→ 至多一条 ─→ 发送
情绪 ─┤                                      │
约定 ─┘                                      └─→ 其余：defer（等）/ drop（弃）
```

`daily_life.py` 和 `story.py` **没有发送这条路**：它们不 import sqlite3、不 import urllib、
不认识任何发送接口。这不是约定，是
`test_goal6_agenda_and_story_have_no_send_path` 在守。
帧里也明写给模型看：`proactivePolicy.modulesMaySend = false`。

## 候选

每个候选带 `source` `kind` `reason` `content` `topic` `motive` `signature`
`salience` `warmth` `urgency`，以及可选时间窗 `windowStart` / `bestUntil` / `expiresAt`。

| kind | 基础分 | 依据 | 归哪个许可管 |
| --- | --- | --- | --- |
| `follow_up` | 90 | 用户亲口交代的约定 | — |
| `open_emotion` | 75 | 还没闭合的情绪线程 | 共同记忆 |
| `grounded_opening` | 70 | 今日有据事实 | — |
| `agenda_share` | 55 | 生活契机（日程段） | — |
| `story_beat` | 52 | 故事走到新一格 | 共同记忆 |
| `memory_echo` | 50 | 记忆回响 | 共同记忆 |
| `mood_checkin` | 45 | 心情问候 | 主动关心 |
| `daily_greeting` | 40 | 早晚安锚点 | 主动关心 |
| `meal_care` | 35 | 饭点关心 | 主动关心 |

`score = 基础分 + urgency + 0.5·warmth + 0.5·salience − 0.01·年龄分钟`。
低于 `MIN_CANDIDATE_SCORE = 30` 直接弃掉——不值得打断用户的念头不该排队等。

**「主动关心」和「共同记忆」分开管是必要的**：她分享自己今天做了什么，
和她主动来关心你，不是同一件事，也不该被同一个开关一起关掉。

## 闸门链（顺序即优先级，命中即否决）

| # | 闸门 | vetoReason | 后续 |
| --- | --- | --- | --- |
| 1 | 用户仍在聊天（静默 < 2 min 且 active） | `user_still_chatting` | defer |
| 2 | 用户明说在休息 / 会议中 | `user_resting` | defer |
| 3 | 免打扰时段（深夜） | `quiet_hours` | defer |
| 4 | 今日已达上限 | `daily_cap` | defer |
| 5 | 距上次开口不足最小间隔 | `too_soon` | defer |
| 6 | 连续 3 次无回应 | `no_response_pause` | defer |
| 7 | 关系阶段不允许主动 | `relationship_boundary` | **drop** |
| 8 | 互动状态受伤/回避 | `interaction_converged` | defer |
| 9 | 无主动关心许可 | `care_not_permitted` | **drop** |
| 10 | 关心额度用完 | `care_quota_used` | defer |
| 11 | 无共同记忆许可 | `shared_memory_not_permitted` | **drop** |
| 12 | 候选价值不足 | `low_value` | **drop** |
| 13 | 时间窗还没到 | `window_not_open` | defer |
| 14 | 时间窗已过 | `window_expired` | **drop** |
| 15 | Token 硬限额 | `token_hard_limit` | defer |
| ★ | **用户边界** | `user_boundary` | **drop** |

★ 用户边界在闸门链**之后**统一复核，压过上面每一条，也压过关系已经很近这件事。
识别三种：`blockProactive` 标记、规则文本里的「不要/别…主动」「免打扰」、
以及候选正文命中被屏蔽的话题词。

**免打扰有且只有一个例外**：用户亲口交代要提醒的到期约定（`follow_up`）。
那是他自己让她半夜叫醒他的。

## 候选队列：延后、过期、防重复

按 `signature` 合并新旧两轮：

- **`sent` 是终点。** 说出口的念头永不回队列——防重复那道锁就是它。
- **延后的保留第一次出现的时间。** 每轮重算等于把年龄清零，一条候选可以永远不老。
- **依据没了就是没了。** 上轮留下、这轮没有依据的：有时间窗的留到窗口过期，没有的直接消失。

签名都带日期（`daily_greeting:2026-03-02:morning`）：早安每天该说一次，
但同一天只说一次。只按正文算签名的话，第二天那句一模一样的「早安」
会被当成「已经说过」而永远不再出现。

## 只读评估 vs 落定发送

| | 端点 | 副作用 |
| --- | --- | --- |
| 评估 | `GET /v1/companion/proactive` | **无。** 轮询一百次也不算「说过」 |
| 落定 | `POST /v1/companion/proactive/deliveries` | 写 `sent`、推进故事、记 `deliveryId` |

落定按 `deliveryId` 幂等，与开场领取同构：同一个 id 重放拿到同一份结果，
不会把一条主动记成两条。`sent` 记录保留 3 天（签名带日期，不必无限增长），
`expired` 立即清出队列。

## 关系与权限

8 档好感阶段（`deeply_distant` → `deeply_bonded`），7 档互动状态
（`avoidant` `hurt` `relaxed` `lively` `warm` `close` `affectionate`）。

**对外只给权限，不给分数。** 模型看到的是 `allowProactiveCare` /
`allowSharedMemory` / `allowNickname` / `allowPlayful` / `allowIntimateTone` /
`proactiveQuota`，看不到任何内部评分——`score` `trust` `warmth` `familiarity`
都在 `_PRIVATE_KEYS` 里。

权限由四样东西共同决定，**任何一样都能单独把门关上**：
阶段（有多熟）、互动档（此刻的气氛）、信任（够不够格更进一步）、边界态（用户划的线）。
分数到了、信任没到，一样不开。

同一份权限还以**控制数据**交给组装器（`blocked_modules` / `deny_sensitivities`）：
用户划了线时真的把对应模块挡在上下文外，而不是多给模型一句「请不要」。

关系分只走确定性账本：单日正向增量上限 12 分，去重、迟滞、衰减都在账本里。
`POST /v1/companion/affinity/adjust` 是用户显式控制，直接设分不经过账本上限，
但它只动分数和阶段——熟悉、信任和用户划下的边界不是滑块能拨的。

## LLM 的位置

**生产路径上，模型只做一件事：把已经定好的上下文说成人话。**

- 它拿到的是仲裁后的 `model_view()`，不是原始事件。
- 它的输出过 `model_reply_is_grounded()`：无据的情绪断言、编造的时长、
  编造的进度、越界的称呼、破人设的话术，一律拒绝并转本地兜底。
- 它**改不了任何状态**：情绪、关系分、记忆生命周期、主动发送全部在它够不着的地方。

`judge_*` 五个函数（情绪/好感/主动/表达/生活细化）是**休眠的辅助接口**：
有实现、有单元测试，但**没有接进生产路径**，由
`test_goal10_no_judge_is_wired_into_the_production_path` 锁住。

> 要接入其中任何一个，请连同本文一起改，并说明确定性安全网怎么继续兜住它。
> 设计底稿 `DESIGN-living-companion.md` 描述的是「LLM 提议 + 确定性安全网」的
> 目标形态，**不是当前生产行为**。
