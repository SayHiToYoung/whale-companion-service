# 共享陪伴服务架构

> **本文是架构全景。** 两个子系统另有权威细则，冲突时以细则为准：
>
> - 上下文如何从原始事件走到模型 → [`CONTEXT-PIPELINE.md`](CONTEXT-PIPELINE.md)
> - 她何时开口、关系允许到哪一步 → [`COMPANION-POLICIES.md`](COMPANION-POLICIES.md)
> - 上述行为的可执行验收口径 → [`ACCEPTANCE.md`](ACCEPTANCE.md)
>
> [`DESIGN-living-companion.md`](DESIGN-living-companion.md) 是**已废弃的设计底稿**，
> 只作历史资料，不描述当前行为。

## 定位

本仓库是小鲸与大鲸的权威共享大脑。桌宠小鲸是感知器官，手机大鲸是陪伴界面，Agent Office
是可选视觉舞台；三者都不拥有第二份权威记忆或人格。

## 边界

```text
dsh-pet-indesktop
  ActivityCollector → local outbox → MemoryServiceConnector
                                      │
                                      │ HTTPS / API v1
                                      ▼
whale-companion-service
  protocol → repository(SQLite) → lifecycle → companion prompt/model
                                      │
                                      ▼
                               mobile PWA

dsh-pet-indesktop ← localhost handoff protocol → dsh-agent-office
```

- 桌宠只能通过 `POST /v1/memory/batches` 上传事实、线索和用户明确情绪。
- 手机 PWA 只通过服务 API 读取记忆、领取开场和写入对话。
- Agent Office 不连接本服务，不读取手机对话，也不持有记忆数据库。
- SQLite 默认路径保持为 `~/.dsh-whale-memory/memory.sqlite3`。
- 协议版本当前为 `1`；不兼容变更必须升级版本，禁止静默改变字段语义。
- `/debug/` 是仅用于本地闭环验证的观察与控制界面；它读取两端权威状态，不创建第三份记忆。

## 长期认识与边界

用户在对话中明确说出的姓名、人生阶段等稳定事实进入 `user_facts`，原话和消息 ID
单独保存在 `user_fact_evidence`。同一 predicate 出现新值时，旧值标记为 `corrected`，
不物理覆盖，以便追踪冲突。用户明确提出的称呼、话题和身份纠正进入
`companion_boundaries`，在模型上下文与输出防火墙中拥有最高优先级。

只有 `confirmed` 用户事实和 `active` 边界进入长期回复上下文；`paused`、`corrected`
和 `forgotten` 条目仍可在本地控制台审计，但不会参与陪伴回复。

## CompanionMind 对话行动层

普通对话不再由情绪状态机直接选择话术。`CompanionMind` 先从最近对话推导当前意图、
话题锚点、上一轮双方表达、场景与建议动作，再交给模型以大鲸人格生成表达。动作包括直接
回答、承接上一轮、接住情绪信号、修复误解、尊重边界和表达角色自身反应。情绪线程只是
输入信号之一，不是所有对话的默认入口。

明确的今日记忆查询仍走确定性事实路由，用户边界仍由输出防火墙强制执行。模型不可用时，
服务使用上下文型最低限度回复，不再机械复述用户原话或套用“最戳你的地方”模板。

`CompanionMind` 现仅作为 API 兼容层。正式生成使用 `companion_runtime/` 下的
`CompanionFrame`，由以下无台词输出的独立模块组成：

- `shared_scene`：当前话题、指代、未完成轮次与用户意图。
- `relationship`：关系阶段、已知程度与当前互动分寸。
- `scene`：时间场景与最近可验证的桌面事实。
- `emotion_state`：明确情绪、置信度与互动气氛；未知保持 unknown。
- `open_threads`：仍未闭合的情绪线程，帧里唯一跨会话延续的状态。
- `inner_reaction`：大鲸基于前述状态形成的主观反应。
- `turn_decision`：本轮唯一主要动作、长度和是否允许提问。
- `speech_style`：只负责把反应表达成人设一致的语言。
- `safety`：事实、心理推断、身份和用户边界约束。

`orchestrator` 调用适配器收集 ContextFragment，再交给 ContextAssembler 仲裁，不生成台词。控制台可查看完整
`CompanionFrame`，用于区分理解错误、状态错误、动作错误和表达错误。

## 记忆检索打分

`select_companion_memories` 仍用 relevant / important / recent 三个桶产生候选并写进轨迹，
但顺序不再由桶优先级决定，而是三个信号加权：相关度 0.5、重要性 0.3、时近性 0.2。

相关度按查询覆盖度归一，且完整词命中与字符二元组命中不同权（二元组 0.3）。二元组是中文在
没有分词和向量时的召回兜底，但"今天"这类二元组能撞上几乎任何记忆；不区分权重时，一次巧合
命中会把无关记忆顶到重要且新鲜的记忆前面。轨迹里保留 `blendedScores`，控制台可复核排序理由。

