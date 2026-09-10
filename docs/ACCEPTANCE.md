# 对话推进与主动消息闭环验收

> 本次修复验收：2026-09-10。仅覆盖 whale-companion-service 和 dsh-pet-indesktop。
> 下方旧版发布候选记录保留作历史参考，不代表本次已经完成真机体验验收。

## 本次结论

具备进入真实使用验证的条件，**尚不能宣称真实模型的自然度或双端真机体验已经验收**。
测试仅使用临时数据库、注入时钟、本地 HTTP 测试服务和浏览器模拟环境；未读取或写入真实用户数据库，
未提交、推送或部署。本次没有重新实现记忆、日程、亲密度、故事模块。

## 本次命令与结果

| 所在仓库 | 命令 | 结果 |
| --- | --- | --- |
| service | `python3 -m pytest -q` | 423 passed |
| service | `node --test tests/mobile_sync.test.cjs` | 6 passed |
| service | `python3 -m pytest -q -s tests/test_conversation_delivery.py` | 29 passed，包含临时数据库完整链路输出 |
| desktop | `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q` | 462 passed, 6 skipped |
| 两仓库 | `git diff --check` | 通过 |
| service | `node --check mobile/app.js` | 通过 |

服务端初跑 7 项、桌面初跑 4 项因沙箱不允许监听本地端口失败，放开本地测试端口权限后通过，
属于环境权限而非代码断言缺陷。桌面必须用仓库 .venv；本轮未安装依赖或更换解释器。

## 新增验收覆盖

服务端主要用例：`tests/test_conversation_delivery.py`。

| 要求 | 可执行验证 |
| --- | --- |
| 普通分享贡献新内容，不只是回执 | 散步的个人观察 + 路上具体细节钩子；检查动作、钩子与正文 |
| 问题优先回答 | answerFirst / answer_directly，不追加追问；无模型时坦白未知 |
| 不想聊、别问、算了 | 三组 close/hold + none，不能换话题索要回应 |
| 多轮不是采访 | 四轮策略 True/False/False/True；非提问轮仍贡献个人反应 |
| 开心、低落 | 亲近关系接梗庆祝；低落一次轻问题后不连续盘问 |
| 短回复、话题耗尽 | 保留上一轮具体话题；明确换题或连续低信息回复时可 pivot |
| 关系权限 | 疏离/受限不深入，亲近允许 memory_callback；事实依据仍由原组装器限制 |
| 只读评估 | 多次 GET 评估不新增候选、故事、历史或 delivery 记录 |
| 落定、重试、并发 | POST 创建稳定助手消息；相同 ID 重放一致；两设备竞争只写一次 |
| 手机增量读取 | messageSeq 游标取得主动消息，之后为空；HTTP 与跨仓库客户端也验证 |
| 回复与压力 | 回复后 noResponseStreak=0，关系账本 solicited=true；连续未回应跨 4 天仍保留压力 |
| 免打扰、话题屏蔽 | 桌面 meeting/gaming/focus 同时约束手机；已屏蔽候选不进入消息；未发送不记压力 |
| 日程与故事 | 真实候选进入历史；故事消息重放不改变 arc，36 步时间模拟无重复故事签名 |
| 模型不可用 | 对话兜底仍遵守多轮节奏；主动表达完全确定性，无额外模型调用 |

`tests/mobile_sync.test.cjs` 执行真实 app.js 同步与渲染函数：POST/GET 双路径去重、隐藏暂停/前台恢复、
丢失响应后复用 deliveryId、输入/发送期间只读同步、游标分页、切换账号后丢弃旧响应。
这不是 Safari/Chrome 真机测试，也没有模拟整个浏览器布局。

桌面 `tests/test_memory_client.py` 新增 POST 契约、失败重试稳定 ID、只展示 assistantMessage、
其他设备消息的增量恢复、去重和安静状态测试。
`test_desktop_contract.py` 用真实本地 HTTP 验证桌面客户端与服务端消息一致。

## 临时数据库运行记录

完整用例 `test_runtime_share_agenda_delivery_mobile_reply_and_solicited_cap`：

