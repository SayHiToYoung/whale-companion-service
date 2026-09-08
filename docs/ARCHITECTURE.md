# 共享陪伴服务架构

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

`orchestrator` 只按顺序组装这些结构化输出，不生成台词。控制台可查看完整
`CompanionFrame`，用于区分理解错误、状态错误、动作错误和表达错误。

## 记忆检索打分

`select_companion_memories` 仍用 relevant / important / recent 三个桶产生候选并写进轨迹，
但顺序不再由桶优先级决定，而是三个信号加权：相关度 0.5、重要性 0.3、时近性 0.2。

相关度按查询覆盖度归一，且完整词命中与字符二元组命中不同权（二元组 0.3）。二元组是中文在
没有分词和向量时的召回兜底，但"今天"这类二元组能撞上几乎任何记忆；不区分权重时，一次巧合
命中会把无关记忆顶到重要且新鲜的记忆前面。轨迹里保留 `blendedScores`，控制台可复核排序理由。

## 跨会话的情绪连续性

除 `open_threads` 外，所有帧模块都只看当前这一轮。`emotional_threads` 表保存用户亲口说出的
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
它每轮直接进模型上下文，不参与检索、不衰减，定位是 **constraint 而非 topic**：大鲸据此理解
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