检索只负责"这一轮先看哪几条"。"这条还记不记得"是另一件事，由下面的激活度决定。

## 记忆衰退与连续激活度

固定过期把"还记不记得"压成一个瞬间的悬崖：48 小时内满血，48 小时零一秒彻底消失。更糟的是
所有记忆共用同一把尺子——桌面上"刚才在写代码"和用户亲口说的难过按同样的速度被抹掉。

`memory_activation.py` 把它改写成一个连续量：

```
activation = retention(按类型的半衰期) × (0.35 + 0.65 × strength)
```

`retention` 只回答"过去多久了"，`strength` 回答"它凭什么值得被想起来"，两者不重叠。
低于 `SPEAKABLE_ACTIVATION`（0.30）的记忆退到背景里，不再由大鲸主动带进上下文。

| 类型 | 半衰期 | 归类依据 |
| --- | --- | --- |
| `transient_context` 临时情境 | 24 小时 | L1 桌面观察、L2 未确认线索 |
| `user_emotion` 用户情绪 | 60 小时 | L3 用户亲口说出的情绪 |
| `shared_experience` 共同经历 | 30 天 | `milestone`/`promise` 等，或被两人聊过 ≥2 轮的情绪线程 |
| `relationship` 关系记忆 | 90 天 | 已确认洞察、互动偏好 |
| `user_fact` 用户事实 | 不衰退 | `user_facts` 中的 predicate |
| `boundary` 用户边界 | 不衰退 | `active` 的边界规则 |

用户事实和边界的 `retention` 恒为 1.0：它们只会被用户改写或撤回，不会被时间冲淡。

`strength` 是六项加权和（合计 1.0）：重要性 0.30、情绪强度 0.20、来源置信度 0.20、
当前话题相关度 0.15、提及次数 0.10、未完成状态 0.05。加上时间衰退，这就是需求里的七个信号。
每次读数都返回逐项分解，控制台可以逐条解释"为什么这条还在/不在"。

记忆类型由三级来源决定，越靠前越权威：持有关系数据的服务端覆盖层 > 记忆自述的 `memoryClass`
> 从 `kind` / `layer` 推出的默认值。被两个人反复聊过的一次情绪会被服务端改判为共同经历，
半衰期从 60 小时变成 30 天——它已经不只是那天的情绪了。

### 强化、纠正与四种用户意图

两条线互不代替，**用户意图永远压过激活度**：

- **强化**：每被再次提及一次，半衰期延长 60%（最多算 5 次），且"上一次被碰到"推到现在。
  计数来自既有的 `memory_mentions`，不另立一份账。用户说"它还没过去"会记一笔
  `user_recall`；`POST /v1/memory/reinforce` 是显式入口。强化只改分量，不改状态，
  也**不能**让 suppressed 的记忆复活——那是边界，不是遗忘。
- **纠正**：新增 `corrected` 状态。用户说"你记错了"时原始事件不被改写，但这条不再可说，
  也不再作为下一轮的对话焦点。纠正的判定排在"已解决"之前，否则一条记错的内容会被
  标成"已解决"而留在原地。
- **解决 / 暂停 / 禁止再提**：`resolved` / `dormant` / `suppressed` 语义不变，仍然只由
  用户明说触发，时间永远不能替用户宣布一件事结束。

### 进上下文前的净化

原始事件永远不直接进模型。`context_adapters.memory_fragments` 是唯一通道：
**筛选（激活度）→ 事实校验 → 去重 → ContextFragment**。

- 事实校验按层白名单投影字段，并校验 layer 与 `sourceType` 自述一致。一条自称
  `user_stated` 的桌面观察会被整条丢弃——否则它进仲裁后就凭空升了一级证据强度。
  缺少本层必备证据（L1 的 app、L2 的 statement、L3 的 label）或时长非有限非负的，同样丢弃。
- 去重按投影后的内容签名合并同一件事：证据 id 取并集，时长这类可加量求和，
  其余字段必须逐字相同才算同一条。
- 激活度在记忆这一档内部排先后（`Priority.MEMORY + activation×50`），预算不够时先丢激活度低的，
  但仍压在未完成对话之下——它决定顺序，不决定真假。

## 日摘要与周摘要

`memory_digest.py` 定义日/周摘要的骨架：时间段、记忆 id、类型与层级分布、情绪标签计数、
激活度最高的几条、未完结的记忆 id。日切口径与既有 `daily_episodes` 逐字一致。

骨架是确定性的，同样的记忆在任何时刻重建得到逐字节相同的对象。**本阶段 `summary` 恒为空、
`status` 恒为 `pending_summary`**：写出摘要正文需要一次额外的模型调用，留给后续阶段。
摘要写在 `memory_digests` 表，跟着 `POST /v1/memory/batches` 一起落库（一批全是已知记忆时跳过），
经 `GET /v1/memory/digests` 读取，并出现在控制台快照里。摘要不进模型上下文。