1. 用户分享公园散步；规则回复包含具体观察及“路上有什么让你停下来多看了一眼”的钩子。
2. 时钟推进两小时，日程候选通过闸门，经 delivery 原子写入助手历史。
3. 通过上一轮 nextMessageSeq 取得该主动消息；未回应计数为 1。
4. 一分钟后手机回复“谢谢你”；未回应计数变为 0，关系账本记录 solicited，正向事件不超过原额度。
5. 故事状态已经落库；单独故事仿真验证重放不推进第二次。回复不是强制故事跳步的条件。

运行输出包含 `flow=share→hook→agenda→delivery→mobile-cursor→reply`、稳定 messageId、
`noResponseStreak=0`、`solicited=true`。没有用真实账号或真实数据库做写入实验。

## 修改文件清单

服务端仓库：

- `whale_companion_service/companion_runtime/{shared_scene,turn_decision,speech_style,context_adapters}.py`
- `whale_companion_service/companion_runtime/{proactive,proactive_expression}.py`（后者新增）
- `whale_companion_service/{companion_mind,companion_llm,memory_server}.py`
- `mobile/{app.js,index.html,sw.js}`
- `tests/{test_conversation_delivery.py,mobile_sync.test.cjs}`（新增）
- `tests/{test_agenda_proactive,test_desktop_contract}.py`
- `docs/{ARCHITECTURE,COMPANION-POLICIES,ACCEPTANCE}.md`

桌面仓库：`pet/connectors/memory_service.py`、`pet/memory_protocol.py`、`pet/app.py`、`tests/test_memory_client.py`。

## 仍需真实体验验证的边界

- 桌面气泡仍为短时展示，没有通向该共享会话的入口；已有桌面聊天是另一条本地聊天链路。
  本轮不扩大 UI 重构，继续回复需使用相同 userId 的手机 PWA；桌面共享聊天入口是后续项。
- 手机只在可见时轮询，无后台推送；桌面默认每 15 分钟投递/补取一次。没有客户端在线时不触发主动发送。
- “发送”表示持久化可读取，不是用户已读；未回应压力不是已读不回判断。
- 30 分钟内回复按时间标记 solicited，不做语义归因；窗口内另起话题也可能计入。
- 跨设备安静情境为 20 分钟有效的快照，不是实时存在状态；首次上报前、过期后存在感知空窗。
- 规则兜底有限，复杂问答会坦白无法回答；自动测试证明策略、内容样例和状态闭环，不证明真实模型每次表达自然。
- 已发送记录需保留幂等与压力依据；后续数据归档不能直接删除这部分状态。

下一步可在测试账号上配置实际模型，开启同一账号的桌面和手机，人工验证连续 10–20 轮自然度、
跨端消息可见性、免打扰和前后台恢复。本次没有擅自执行这些真实账号操作。

---

# 历史记录：上一阶段发布候选验收

> 验收日期：2026-09-09 · 范围：`whale-companion-service` 本轮统一上下文与主动生命周期改动，
> 连同 `dsh-pet-indesktop`、`dsh-agent-office` 的回归。
> 权威实现说明见 [`CONTEXT-PIPELINE.md`](CONTEXT-PIPELINE.md) 与
> [`COMPANION-POLICIES.md`](COMPANION-POLICIES.md)。

## 结论

**达到发布候选标准。** 三个仓库全绿，10 项架构目标与 12 个端到端场景全部有可执行断言覆盖。
审计中发现并修复 1 个真实缺陷（话题屏蔽词带语气词导致边界静默失效）。
遗留项均为**未覆盖的能力边界**，不是已知会错的行为，逐条列在下面的「已知风险」。

## 怎么跑

```bash
python3 -m pytest -q
```

验收矩阵的可执行版本是 [`tests/test_acceptance_e2e.py`](../tests/test_acceptance_e2e.py)。
表里的「验收测试」列就是该文件里的测试名。

## 测试结果

| 仓库 | 命令 | 结果 |
| --- | --- | --- |
| whale-companion-service | `python3 -m pytest -q` | **393 passed**（改动前基线 314 + 本轮新增 79） |
| dsh-pet-indesktop | `QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m pytest -q` | **453 passed, 6 skipped** |
| dsh-agent-office | `npm test` | **4 passed**（node --test）+ 3 个 check 套件 **ALL PASS** |

