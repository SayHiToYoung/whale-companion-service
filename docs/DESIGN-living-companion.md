# 「活着的陪伴」五子系统整合设计底稿

> ## ⚠️ 已废弃：历史设计资料，不是当前行为
>
> **本文保留作为设计意图的历史记录，不再描述系统的实际行为。**
> 任何冲突一律以下面三份权威文档为准：
>
> | 权威文档 | 取代了本文的哪部分 |
> | --- | --- |
> | [`CONTEXT-PIPELINE.md`](CONTEXT-PIPELINE.md) | 上下文如何组装、事实如何带来源 |
> | [`COMPANION-POLICIES.md`](COMPANION-POLICIES.md) | 好感度、表达学习、主动候选生命周期、生活连续性的**实际**策略 |
> | [`ACCEPTANCE.md`](ACCEPTANCE.md) | 上述行为的可执行验收口径 |
>
> **最重要的一处出入**：下面「核心设计原则」一节写的是
> 「LLM = 心脏，五个 `judge_*` 已接入」。**当前生产路径并非如此。**
> 五个 `judge_*` 函数有实现、有单元测试，但**都没有接进生产路径**——
> 生产上模型只做一件事：把已经定好的上下文说成人话。
> 这条不变量由 `test_goal10_no_judge_is_wired_into_the_production_path` 锁住。
> 详见 [`COMPANION-POLICIES.md` → LLM 的位置](COMPANION-POLICIES.md#llm-的位置)。

> 工作底稿：从 menglimi/astrbot_plugin_private_companion（作者已授权照搬）提取设计，
> 映射到 whale-companion-service 的 1:1 私聊 HTTP/PWA 宿主。
> 五个子系统：生活连续性、自我时间线、表达学习、好感度状态机、主动候选生命周期。

## ⚠️ 核心设计原则（主人钦定，2026-09-09）

> **AI 情绪陪伴要的不是确定性，是 LLM 的不确定性。**
> 人和人的关系都充满不确定性，AI 和人的关系更该如此。

**两层分工，不可颠倒：**

- **确定性 = 骨架/安全网（一票否决）**：记忆事实、L1/L2/L3 边界、越界账本、免打扰、
  幂等、审计 trail。这些必须确定——否则会编造、越界、损坏数据。
- **LLM = 心脏（带温度、带随机、带她自己的判断）**：她的**感受**（情绪）、**关系**（亲近还是受伤）、
  **主动意愿**（想不想找你、说什么）、**语气**（怎么说）。这些交给 LLM 去"感觉"，不做成
  regex 规则 / score 机械映射。

**当前状态（已改，2026-09-09）**：五个模块已改为「LLM 提议 + 确定性安全网」——

- **LLM（心脏）**：`judge_*` 函数。情绪（`judge_companion_emotion`）、好感度
  （`judge_affinity_event`）、主动（`judge_proactive_decision`）、表达
  （`judge_expression_candidate`）、生活细化（`judge_refinement`）都由 LLM 产出
  判断，带温度 0.9、带不确定性。
- **确定性（安全网）**：`*` 原函数全部保留为 fallback；账本去重/上限/迟滞、
  免打扰、事实边界、幂等、审计这些一票否决的闸门照旧。
- **接入**：`ModelCompanionResponder.judge_json()` 是统一判断层，`MemoryRepository._judge()`
  在模型可用时返回它、不可用时返回 None（自动回退确定性）。

每个 `judge_*` 的签名统一为 `(…, judge)` → `(result, source)`，`source ∈ {"model","fallback"}`。

---

## 0. 宿主约束（whale-companion-service）

- 1:1 私聊，无群聊 / 无 QQ / 无真实天气/设备/定位 API。
- 持久化：SQLite（`MemoryRepository._initialize` 加表）。
- 帧组装：`orchestrator.build_companion_frame` 加模块；系统提示词在 `companion_llm.py` `SYSTEM_PROMPT`。
- 路由：`_dispatch` 加 `/v1/...`。
- 风格：确定性纯函数、可审计、克制（不臆测用户、不把观察说成事实）。

---

## 1. 好感度状态机（已到，值精确）

### 分数与 8 档（普通阶段）
`relationship_policy.py` `_DEFAULT_STAGES`，分数区间 -1200 ~ 1200：

| key | label | min | max | 主动关怀额度 | 调侃/续话/提记忆/日常关怀 |
| --- | --- | --- | --- | --- | --- |
| deeply_distant | 极度疏离 | -1200 | -801 | 0 | 全关 |
| strongly_distant | 强烈疏离 | -800 | -401 | 0 | 全关 |
| distant | 疏离 | -400 | -1 | 0 | 全关 |
| acquaintance | 初识 | 0 | 199 | 0 | 仅续话 |
| familiar | 熟悉 | 200 | 599 | 1 | 全开 |
| close | 亲近 | 600 | 899 | 2 | 全开 |
| intimate | 亲密 | 900 | 1199 | 3 | 全开 |
| deeply_bonded | 深度联结 | 1200 | 1200 | 4 | 全开 |

- 专属联结 `owner_exclusive`：主要用户专属，分数冻结不自动增减、close 基线，豁免越界判定。

### 互动状态 7 档
avoidant(回避) / hurt(受伤) / relaxed(放松) / lively(活泼) / warm(温暖) / close(亲近) / affectionate(爱意)。close+affectionate 仅主要用户开放。

### 账本机制
- 事件去重 30min；单次增量上限（普通 4 / 底线 12）；每日上限 12。
- 阶段迟滞 20 分（跨档需越过阈值 + 迟滞才切换，防抖动）。
- 自然降温只向 0 回落，3 天宽限；账本限长 200 条。

### 越界与修复
- 按档位差 gap=1/2/3 分轻/中/重，恶意踩底线单独。扣分 轻4 / 中7 / 重12 / 底线14。
- 结果原子写入：账本 + 情绪余波 + 互动状态。
- 修复四路径：自然恢复 / 道歉返 60% 且 3 倍提速 / 同类再犯追回上次恢复 / 第 3 次踩底线降档。

### 主动额度联动
`主动额度 = min(全局, 用户, 关系阶段, 互动状态)`。

### 映射到 whale
- 替换 `companion_runtime/relationship.py` 的 3 档（early/warming/familiar）→ 8 档 + 互动档。
- 新表：`relationship_state`（score/stage/interaction/updated）+ `relationship_ledger`（event 去重账本）+ `relationship_events`（审计）。
- 纯函数模块 `companion_runtime/affinity.py`：`apply_event(state, ledger, event) -> (new_state, ledger_entry)`；`stage_of(score)`；`interaction_of(...)`；`proactive_quota(...)`。
- 最小可移植版：保留 8 档查表 + 账本去重/上限/迟滞/降温 + 越界 4 级扣分 + 修复，互动 7 档简化为从情绪/越界派生。

---

## 2. 表达学习（已到）

### 三者边界（勿混）
- **表达学习**（学"怎么说"）：`user_memory.py` `UserMemoryMixin._expression_*`。
- **表达决策**（消费侧）：`companion_interaction_expression.py` ExpressionBand 7 档 + CONTENT_TIERS 2 档（normal/flirt），与好感度档位联动（`_CONTENT_STAGE_INDEX` deeply_distant=0 … intimate=6）。
- **reaction 表情资产**：贴图/颜文字，不属于表达学习，排除。

### 学习引擎
- 来源：私聊（`_expression_private_learning_source_enabled`，scope 模式 owner/selected/all）+ 群聊（群聊在 whale 无）。
- 白/黑名单：只学短表达、句法、情境风格；**不学**昵称/账号/关系事实/秘密/长句。`_safe_expression_phrase(pattern, 100)` 脱敏校验。
- 去重：`_deduplicate_expression_rule_families`（规则家族去重）；上限 `max_learned_expression_items` 默认 60。

### 审核
- pending / learned 分库；`scope_binding` 要求 approved 才进召回（简化：approved→召回，pending→不进）。
- 证据 examples 只在审核页，只有脱敏 pattern 进召回。

### 召回
- 按 scene（`_expression_scene_from_text`）+ 打分 + 上下文门控选规则；执行优先级：情境表达 > 语法习惯 > 组合规则。
- 注入时 `_format_expression_rule_bundle_line`：`可复用表达“pattern”（instruction）`。

### 映射到 whale
- 新表：`expression_rules`（id/pattern/instruction/scene/kind(style|grammar|combo)/status(pending|approved)/evidence_json/usage_count/created）。
- 纯函数模块 `companion_runtime/expression_learning.py`：`learn_candidates(conversation) -> [rule]`（白黑名单+脱敏+去重）；`review(...)`；`recall(scene, approved_rules) -> [rule]`。
- `speech_style.py` 从 persona 静态规则扩展为「persona 规则 + 已审核表达规则」。
- 表达决策：把 ExpressionBand 7 档 + CONTENT_TIERS 2 档接入 `relationship.stage`（8 档 → 语气/内容档位）。

---

## 3. 主动候选生命周期（已到，全文在 proactive_candidate_lifecycle_spec.md）

### 候选两层
- `impulse`（潜在念头）→ 物化为 `candidate`。
- 关键字段：source/kind/reason/action/topic/motive；时间窗 window_start/preferred/best_until/expire；价值 salience/warmth/urgency/persona_fit/semantic_score/pressure/risk/score(0-100)；去重 signature/origin_event_id/repeat_count。

### 来源（1:1 保留子集）
timer（约定/提醒）、daily_greeting（早/午/晚）、diary_share / state_share、memory_echo / mood_checkin、open_loop / followup、agenda 细化/醒来锚点、meal_care、insomnia_night。
跳过：群聊 group_share / QQ 空间 / 戳一戳 / 语音 / 生图 / 屏幕 / 定位 / 身体监测 / 天气。

### 闸门（1:1 保留子集，按序）
每日上限、最小间隔（×未回应倍率）、免打扰 quiet_hours、用户休息、未回应降速+暂停、Token 硬/软限额、关系边界（friend 降级）、窗口过期、时区作废、低价值过窗、问候已被满足、用户刚活跃。

### 评分
`score = salience + urgency×0.9 + warmth×0.7 + kind_bias + quota_bias + source_feedback(±0.18) + (persona_fit−0.6)×0.36 + (semantic−0.5)×0.42 − pressure(>0.55)×0.22 − risk×0.42 − 年龄×decay/h − 偏离 preferred×0.14/h − 超 best_until×0.25/h − 未回应惩罚`。
调度序 = score + 编排优先级/300。时间窗 = active_window(55min) + grace(80min)。

### 发送前复核
①人格判定 → send/defer/drop/rewrite，命中硬标记（拒绝/免打扰/隐私/越界/串用户/内部机制/捏造/不安全）硬阻断；②正文渲染后 LLM 价值复核 → 连败退避（严格 3 次弃条）。

### 状态机
impulse：queued→deferred→accepted(物化)→planned→generating→reviewing→sent/delivered｜blocked/cancelled/dropped/failed/expired。
候选池 status：queued/accepted/deferred/blocked/cancelled/dropped/sent/failed。

### 映射到 whale
- 升级 `companion_runtime/proactive.py`：候选来源扩到生活事件 + 价值评分 + 完整闸门链 + 未回应降速 + 审计。
- 新表：`proactive_candidates`（candidate + audit 字段，三段正文 preview/route/semantic/status/diagnostic）。
- 纯函数模块拆分：`candidate_sources.py`（Signal→Motive→Candidate）、`gates.py`（闸门链）、`scoring.py`（评分）、`proactive.py`（编排）。
- `/v1/companion/proactive` 升级为返回候选生命周期 + 审计。

---

## 4. 自我时间线（已到，见下文旧稿）

（详见旧稿「子系统：自我时间线」章节，已完整。）

---

## 5. 生活连续性（已到）

### 核心思想
每日状态**不是固定字段，而是由「状态条件 condition」动态合成**；日程分三层（粗计划→临近细化→观测事实）；时型与位置是独立连续体。

### 每日状态（合成式）
`daily_state` 输出：`date / sleep / dream / health / hunger / body_cycle / location / weather(占位) / mood_bias / energy(0-100,基值75) / note / conditions[]`。
- condition：`kind / title / label / mood / energy_delta / intensity / start_ts / end_ts / duration_hours / cause / phase / transition_options / on_end_transition / episode_key`；kind∈{sleep,dream,hunger,body_cycle,health,memory_afterglow,manual_state,care_warmth,cycle_discomfort}。
- 合成规则：`energy = 75 + Σ(condition.energy_delta)`，clamp[10,100]；`mood_bias` = 非「平稳」且 intensity 最高的 condition 的 mood。
- 来源：每日随机池（sleep/dream/hunger/cycle/health）；昨日日记标签「延续」；饭点时间窗；用户手动；关心/吃饭反馈。

### 日程（三层）
1. C3 计划项：`plan_id/title/source_kind=planned/status=temporal_phase/evidence_level/authority_kind/commitment_level/content_granularity/fact_eligibility/confidence`。
2. 当天框架 `daily_plan` 项：`{time,end,activity,mood,message_seed,basis,confidence}`（每日 07:30 生成一次）。
3. 临近时段细化（段前约 3 分钟生成）：补 `summary/location/state_variables[]/today_events[]/proactive_events[]`。
   - today_events：`{window,event,mood,basis,confidence}`（生活正文）
   - proactive_events：`{window,reason,action,why,topic,motive,scene,tone,impulse,chain}`（可分享碎片 + 主动契机）
   - 硬约束：**位置切换必须写清移动过程，location 填段末实际所在处**。

### 时型 chronotype
`chronotype_profile`：`hour_histogram[24] / hist_active_days / explicit_wake_minute / explicit_sleep_minute / learned_wake_minute / learned_sleep_minute`。
- 默认锚点 7:30 醒 / 22:30 睡；双源：消息时间直方图（7 天半衰、≥5 活跃天、总数≥40 才学）+ 显式告知（"我一般两点睡"，TTL 90 天，confidence 0.9 > learned 0.6）。
- 输出 `wake_minute/sleep_minute/source/confidence/shift_minutes(-180~+360)`；影响活跃窗判定与主动概率 boost。

### 位置连续性（最小版可裁剪）
只存「已确认具名地点」不存坐标；`places/routes/current_place_key`；到达/离开生成 confirmed 记忆事件。daily_state.location 来源优先级：对话覆盖(4h TTL) > 细化 location > 日程推断。

### 每日 tick 状态机
- 状态生命周期：当日首次→生成条件一次（记 state_generated_day）→合成；日内每次访问→清理过期条件→注入饭点饥饿→重合成。
- 主动链路：should_send 门禁→人格判定→发送→记回执。
- 生活事件候选：每临近段生成细化→today_events + proactive_events→排程。

### 最小可移植版（1:1 HTTP，无 QQ/天气）
闭环 = 每日状态 + 日程 + 时型 + 细化：
1. 每日状态最小字段 `{date,sleep,dream,hunger,health,body_cycle,location,energy,mood_bias,weather占位,note}` + `conditions[]`；energy=75+Σdelta。
2. 日程粗计划 `{time,end,activity,mood,message_seed}`；细化 `{summary,location,state_variables[],today_events[],proactive_events[]}`。
3. 时型 `{wake_minute,sleep_minute,source,confidence,shift_minutes,hour_histogram[24]}`。
4. 细化触发：段开始前 N 分钟生成当前段；位置切换约束为硬约束。
5. tick：日切→重生成条件；段切→生成细化并排程 proactive_events；每次访问→合成状态。降级：无天气→固定占位。

### 映射到 whale
- 新表：`daily_state`（conditions_json + composed_json）、`chronotype_profile`、`daily_plan`、`refinement`（可选）。
- 纯函数模块：`companion_runtime/daily_life.py`（condition 合成 + 时型 + 日程三层 + 细化 + tick）、`companion_runtime/self_timeline.py`（事件收集 + 召回）。
- 帧里新增 `dailyLife`（energy/mood_bias/当前段/天气占位）与 `selfTimeline`（用户问"你昨天做了什么"时注入）。
- 候选源新增：醒来锚点 / 段细化 proactive_events / 日记 share / meal_care。