## 跨会话的情绪连续性

早期只有 `open_threads` 跨会话；现在关系、每日状态和自我时间线也有独立持久化状态。`emotional_threads` 表保存用户亲口说出的
L3 情绪所开启的线程；`open_threads` 把仍处于 `active`/`acknowledged` 的线程读回帧里，让大鲸
知道上次那件事还没说完。

线程只能由 `explicit_emotion_label` 命中的用户原话创建，本模块不新增任何情绪推断。带回来的
情绪按半衰期衰减而不是到期作废：情绪半衰期 1.5 天，用户亲口交代的约定（`follow_up_hint`）
半衰期 3.5 天，权重低于 0.25 才退出当前轮。帧里因此带着连续的 `carryWeight`，隔得越久越轻。

`emotion.userEmotion` 只表示用户这一轮说出的情绪，`emotion.carriedEmotion` 是低置信度背景，
两者语义不同且不可互换。carriedEmotion 永远不会让 `mayStateAsFact` 变真，因此
`safety.doNotInferEmotion` 仍然生效——大鲸可以记得，但不能把它当成用户此刻的状态说出来。
用户亲口交代的约定通过 `relationship.commitments` 进入上下文。

## 常驻认知（门牌）

情景记忆走召回是抽卡：对用户的了解取决于本轮抽到哪几条。常驻认知补上"固化"这一步——
它每轮作为常驻认知候选进入统一组装器，不参与情景检索、不衰减，但仍服从边界与上下文预算，定位是 **constraint 而非 topic**：大鲸据此理解
用户，但不主动把它当话题复述。

当前两块门牌，全部由用户明说过的内容聚合而成：

| 门牌 | 内容 | 来源 |
| --- | --- | --- |
| 「TA的事」 | 用户是谁 | `user_facts` 中 `confirmed` 且有固化措辞的 predicate |
| 「我们之间」 | 用户明说过的互动偏好 | `active` 边界 + `emotional_threads.need` |

`standing_knowledge.py` **不推导任何用户没有明说过的事**。从 L1 桌面观察聚合生活规律
（"最近主要在写代码"）在证据上属于观察而非用户陈述，已明确排除在本层之外；L1 仍然以单条
事实的形式参与召回。

常驻认知在读取时推导，不落第二份副本：来源行本身已是权威且可通过
`POST /v1/profile-memories/actions` 纠错，再存一份只会与来源漂移。每条带 `basedOn` 指回
来源行，控制台「常驻认知」面板逐条显示印证次数与来源 id。

## PersonaCard 人设模块

`PersonaCard` 独立描述大鲸的身份、关系位置、性格、偏好、厌恶、表达原则和正反例。
卡片由专用编译器转成模型上下文，不包含记忆检索、回复动作或事实安全规则。

人设版本保存在 `persona_versions`。编辑先生成 draft，试演只读取 draft 且不写入正式对话；
publish 后才影响聊天。rollback 不覆盖历史，而是从指定历史版本创建新的 published 版本。
本地控制台提供结构化编辑、编译结果、试演、发布及回滚入口。

## 所有权

| 数据或行为 | 权威所有者 |
| --- | --- |
| 前台应用采样、本地未发送 outbox | 桌宠小鲸 |
| 已接收 L1/L2/L3、生命周期、对话、消费游标 | 共享陪伴服务 |
| 大鲸提示词、模型调用、事实防线 | 共享陪伴服务 |
| 手机消息展示与断线发送队列 | 手机 PWA |
| 桌面/Office 唯一视觉归属 | 桌宠的 AgentOfficeConnector |

## 兼容策略

桌宠仓库的 `scripts/run-memory-server.py` 是过渡兼容入口，只负责读取桌宠现有模型设置并加载
同级的 `whale-companion-service`。服务仓库也提供不依赖桌宠设置的独立启动器。两条启动路径
运行的是同一份服务实现，不复制业务逻辑。

## 活着的陪伴（五个子系统）

`companion_runtime/` 下新增五个确定性纯函数模块，共同补上大鲸的「生活维度」。
设计提取自 menglimi/astrbot_plugin_private_companion（作者已授权照搬），
按本项目哲学精简为不额外调用 LLM 的确定性规则。

当前**实际**策略见 [`COMPANION-POLICIES.md`](COMPANION-POLICIES.md)。
`DESIGN-living-companion.md` 是已废弃的设计底稿，仅作历史资料。