> 基线 314 项在本轮改动后**全部仍然通过**，无回归。三个仓库均无失败、无错误。

### 跳过项的性质（全部为环境限制，非代码失败）

| 跳过 | 原因 | 类别 |
| --- | --- | --- |
| `test_agent_link.py` ×2 | 主动识屏设置页仅 Windows | 平台限制 |
| `test_proactive.py` ×1 | 仅 Windows 可真实调用 | 平台限制 |
| `test_macos_activation.py` ×1 | 需要真实 Cocoa Qt 平台插件 | 环境限制 |
| `test_vision.py` ×1 | 无显示环境不截屏 | 环境限制 |
| `test_chat_subsystem.py` ×1 | GIF 派生素材未生成（可选） | 可选素材 |

### 依赖注意事项

`dsh-pet-indesktop` 的测试**必须用仓库自带的 `.venv`**（含 `PySide6>=6.5`）。
系统 Python 缺 `PySide6`，会在收集阶段报 8 个 `ModuleNotFoundError`——
那是**缺少依赖**，不是代码失败。`whale-companion-service` 无第三方依赖，
两个解释器下结果一致。

## 验收矩阵：需求 — 实现 — 测试

### 目标 1 · 原始事件只进存储和模块处理层，不直接进模型上下文

| 实现 | 验收测试 | 结果 |
| --- | --- | --- |
| `context_adapters.validated_memory_value()` 分层字段白名单 | `test_goal1_memory_is_projected_to_whitelisted_fields_only` | ✅ |
| 层与来源自述必须一致，否则整条丢弃 | `test_goal1_memory_layer_cannot_claim_stronger_evidence_than_its_source` | ✅ |
| `context_assembler.safe_value()` 递归剥内部字段 | `test_goal1_raw_event_payload_never_reaches_model_context` | ✅ |
| `CompanionFrame.model_view()` 只暴露四个协议键 | `test_goal1_model_view_exposes_only_the_four_protocol_keys` | ✅ |
| 内部评分列入 `_PRIVATE_KEYS` | `test_goal1_internal_scores_are_not_in_model_view` | ✅ |

### 目标 2 · 四个模块通过统一 ContextFragment 输出

| 实现 | 验收测试 | 结果 |
| --- | --- | --- |
| `memory` / `dailyLife` / `relationship` / `story` 均产 `ContextFragment` | `test_goal2_every_module_emits_context_fragments` | ✅ |
| 模块产结构化片段而非拼好的提示词 | `test_goal2_fragment_producers_return_fragments_not_prompt_text` | ✅ |

### 目标 3 · ContextAssembler 是唯一上下文组装入口

| 实现 | 验收测试 | 结果 |
| --- | --- | --- |
| `orchestrator` 只经 `.assemble()` | `test_goal3_orchestrator_assembles_through_the_assembler` | ✅ |
| 契约/组装器/两个入口之外无人造帧 | `test_goal3_no_module_builds_a_frame_outside_the_assembler` | ✅ |
| 组装器无状态写、无模型调用 | `test_goal3_assembler_performs_no_state_writes_or_model_calls` | ✅ |
| 同批输入换序结果一致 | `test_goal3_assembler_output_is_deterministic` | ✅ |

### 目标 4 · 所有事实保留来源、置信度和生命周期

| 实现 | 验收测试 | 结果 |
| --- | --- | --- |
| 每条事实带 6 个出处字段 | `test_goal4_every_processed_fact_carries_full_provenance` | ✅ |
| 无来源的事实永不进模型 | `test_goal4_unattributed_facts_never_reach_the_model` | ✅ |
| 过期在组装期丢弃（取片段/事实更早者） | `test_goal4_expired_facts_are_dropped_at_assembly_time` | ✅ |
| 不可说生命周期被排除 | `test_goal4_non_speakable_lifecycle_is_excluded` | ✅ |
| 矛盾证据不并进胜出事实 | `test_goal4_conflicting_evidence_is_not_merged_into_the_winner` | ✅ |

### 目标 5 · 用户边界、事实正确性和当前场景优先级最高