| 模块 | 职责 | 关键入口 |
| --- | --- | --- |
| `daily_life.py` | 时型、每日状态合成（energy=75+Σ条件）、三层日程、临时生活事件、日程推进 | `advance_agenda` / `daily_life_fragments` / `agenda_candidate_signals` |
| `self_timeline.py` | 大鲸自己的行为历史：触发判定、打分召回 | `asks_about_self_timeline` / `recall_self_timeline` |
| `affinity.py` | 七维内部关系状态 + 账本 + 越界修复 + 对外权限 | `apply_event` / `relationship_permissions` / `derive_boundary_state` |
| `expression_learning.py` | 学「怎么说」：学习边界、审核、场景召回 | `learn_candidates` / `review_rule` / `recall_rules` |
| `proactive.py`（升级） | 主动候选生命周期：候选源、评分、闸门链、审计 | `evaluate_proactive_lifecycle` |

生活模块最初通过 `companion-frame-v4` 接入，提供 `dailyLife`、
`selfTimeline`（仅当用户问「你昨天/今天做了什么」）、`learnedExpressions` 三段；
`relationship` 从 3 档升级为 8 档 + 互动状态 + 权限位。

新增持久化表：`relationship_state` / `relationship_ledger` / `daily_state` /
`chronotype_profile` / `self_timeline_events` / `expression_rules`。

新增 API：
- `GET /v1/companion/affinity`、`POST /v1/companion/affinity/events`
- `GET /v1/companion/daily-state`
- `GET /v1/companion/self-timeline`、`POST /v1/companion/self-timeline/events`
- `GET /v1/companion/expressions`、`POST /v1/companion/expressions/review`

## 自主控制（控制台 + 配置）

「活着的陪伴」参数不再焊死在代码里，控制台（`/debug/`）可直接调，改动落库、跨重启保留。

- 配置表 `living_config`：`proactive.dailyLimit / minIntervalMinutes / noResponsePauseAfter`、
  `affinity.warmDelta / violationDelta`、`expressionLearning.enabled / maxItems`、
  `chronotype.wakeMinute / sleepMinute`。默认值见 `memory_server.py` 的 `DEFAULT_LIVING_CONFIG`。
- 控制端点：
  - `GET/POST /v1/companion/living-config`（读写配置）
  - `POST /v1/companion/affinity/adjust`（`{score}` 直接设分 / `{delta}` 微调 / `{reset}` 重置）
  - `POST /v1/companion/chronotype`（`{wakeMinute, sleepMinute}` 设作息）
  - `POST /v1/companion/expressions/review`、`/delete`（表达审核/删除）
  - `POST /v1/companion/self-timeline/delete`（删时间线事件）
- `debug_snapshot` 的 `living` 块把好感度/每日状态/时型/时间线/表达/配置一次性给控制台渲染。

## 旧 LLM 判断层（辅助接口，**未接入生产路径**）

以下是历史设计。辅助函数和单元测试保留供兼容，但**没有任何一个被生产路径调用**——
`MemoryRepository._judge()` 定义了却无人调用，五个 `judge_*` 也只出现在自己的定义和单测里。
生产上模型只做一件事：把已经仲裁好的上下文说成人话。

这条不变量由 `test_goal10_no_judge_is_wired_into_the_production_path` 锁住：
往 `memory_server.py` 里接入任何一个 `judge_*` 都会让它失败。
要接入时，请连同 [`COMPANION-POLICIES.md`](COMPANION-POLICIES.md) 一起改，
并补上「模型给出越界提议时安全网兜住」的用例。

`DESIGN-living-companion.md` 中 LLM 决定关系和主动意愿的底稿，与当前确定性原则不同，已废弃。
当前行为以下面的写入、状态、读取和主动候选路径为准。

- `ModelCompanionResponder.judge_json(task, context, fields, fallback)`：统一结构化判断层。
  要求模型只输出一个 JSON 对象，解析失败/未配置/超时一律静默回退到 fallback，不阻断主链。
- 五个模块各有一个 `judge_*` 函数（`(…, judge) -> (result, source)`，source ∈ model/fallback）：
  `judge_companion_emotion` / `judge_affinity_event` / `judge_proactive_decision` /
  `judge_expression_candidate` / `judge_refinement`。
- `MemoryRepository._judge()` 在模型可用时返回 `judge_json`，否则返回 None → 全部走确定性兜底。
- 确定性原函数（`update_companion_emotion` / `derive_affinity_event` / `learn_candidates` /
  `refine_segment` 等）全部保留为 fallback；账本上限/迟滞、免打扰、事实边界、幂等、审计
  这些一票否决的闸门照旧，LLM 判断不越过它们。

## 第一阶段：统一上下文架构

```text
原始事件 → repository 事件存储 → 各确定性状态模块 → 权威状态表
                                                 ↓
当前消息 → 模块适配器 → ContextFragment → ContextAssembler → CompanionFrame v5
                                                               ↓ model_view()
                                                    近期对话 / 事实 / 模块指令 → LLM 表达

日程 / 记忆 / 未完成线程 → 主动候选 → proactive 闸门与排序 → send / defer / drop
```

### 写入路径与状态处理路径