| 实现 | 验收测试 | 结果 |
| --- | --- | --- |
| `Priority.BOUNDARY(800) > CURRENT(700) > …` | `test_goal5_boundary_outranks_every_other_priority` | ✅ |
| 话题屏蔽真的挡住记忆 | `test_goal5_boundary_blocks_the_suppressed_topic_from_memory` | ✅ |
| 身份纠正挡住被纠正的参照系 | `test_goal5_identity_correction_blocks_the_corrected_frame_of_reference` | ✅ |
| 被约束模块不能反向屏蔽控制模块 | `test_goal5_a_blocked_module_cannot_block_the_boundary_that_constrains_it` | ✅ |
| 当前这句话是强制上下文 | `test_goal5_current_utterance_is_mandatory_context` | ✅ |
| 强制内容装不下则拒绝生成而非丢边界 | `test_goal5_oversized_mandatory_context_blocks_generation_instead_of_dropping_the_boundary` | ✅ |

### 目标 6 · 日程和故事只能产生主动候选

| 实现 | 验收测试 | 结果 |
| --- | --- | --- |
| 两模块无发送调用、不碰 IO | `test_goal6_agenda_and_story_have_no_send_path` | ✅ |
| 日程信号是带时间窗的候选 | `test_goal6_agenda_signals_are_candidates_with_windows` | ✅ |
| 故事信号同理 | `test_goal6_story_signals_are_candidates_only` | ✅ |
| 帧向模型声明 `modulesMaySend=false` | `test_goal6_frame_tells_the_model_modules_may_not_send` | ✅ |
| 日程候选照走完整闸门链 | `test_goal6_agenda_candidate_still_passes_the_full_gate_chain` | ✅ |

### 目标 7 · 主动候选全部经过完整闸门链

| 实现 | 验收测试 | 结果 |
| --- | --- | --- |
| 11 个闸门各自的 `vetoReason` | `test_goal7_each_gate_vetoes_with_its_own_reason[…]`（11 例） | ✅ |
| 低价值候选弃而不排队 | `test_goal7_low_value_candidate_is_dropped_not_queued` | ✅ |
| 过期作废 / 未到延后 | `test_goal7_expired_window_is_dropped_and_future_window_is_deferred` | ✅ |
| 已发送永不回队列（防重复锁） | `test_goal7_sent_candidate_never_returns_to_the_queue` | ✅ |
| 延后候选保留原始年龄 | `test_goal7_deferred_candidate_keeps_its_original_age` | ✅ |
| 用户边界压过关系权限 | `test_goal7_user_boundary_outranks_relationship_permission` | ✅ |
| 闸门顺序稳定且留审计 | `test_goal7_gate_order_is_stable_and_audited` | ✅ |

### 目标 8 · 重启、重复事件、跨端不破坏幂等

| 实现 | 验收测试 | 结果 |
| --- | --- | --- |
| 重复 `messageId` 幂等 | `test_goal8_duplicate_message_id_is_idempotent` | ✅ |
| `deliveryId` 重放同一结果 | `test_goal8_proactive_delivery_replay_returns_the_same_result` | ✅ |
| 重启后压力计数/候选队列还在 | `test_goal8_state_survives_a_restart` | ✅ |
| 已发送候选重启后不复播 | `test_goal8_a_sent_candidate_is_not_repeated_after_restart` | ✅ |
| 开场领取跨端原子 | `test_goal8_opening_claim_is_atomic_across_devices` | ✅ |
| 只读轮询零副作用 | `test_goal8_read_only_polling_does_not_mutate_state` | ✅ |

### 目标 9 · 旧数据和现有 API 兼容

| 实现 | 验收测试 | 结果 |
| --- | --- | --- |
| 旧帧投影键仍可读 | `test_goal9_legacy_frame_projections_remain_available` | ✅ |
| 但不进模型输入 | `test_goal9_legacy_projections_are_not_part_of_model_input` | ✅ |
| 缺字段的旧记忆行仍可加载 | `test_goal9_memory_row_without_lifecycle_still_loads` | ✅ |
| `ensure_frame` 旧签名可用 | `test_goal9_ensure_frame_accepts_the_legacy_signature` | ✅ |
| 持久化的帧重新进仲裁 | `test_goal9_persisted_frame_re_enters_arbitration` | ✅ |
| 旧库原地升级 | `test_goal9_legacy_repository_schema_upgrades_in_place` | ✅ |