桌面批次仍通过 `POST /v1/memory/batches` 校验并幂等落库，原始事件保留在存储与本地审计中。
用户消息通过原事务写入，对话事实、边界、情绪线程继续由原有提取和生命周期代码处理。
`_observe_interaction_locked` 使用 `derive_affinity_event`、`apply_event`、`learn_candidates`、
`update_companion_emotion` 更新好感度、待审核表达和角色情绪，并写自我互动事件。
角色情绪写入从帧组装中移到消息事务，同一 messageId 重试不会重复改变状态。
每日状态继续由原有惰性 tick 合成；日程模板、衰减、关系权限算法不重写。
LLM 不决定记忆衰退、亲密度变化、故事进度或主动发送，也不在数据库事务里调用模型。
记忆的激活度、生命周期状态和日/周摘要骨架同样是确定性的，摘要正文本阶段留空。

### 上下文读取路径

`orchestrator.build_companion_frame` 只调用 `context_adapters.collect_context_fragments` 和组装器。
适配器是迁移边界：旧函数可以继续提供结构化状态，但不能绕过统一组装器进入模型。

| 输入模块 | 进入上下文的投影 | 不进入模型的内容 |
| --- | --- | --- |
| memory retrieval | 三桶召回 → 激活度筛选 → 事实校验 → 去重后的 L1 观察摘要、L2 线索、L3 用户陈述，保留记忆 ID | 原始活动样本、任意 payload 字段、检索分数、激活度分解、日/周摘要 |
| 用户事实 / 边界 | confirmed predicate/value；active 规则及结构化屏蔽条件 | 纠错历史、完整 evidence 原话流水 |
| open threads | 衰减后仍有效的线程摘要、约定，保留 thread/message 来源 | 提及次数、内部权重和完整历史 |
| relationship / affinity | 关系阶段、互动方式和权限 | 好感分数、账本 |
| daily life | 日程推进出的当前活动、精力、心情底色、位置与至多两条可分享小事 | 完整日程、conditions 流水、临时事件池、主动候选、过期日期状态 |
| self timeline | 用户自查时召回的 confirmed 摘要，逐条保留事件 ID | detail、未确认或过期事件 |
| learned expressions | 已审核且通过原有敏感表达检查的说法 | examples、审核证据和使用分数 |
| proactive policy | 当前关系的主动权限、唯一决策器及允许动作 | 候选评分、时间窗、队列状态和发送审计 |
| story | `StoryProvider.context_fragments`；默认 empty/currentNode=null | 本阶段没有故事生成或进度引擎 |
| persona / style | 原人格编译器输出和表达约束，同样服从边界与预算 | 外部旧 profile 上下文的旁路注入 |

### 统一协议与仲裁

`ContextFragment` 字段：`module / priority / facts / instructions / source_event_ids / confidence /
created_at / expires_at / token_budget / sensitivity / metadata`。
每个 `ContextFact` 包含 `key / value / source_event_ids / confidence / created_at / expires_at /
source / lifecycle`；未指定的来源、置信度和时间继承片段，事实可缩短但不能延长片段生命周期。
相同 key 表示同一命题的互斥值；不同时点事件应使用不同 key，避免把不同经历误判为冲突。

优先级：用户边界 > 当前消息与场景 > 未完成对话 > 有据记忆 > 关系权限 > 当前生活 > 故事节点 > 风格。
组装器先处理过期和隐私，再按 key 合并同值证据；冲突时用户边界胜出，其余按置信度、来源
（用户陈述 > 已确认 > 观察 > 推导 > 默认）、时间仲裁，并用稳定排序打破平局。
不同模块的高优先级不能把弱证据提升成事实。被否定来源不会被合并到胜出值的来源列表。
场景命中、查询词命中决定同优先级内容的相关性顺序。

`metadata` 只用于 `scenes / blocked_terms / blocked_modules / blocked_keys / deny_sensitivities`
等控制信息，不复制到模型。`internal / private / secret` 默认排除；边界规则可进一步收紧。
现有称呼和话题边界提取具体屏蔽词，先过滤低优先级内容，再把边界指令交给模型。
自然语言边界不做任意语义推理；新模块应使用明确的 key 或结构化屏蔽条件。

预算按完整 `model_view()` JSON 的 UTF-8 字节数作为保守 token 上界计量（无 tokenizer 依赖），
默认总额度 48000；每个模块同时受片段预算和调用方 module_budgets 的较小值限制。
来源、时间、指令和近期消息均计入；首个超大事实不会破例放入，不截断半条事实。
近期对话最多 8 条；当前消息及边界装不下时抛出 `ContextBudgetError`，repository 返回
`generationBlocked=context_budget_exceeded` 的诊断帧并走已有本地回复，禁止调用模型。
固定系统规则和传输封装不属于动态上下文预算。

### CompanionFrame 与模型边界

v5 是可 JSON 序列化的 dict-compatible `CompanionFrame`：

- `version`: `companion-frame-v5`。
- `recentConversation`: 有界 role/content，仅供当前轮理解。
- `processedFacts`: 仲裁后的 key/value/module/source/source_event_ids/confidence/created_at/expires_at/lifecycle。
- `moduleInstructions`: module/text 及来源、置信度、创建/失效时间。
- `internalMetadata`: 预算单位、总额和使用量，或生成被阻止的原因。
- 原 `sharedScene / relationship / openThreads / dailyLife / …` 字段保留为诊断 API 兼容投影，
  不作为模型输入，也不额外占用动态模型预算。原始事件和状态写回对象不进入这些投影。

`ModelCompanionResponder.reply` 的旧参数签名保留；没有帧的调用方先经过兼容适配器组装。
实际请求只读取 `frame.model_view()`；旧 shared_memory、CompanionMind、standingKnowledge、
personaCard 不再被独立拼入请求。持久化 v5 帧读回时也重新经过来源与过期校验。
保留旧 dialogue_state 标签以兼容现有请求断言，其值只来自已入选的 dialogueState 事实。
输出仍执行既有事实、身份边界和表达风格防线。

### 主动候选路径及阶段范围

所有来源统一交给 `evaluate_proactive_lifecycle` 排序、检查节律和用户边界，
再返回 send/defer/drop。响应保留 `shouldSpeak / selected / content / kind / reason / vetoReason`
的旧语义，新增顶层和逐候选 `decision`。帧读取和模块适配器都不发送主动消息。
持久化候选队列与跨轮 send/defer/drop 状态迁移见上一节，已在第三阶段落地；
故事生成系统仍不在范围内。

## 虚拟人日程与主动触达

日程和主动是两件事，中间有一道明确的授权边界：**日程负责产生生活状态和主动候选，
proactive 负责决定 send / defer / drop。** 日程模块连发送需要的依赖都没有导入
（既不碰 `memory_server`，也不碰 `sqlite3` 或任何 responder），它造得出候选，造不出消息。

### 三层日程

| 层 | 变化频率 | 内容 |
| --- | --- | --- |
| 固定节律 | 只随 chronotype 变 | 起床 / 上午 / 午饭 / 下午 / 傍晚 / 睡觉六段，段段相接、跨午夜闭合 |
| 每日日程 | 每天变 | `build_daily_agenda` 按日期种子决定下午那一段具体做什么 |
| 临时生活事件 | 一天内出现又过去 | `transient_life_events` 排出当天的插曲，每件只在自己的时间窗里存在 |

三层都由日期种子确定性生成：同一天永远算出同一个结果，仿真测试可以把时钟快进一整天。

### 日程推进

`advance_agenda` 把时钟推到 `now`，输出此刻的 current activity、energy、mood bias、
location 和可分享事件。位置换了必须交代 `movedFrom`，否则她会凭空出现在另一个房间。

持久化的 `daily_state` 是精力的唯一权威：传了它，`advance_agenda` 就不再自己合成一份，
否则同一时刻会算出两个不一样的精力值。临时事件在 `_daily_state_locked` 里参与合成，
但不写进 `conditions_json`——窗口一过它就该消失，而不是留在当日流水里。

### 只输出处理过的 ContextFragment

`daily_life_fragments` 是日程进模型的唯一形态，值里只有
`stateKind / period / activeWindow / energy / moodBias / currentActivity / location / movedFrom`，
外加至多两条可分享小事。完整日程、状态条件流水、临时事件池、主动候选一律不进模型。
指令里写明这是她自己的虚拟生活，不是对用户处境的判断。

### 统一闸门链

所有候选源（到期约定、情绪线程、事实报信、日程信号）汇到
`evaluate_proactive_lifecycle`，按固定顺序过同一条链，命中即否决并留下 `vetoReason`：

| 序 | 闸门 | vetoReason |
| --- | --- | --- |
| 1 | 用户情境 | `user_still_chatting` / `user_resting` |
| 2 | 免打扰 | `quiet_hours`（到期约定例外） |
| 3 | 每日上限 | `daily_cap` |
| 4 | 最小间隔 | `too_soon` |
| 5 | 用户未回应次数 | `no_response_pause` |
| 6 | 关系权限 | `relationship_boundary` / `interaction_converged` |
| 7 | 候选价值 | `low_value` |
| 8 | 有效时间窗 | `window_not_open` / `window_expired` |
| 9 | 系统预算 | `token_hard_limit` |

用户边界压过全部九道闸门，直接 `user_boundary`。窗口过期和价值不足是 `drop`（不必再等），
其余是 `defer`（换个时候还能说）。时间窗是可选的：约定和情绪线程没有"过点作废"这回事，
早安和饭点关心有——晚三个小时再说就不是关心了。

### 候选队列：延后、过期、防重复

`reconcile_candidates` 按 signature 把这一轮新算出的候选和上一轮留下的合成一个队列：