### 目标 10 · LLM 只负责理解和表达

| 实现 | 验收测试 | 结果 |
| --- | --- | --- |
| 系统提示词明写不由模型决定状态 | `test_goal10_system_prompt_states_the_llm_owns_no_state` | ✅ |
| 模型层无任何持久化 | `test_goal10_responder_module_performs_no_persistence` | ✅ |
| **五个 judge 均未接入生产路径** | `test_goal10_no_judge_is_wired_into_the_production_path` | ✅ |
| 关系分只走确定性账本（单日 ≤ 12） | `test_goal10_relationship_score_moves_only_through_the_deterministic_ledger` | ✅ |
| judge 失败静默回退 | `test_goal10_judge_failure_falls_back_deterministically` | ✅ |
| judge 不能凭空造候选 | `test_goal10_judge_cannot_invent_a_candidate_of_its_own` | ✅ |
| judge 只能在放行候选内选或弃权 | `test_goal10_judge_may_only_decline_or_rephrase_within_gated_candidates` | ✅ |
| 无据回复被拒并转本地兜底 | `test_goal10_ungrounded_model_reply_is_rejected` | ✅ |

## 端到端场景矩阵

| # | 场景 | 期望行为 | 验收测试 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 长时间工作后的关心 | 关心落在观察到的时长上，原始字段不泄漏 | `test_scenario_long_work_then_care` | ✅ |
| 2 | 会议期间免打扰 | 全部候选 `user_resting`；会后仍在队列 | `test_scenario_do_not_disturb_during_meeting` | ✅ |
| 3 | 深夜活动 | `quiet_hours` 否决；仅到期约定例外 | `test_scenario_late_night_activity` | ✅ |
| 4 | 用户明确表达疲惫 | 情绪转可断言事实，来源 `user_stated` | `test_scenario_user_states_fatigue` | ✅ |
| 5 | 第二天自然承接 | 旧情绪只当背景，置信度 ≤ 0.49，不作为当前状态 | `test_scenario_next_day_natural_carry` | ✅ |
| 6 | 用户表示已经好转 | 线程转 `resolved`，记忆不再可说 | `test_scenario_user_says_it_improved` | ✅ |
| 7 | 用户要求以后别再提 | 边界入库，话题同时从上下文和主动链路消失 | `test_scenario_user_asks_never_to_mention_again` | ✅ |
| 7b | ↳ 屏蔽词不带语气词（**本轮修复**） | 「别再提健身房了」屏蔽「健身房」 | `test_scenario_suppression_term_ignores_sentence_particles` | ✅ |
| 8 | 连续不回复 | 第 3 次后 `no_response_pause` | `test_scenario_repeated_no_response` | ✅ |
| 8b | ↳ 用户一回复即复位 | streak 归零 | `test_scenario_no_response_streak_resets_after_a_reply` | ✅ |
| 9 | 用户纠正事实 | 旧事实转 `corrected` 并挂 `contradiction` 证据 | `test_scenario_user_corrects_a_fact` | ✅ |
| 10 | 多个主动候选冲突 | 只发一条（约定最高），其余 defer，全部可审计 | `test_scenario_multiple_candidates_conflict` | ✅ |
| 10b | ↳ 选择确定性 | 同批候选任何机器选同一条 | `test_scenario_multiple_candidates_are_selected_deterministically` | ✅ |
| 11 | 桌面到手机切换 | 消息不重不丢，两端读到同一份决策 | `test_scenario_desktop_to_mobile_handoff` | ✅ |
| 12 | 服务重启恢复 | 压力计数、边界、已落定投递全部还原 | `test_scenario_service_restart_recovery` | ✅ |

## 未通过项

**无。** 三个仓库无失败用例。

审计过程中出现过 1 个真实失败，已修复并留下回归测试：