- 已经说出口的（`sent`）永远不再回队列。**这是防重复那道锁**：同一个念头不会因为轮询说第二遍。
- 留在队列里的保留**第一次出现的时间**。每轮重算若把年龄清零，一条候选可以永远不老，
  价值衰减和窗口过期都会失效。
- 上一轮留下、这一轮没有依据的：有时间窗的留到窗口过期，没时间窗的直接消失——依据没了就是没了。

签名都带当天日期（`daily_greeting:2026-03-05:morning`）。只按正文算签名的话，
第二天那句一模一样的"早安"会被当成"已经说过"而永远不再出现。

### 只读评估与落定发送

- `GET /v1/companion/proactive`：纯只读评估，轮询多少次都不改变状态，也不算作"说过"。
- `POST /v1/companion/proactive/deliveries`：评估并落定，是主动消息真正算作说过的唯一入口。
  与 `claim_opening` 同构，同一个 `deliveryId` 重放拿到同一份结果。

落定后 `daily_sent` / `noResponseStreak` / `lastProactiveAt` 才会推进——不从持久化的
发送记录里读，这三个数字永远是 0，每日上限、未回应降速和最小间隔三道闸门形同虚设。
最小间隔同时看两个口：普通回复和上一次主动开口。

新增表 `proactive_candidates`（signature 主键 + 状态机 + 时间窗 + 发送时刻）。
已发送的记录保留 3 天供防重复使用，过期候选每次落定时清出队列。

`MemoryRepository(clock=...)` 可注入时钟，仓库内部所有时间戳都走它——
仿真测试因此能把一整天快进一遍，而不必改系统时间。

## 亲密度与关系权限

亲密度不是一个控制语气的数字。一个数字没法同时表达"很久没联系但还很熟"和
"刚吵完架但仍然信任"——而这两种状态在一段真实关系里都存在。

### 七个内部维度

| 维度 | 取值 | 由什么改变 | 时间尺度 |
| --- | --- | --- | --- |
| `score` | -1200..1200 | 有据可查的事件账本 | 3 天宽限后 3%/天 向 0 |
| `stage` | 8 档 | score 跨阈值 + 20 分迟滞 | 跟随 score |
| `interaction` | 7 档 | 阶段 + 近期越界 / 暖意 | 即时 |
| `familiarity` | 0..1 | 每次真实互动 +0.01 | 半衰期 180 天 |
| `trust` | 0..1 | 正向 +0.02，越界 −0.15 | 半衰期 30 天 |
| `warmth` | 0..1 | 正向 +0.12，越界 −0.4 | 半衰期 2 天 |
| `boundaryState` | open / guarded / restricted | **只由用户明说决定** | 不随时间变 |

三个连续维度的时间尺度差了两个数量级，这正是它们不能合成一个数字的原因。
信任掉得比涨得快得多；熟悉度连吵架都不会减少——认识就是认识了。

`boundaryState` 不参与降温：用户划下的线不会因为时间到了就消失。

老库只有 `score / stage / interaction` 三列，启动时 `ALTER TABLE` 补列，
`normalize_affinity_state` 给缺的维度补零值——不需要迁移脚本，老数据直接可用。

### 对外只给权限

内部评分从不进模型。模型拿到的是 `relationshipPermissions`，只说能做什么：

`allowProactiveCare` / `allowSharedMemory` / `allowNickname` / `allowPlayful` /
`allowIntimateTone` / `proactiveQuota`

权限由四样东西共同决定，**任何一样都能单独把门关上**：阶段（有多熟）、互动档（此刻的气氛）、
信任（够不够格更进一步）、边界态（用户划的线）。所以分数到了但信任没跟上，昵称和更亲密的
表达一样不开——它们要的不是熟，是被信任过。

`GET /v1/companion/relationship` 把内部七维和对外权限分开陈列，供控制台复核，两者永不混谈。

### 关系变化的五条硬约束

1. **要有证据**：没有 `eventKey` 的事件不改变任何东西。关系不会因为"感觉上该变了"而变。
2. **每日上限**：正向增量每天至多 12 分，一天之内长不快，但第二天额度重开，仍然长得动。
3. **阶段迟滞**：跨档要越过阈值 ±20，标签不会在边界上抖。
4. **降温只向中性**：每个维度按自己的尺度回落，从不越过中性，也从不朝远离中性的方向走。
   负分不自动回弹——裂痕不会自己长好，只能靠修复事件补回来（道歉返还六成，不是全部）。
5. **打扰买不到关系**：主动消息发出 30 分钟内的用户回应标记为 `solicited`，
   这类正向增量每天另设 2 分的小额度。负向增量不受此限——多打扰几次不能让人更喜欢你，
   但确实可以让人更烦你。

用户划线时收敛的是**行为，不是关系**：`boundaryState` 转 `restricted`，全部权限归零，
但 `score` 和 `stage` 一分不动。她没有因为被划线就"不喜欢你了"。
`profile_memory` 新增 `contact` 类边界识别"别主动找我"这类对行为本身的限制——
它不是话题限制，必须单独识别，否则只会被当成一句普通抱怨。