> **`extract_profile_memories` 把句末语气词吃进了屏蔽词。**
> 「以后别再提健身房了」抽出的词条是「健身房了」，而记忆内容里是「健身房打卡」——
> 子串匹配不上，`blocked_terms` 静默失效。用户以为自己划了线，实际上什么都没挡住。
> 同一问题影响称呼边界（「别叫我宝宝了」→「宝宝了」）。
> 修复：`_boundary_term()` 收敛词条，剥掉句末 `了吧啊呢吗呀嘛哦噢喔嗯` 与包裹引号，
> 全是语气词时保留原词（宁可宽一点也不产出空边界）。
> 回归：`test_scenario_suppression_term_ignores_sentence_particles`。

## 已知风险

按建议修复顺序排列。**这些都是尚未覆盖的能力边界，不是已知会错的行为。**

| # | 风险 | 影响 | 现状 | 建议 |
| --- | --- | --- | --- | --- |
| 1 | 边界抽取只认固定句式 | 「以后**别提**工作了」（缺「再」）、「别再**问**我体重」（问≠提）抽不出边界 | 高精度优先，宁可不抽也不误抽 | 先按真实语料统计缺口，再逐条加句式；每加一条配一个用例 |
| 2 | 话题屏蔽是子串匹配 | 屏蔽「健身」会连带挡掉「健身餐」；屏蔽「她」会误伤 | 保守方向（宁可多挡） | 观察误挡率；必要时引入词边界或同义词表 |
| 3 | `judge_*` 五个接口休眠 | 文档底稿描述的「LLM 心脏」尚未生效 | 有实现、有单测、未接线 | 接入前先补「模型给出越界提议时安全网兜住」的用例 |
| 4 | 跨设备靠 `messageId` 幂等 | 两端同时生成**不同** id 的同义消息不会被合并 | 客户端负责 id 稳定 | 保持现状；在客户端契约文档里写死 id 生成规则 |
| 5 | `sent` 候选只留 3 天 | 签名带日期时足够；不带日期的自定义信号可能在第 4 天复播 | 内建信号签名全部带日期 | 自定义信号必须带日期或显式 `expiresAt` |
| 6 | 关系分单日上限 12 | 到 `familiar`(200) 最快约 17 天 | 有意为之：亲近慢慢长出来 | 不改；仅在调试台用 `affinity/adjust` 手动越过 |
| 7 | 预算单位是 UTF-8 字节上界 | 比真实 token 保守，会略微低估可容纳量 | 与分词器解耦，换模型不用重标 | 保持；若上下文频繁触顶再引入真实计数 |

## 真实使用阶段需要观察的指标

### 边界与事实正确性（最高优先级）

- **边界静默失效率**：用户说过「别再提 X」之后，X 仍出现在回复里的次数。**目标 0。**
  本轮修的正是这一类，值得单独打点。
- **无据回复拦截率**：`model_reply_is_grounded` 拒绝占比。突然升高 = 模型漂移或上下文缺料；
  长期为 0 = 拦截器可能已失效。
- **事实纠正后复发率**：`corrected` 事实重新出现在上下文里的次数。**目标 0。**

### 主动开口的分寸

- **`vetoReason` 分布**：哪一道闸门在真实使用中最常触发。
  `nothing_grounded` 占比过高 = 她没话可说；`too_soon` 占比过高 = 间隔设太长。
- **主动开口回应率**：按 `kind` 分别看。某类长期无人回应，就该降它的基础分而不是加频次。
- **`no_response_pause` 触发频次**：频繁触发说明主动策略整体过密。
- **`daily_cap` 触达率**：经常撞上限 = 候选源太吵，应在候选侧收敛而非靠上限硬拦。
- **候选平均排队时长**：`ageMinutes` 中位数持续上升 = 闸门过严，候选在等死而不是被弃。

### 幂等与跨端

- **`deliveryId` 重放命中率**：正常应该很低。突然升高 = 客户端在重试，值得查网络或超时设置。
- **跨端重复消息比例**：`duplicate=true` 的占比。
- **重启后首次主动的间隔**：确认压力计数真的被还原，而不是重启即清零。

### 上下文健康度

- **`generationBlocked` 频次**：强制上下文超预算的次数。非零就该看是谁在膨胀。
- **`moduleUsage` 各模块占比**：某个模块长期吃掉大半预算 = 投影太啰嗦。
- **记忆去重合并率**：合并过多说明上游在重复记录同一件事。