### 权限约束其他模块

`ContextAssembler` 的控制模块从 `boundaries` 扩为 `{boundaries, relationshipPolicy}`。
控制模块可以声明 `blocked_modules` / `blocked_keys` / `blocked_terms` / `deny_sensitivities`，
且**自身不受屏蔽**——否则一个被约束的模块可以反过来把约束它的那个挡掉。

结构性屏蔽只在"用户划了线"或"关系已经疏离"时生效；其余情况权限仍是给模型的分寸提示，
不去悄悄改变既有的上下文组成：

| 权限关闭 | 被挡在上下文外的模块 |
| --- | --- |
| `allowPlayful` | `learnedExpressions` |
| `allowSharedMemory` | `openThreads` |
| `allowProactiveCare` | `dailyLife` |
| `boundaryState == restricted` | 以上全部 + `story` |
| `allowIntimateTone` | 敏感级 `intimate` |

主动链路同样按权限分类闸门：`daily_greeting` / `meal_care` / `mood_checkin` 归
"主动关心"额度管（`care_not_permitted` / `care_quota_used`），
`memory_echo` / `open_emotion` 需要 `allowSharedMemory`（`shared_memory_not_permitted`）。
`agenda_share` 不归这两者管——她分享自己今天做了什么，和她主动来关心你，不是同一件事。

## 最小故事线

故事在这里不是内容库，是**状态机**。它不生成情节，只记住"你们走到哪儿了"，
并且只在现实里真的发生了什么的时候往前走一格。

两类线，同时最多各一条：**共同故事**（你们之间的，主线）和**她自己的生活故事**（轻支线）。
再多就成了连续剧。

### StoryArc

`id / type / premise / status / current_beat / prerequisites / related_memory_ids /
next_eligible_at / completed_beats / available_branches`，外加 `weight` 和 `started_at`。

`started_at` 和 `updated_at` 必须分开：后者每走一格都会变，用它算"种下去多少天"会一直归零。

状态机：`eligible → active → (paused ↔ active) → completed`。

### 进度只由真实事件驱动

每个节拍的 `advance_when` 是一组声明式条件，由已落库的状态求值：

| 信号 | 来源 |
| --- | --- |
| `relationship_stage_at_least` | `relationship_state` |
| `known_fact_count_at_least` | `user_facts` 中 confirmed 的条数 |
| `thread_mentions_at_least` / `thread_resolved` | `emotional_threads` |
| `timeline_events_at_least` | `self_timeline_events` 中的日记 |
| `days_since_start_at_least` | `started_at` 到现在 |
| `user_choice` | 用户显式选的分支 |

**认不出的条件一律不满足**——未知不等于放行。没有对应的事实、记忆、关系阶段或用户选择，
故事就停在原地；它绝不会因为"该有进展了"而有进展。走完一格要冷却 6 小时，
否则那不是故事，是进度条。

有分支的节拍**必须等用户选**，条件够了也不自己走：她不替用户决定故事往哪边去。
分叉之后靠节拍上的 `next` 指路，不能再按顺序往下数。

### 只输出当前节点

进上下文的只有 `arcId / type / premise / currentBeat / availableBranches`。
走过的节拍、完整节拍表、前置条件全部留在状态里。`related_memory_ids` 作为
`source_event_ids` 走证据通道，不进正文。

### 故事只能造候选

`story_candidate_signals` 产出 `story_beat` 候选，和日程产出的候选走同一条闸门链。
故事模块里没有 `memory_server`、没有 `sqlite3`、没有 responder——它没有绕过 proactive 的路。
`story_beat` 与 `memory_echo` / `open_emotion` 同受 `allowSharedMemory` 管：
共同故事讲的就是你们之间的经历。

### 暂停与恢复

关系权限、用户边界、免打扰任何一样不满足，故事就**停下**——不是讲慢一点，是停下。

- 共同故事需要 `allowSharedMemory`；她自己的生活故事不需要——那是她的日子，不是你们的账。
- `boundaryState != open` 或深夜时段：两类都停。
- 用户明确拒绝某条线：暂停 72 小时，**不是抹掉**——他可能只是今天不想聊。
  隔天又端上来不算尊重。

恢复要等条件重新成立且冷却走完，走过的路原样保留。

### 读与写分开

`GET /v1/companion/proactive` 只算不写，故事的推进也一样——只读的轮询不该改变故事状态。
落库只发生在 `POST /v1/companion/proactive/deliveries` 和 `GET /v1/companion/story`。
`POST /v1/companion/story/actions` 是用户选分支或拒绝某条线的唯一入口。

新增表 `story_arcs`。四个模板（两个共同、两个生活）各三到四拍，刻意保持很小：
这是骨架，不是内容系统。
