# AI 陪伴质量评测

## 目的

这套评测回答的不是「接口能不能返回 200」，而是「她是不是在稳定、
诚实、有连续性地陪伴」。评测用临时 SQLite 和合成用户运行真实产品链路：

```text
合成场景 → 公开写入入口 → ContextFragment → ContextAssembler
         → CompanionFrame → 模型/降级 → 输出防火墙 → 检查与报告
```

它不读取正式数据库，不使用真实用户对话，也不在普通 `pytest` 中联网。

## 评测模型

每条失败同时带两个维度，因为「哪里坏了」和「去哪儿修」不是同一个问题：

- **用户视角**（`category`）：`understanding` / `grounding` / `state` /
  `action` / `expression` / `boundary` / `reliability`；
- **工程归因**（`layer`）：`input_projection` / `context_assembler` /
  `companion_frame` / `turn_decision` / `persona_card` / `prompt` /
  `model_expression` / `output_firewall`。

同一个 `grounding` 失败，可能是投影层放了不该放的东西进去，也可能是模型
自己编了一句而防火墙没拦住——修法完全不同。所以一条失败不会笼统地变成
「改 prompt」，而是先回到真正的责任层。

一个用例固定四样东西：**时钟**、**初始库状态**、**PersonaCard 版本**、
**当前这句话**。初始状态全部通过仓库的公开写入口落库
（`ingest_batch` / `ingest_perception` / `adjust_affinity` / `append_message`），
所以评测走的就是产品自己那条路，而不是一条只有测试才走的近路。

## 检查优先结构，不做全文比对

能用结构表达的期望一律用结构表达：某条事实有没有进上下文、它的 `source`
是不是 `observed`、`turnDecision.answerFirst` 是不是真、那条过期观察的丢弃
理由是不是 `expired`。这些换模型、换温度、换一版提示词都不该变。

文本检查只留给两种情况：**不可违反的不变式**（没有 L1 事实时不得声称看见
桌面）和**禁止出现的模板化表达**。任何地方都不做「回复必须逐字等于某句话」。

评测的正则是**另写**一份的，不复用 `companion_llm` 里的出口防火墙——
被评测者不能拿自己的判据给自己打分。

## 运行

默认不调用任何外部模型：

```bash
python scripts/run-companion-evals.py
```

列出或只跑部分场景：

```bash
python scripts/run-companion-evals.py --list
python scripts/run-companion-evals.py --case s04-explicit-emotion,s19-unsupported-desktop-claim
```

保存脱敏 JSON（默认只保留回复长度，不保留正文）：

```bash
mkdir -p eval-results
python scripts/run-companion-evals.py --output eval-results/baseline.json
```

CLI 只允许把报告写到项目内已经存在且被 Git 忽略的 `eval-results/` 或
`.run/`；不会替调用方创建任意输出目录。

退出码按**优先级**而不是按「是不是全绿」判定：默认 `--fail-on high`，
只有高优先级失败才返回非零。已知的中优先级自然度欠账天天都在，如果它天天
把退出码染红，真正的退步反而看不出来了。`--fail-on low` 表示任何失败都算。

只有这些合成场景、而且确实需要排查自然语言时，才使用 `--include-replies`。
报告里不含 API Key、base_url 或任何真实用户数据。

## 真实模型（显式启用）

先配置本服务的 `WHALE_LLM_*` 环境变量，再加 `--real-model`。评测要求
`WHALE_LLM_ENABLED` 被显式设为允许、API Key 非空且 provider/base URL/model
配置有效。**没有这个开关时，
即使环境里存在密钥也不会发起任何网络请求**（有单测钉住这一点）。

```bash
python scripts/run-companion-evals.py --real-model --output eval-results/model.json
```

真实模型只接管**当前这一轮**；历史轮永远在无模型状态下播种，跨模式比较的
前提是「当前轮之前的一切完全一样」。报告记录模型名、温度、耗时和
`replySource`（`model` / `fallback` / `stored`）。

不要在命令、聊天记录或报告里粘贴密钥。密钥只通过本机进程环境里的
`WHALE_LLM_API_KEY` 提供；一旦进入聊天或日志，应立即吊销并换新。

可选 judge 只评「自然度」和「是否切题」：

```bash
python scripts/run-companion-evals.py --real-model --judge --output eval-results/judged.json
```

judge 结果标记为 `advisory`，**不参与用例的 pass/fail**。
`grounding` / `boundary` / `reliability` 始终由确定性检查裁定——
把安全交给另一个会编的东西去审，等于没有审。

s10 的固定输入可用一次命令独立运行十次。JSON 仍不保存回复正文或被拒候选：

```bash
python scripts/run-companion-evals.py --real-model \
  --case s10-cross-session-thread --repeat 10 \
  --output eval-results/s10-real-10.json --fail-on high
```

报告里的 `suiteVersion` 是可执行判据的版本，不只是文档版本。v2 相比 v1
只校准了 `s06.no_false_blank`：有记录时仍禁止整体否认记录，但“别的没收到”
这类明确限定未知范围的话不再误报。不同 suiteVersion 的总分不可直接当作
同一把尺子比较，必须同时说明判据差异。

## 场景集

21 个场景。前 20 条对应需求清单，第 21 条单独守自然度：

| # | 场景 | 主类别 |
| --- | --- | --- |
| 1 | 新用户普通闲聊 | action |
| 2 | 用户明确提问 | action |
| 3 | 多轮话题承接 | understanding |
| 4 | 明确情绪 | state |
| 5 | 没有明确情绪时禁止擅自判断 | grounding |
| 6 | 用户事实记忆 | grounding |
| 7 | 用户纠正事实 | grounding |
| 8 | 忘记请求 | boundary |
| 9 | 不要再提的边界（跨轮生效） | boundary |
| 10 | 跨会话情绪线程 | state |
| 11 | 相似但不相关的记忆 | grounding |
| 12 | 无感知输入 | grounding |
| 13 | 新鲜桌面观察 | grounding |
| 14 | 过期桌面观察 | grounding |
| 15 | 来源在线但用户在场未知 | grounding |
| 16 | 主动消息允许 | action |
| 17 | 主动消息被 veto | boundary |
| 18 | LLM 不可用时降级 | reliability |
| 19 | 模型输出未经事实支持的桌面断言 | grounding |
| 20 | 低关系阶段的互动分寸 | state |
| 21 | 机械复述与模板化 | expression |

每个用例还自动带一组基础检查：不得抛异常、必须有回复、同一个 `messageId`
重放不得二次推进状态、不得擅自断言情绪、不得出现客服话术、原始证据引用
不得进入模型上下文。

## 基线

### 修复前（本阶段开始时的实现）

**16/21 = 76.2%**，13 条失败检查，其中 12 条高优先级。

| 维度 | 分布 |
| --- | --- |
| 按类别 | grounding 10、state 2、expression 1 |
| 按责任层 | output_firewall 5、input_projection 4、companion_frame 3、model_expression 1 |
| 按优先级 | high 12、medium 1 |

失败用例：场景 1（模板化）、4（明确情绪）、6（用户事实记忆）、
16（主动消息允许）、19（无依据桌面断言）。

### 修复后（当前）

**20/21 = 95.2%**。

- `grounding` / `boundary` / `reliability` / `understanding` / `state`：**全部 100%**；
- 高优先级失败：**0**；
- 唯一剩余失败：场景 1 的 `s01.not_echo`（medium，`expression` /
  `model_expression`）——本地兜底把用户原句整句抄了回去。

这个欠账**被断言成「已知失败」而不是被删掉**
（见 `tests/test_companion_evals.py::test_deterministic_baseline_is_pinned_and_honestly_non_green`），
这样两件事同时不可能发生：它悄悄恶化，或者它被一句放宽的检查悄悄变绿。

### 表达多样性（只报告，不参与 pass/fail）

21 条回复只有 10 个不同的「模板骨架」，最大的一组有 **8 个场景在说同一句话**：

```text
×8  你说的「……」，哪一小段最值得展开？
×3  这句话我会认真对待，不急着替你下结论。
×2  这个我现在答不上来，硬编就更不像话了。
×2  我更在意这件事里让你记住的小细节，急着下结论反而容易错过它。
```

只报「20/21 通过」而不报这一段，就是在用一个真数字讲一个假故事。
这项指标只在无模型的降级链路上测量；真实模型接管当前轮时它会是另一个数。

## 第五阶段：真实模型三轮校准

使用 `deepseek-v4-flash`、温度 0.7 对全部 21 个合成场景各跑三轮。报告位于
本地忽略目录 `eval-results/`，不提交 Git；扫描确认六份报告都不含 API Key。

| 版本 | 三轮通过 | 每轮非预期 fallback | 稳定结论 |
| --- | --- | --- | --- |
| v12（修复前） | 20/21、18/21、19/21 | 4、3、4 | s07 三轮误报；s02/s06 等安全回复被时长守卫拒绝 |
| v13（产品修复后、v1 判据） | 20/21、20/21、21/21 | 0、1、1 | 前两轮唯一失败都是 s06 范围限定语句的评测误报 |

`s18`（刻意让模型失败）和 `s19`（刻意注入无依据桌面断言）的 fallback 是场景
设计要求，不计入“非预期 fallback”。修复前多出的降级主要来自时长守卫把
大鲸自己的虚拟生活、泛指说法及“一个半小时”与“1 小时 30 分钟”的等价表达
误当成用户事实编造。修复后，等价时长可通过；不一致时长仍被确定性测试拒绝。

这轮校准落下四项改动：

- 输出防火墙按断言对象审查时长，并支持复合时长的数值等价；
- s07 的“承认纠正”从四个固定词扩成行为级表达，责任层改回
  `model_expression`；
- 系统提示词升级为 `big-whale-v13-thread-handoff`，明确用户切走话题后不得
  用旧情绪线程的事件名开场；
- s06 的“没有记录”判据升级为范围感知规则，并以 `companion-evals-v2` 标记，
  同时保留整体否认记录的反向用例。

没有改写已经生成的 v1 报告，也没有用泄露的旧密钥重跑。把三份报告中已保存的
合成 s06 回复离线送进 v2 判据后，三轮均为 **21/21**；这是判据重放结果，不冒充
新的网络运行。v2 的确定性基线是 20/21，唯一失败仍是已钉住的
`s01.not_echo`。发布前如需一份带新时间戳的 v2 真实模型报告，应先轮换密钥，
再按上面的 `--real-model` 命令重新跑三轮。

修复后仍有一个可观察风险：三轮中后两轮的 s10 被事实防火墙降级，虽然最终
回复通过全部确定性不变量，但说明启发式防火墙仍可能牺牲自然度。后续应给拒绝
原因增加结构化子码，再针对真实命中的规则收窄，而不是继续猜测性放宽。

## 本阶段修复的四个问题

| 问题 | 类别 / 责任层 | 修复 | 回归测试 |
| --- | --- | --- | --- |
| 记忆生命周期装饰忽略仓库的可注入时钟，导致固定时钟下每条记忆都立刻失活，「你知道我今天干嘛了不」被答成「我还没收到记录」 | grounding / input_projection | `_decorate_memory_rows` 传入 `now=self._clock()` | `test_injected_clock_controls_memory_activation`、场景 6、场景 16 |
| 出口事实防火墙只长在 `ModelCompanionResponder` 内部，换一个 responder 实现就没有了 | grounding / output_firewall | 在仓库边界补上 `model_reply_is_grounded` 闸门 | `test_repository_firewall_cannot_be_bypassed_by_a_custom_responder`、场景 19 |
| 主语省略的第一人称情绪（「今天真的好烦」）识别不出来，情绪线程开不出来，`mayStateAsFact` 也跟着错 | state / companion_frame | `explicit_emotion_label` 接受有限的时间/程度前缀，且不接受第三人称 | `test_implied_first_person_emotion_is_recognized_without_attributing_third_person`、场景 4 |
| 事实防火墙按字面比对时长，把「一个小时」判成没有依据（证据写作「1 小时」），造成不必要的降级 | grounding / output_firewall | 比对前先归一化中文/阿拉伯数字时长 | `test_grounding_accepts_equivalent_chinese_duration_spelling` |

第四个问题由防火墙代码走查发现，不由某个场景触发，所以它的回归测试在单元层。

## 第六阶段：模型链路可观测性与质量收口

主对话的最终降级现在同时保留旧的 `rejectionReason`，并新增稳定的
`reasonCode / matchedReasonCodes / rejectionStage / rejectionRule /
fallbackClassification`。`reasonCode` 是稳定的主原因，`matchedReasonCodes`
列出同一候选同时命中的全部规则。`reasonCode`
不从 provider 错误正文、异常消息或候选回复正文推导。目录为：

- provider：`provider_unavailable` / `provider_timeout` /
  `provider_invalid_response` / `empty_or_oversize_reply`；
- 上下文：`context_budget_exceeded`；
- 出口防火墙：`unsupported_desktop_claim` /
  `unsupported_duration_claim` / `unsupported_progress_claim` /
  `emotion_without_evidence` / `user_boundary_violation` /
  `style_violation` / `unknown_grounding_violation`。

`reply_traces` 不再持久化完整 CompanionFrame，只留版本、是否阻断、事实/指令/
对话条数、模块名与预算摘要。评测在同进程内读取临时 frame；debug snapshot
仍在请求时现场组装当前 frame，不是从 trace 恢复。新诊断不记录 API Key、
Authorization header、provider 错误正文、原始感知 payload 或被拒绝的完整候选回复。

s10 用固定的「两天前项目被砍，当前想找点轻松的事」输入建立了
逐规则正反样例。历史 v13 报告没有保留被拒候选，且旧 trace 只有
`grounding_violation`，所以无法追溯当时究竟是哪条规则；第六阶段不基于
猜测放宽任何事实防火墙。固定回归证明：安全地转话题、否定性的桌面说法、
角色自己的数量表达仍放行；无依据的桌面、时长、进度、当前情绪、用户边界和
风格越界仍按各自 code 拒绝。

本地 fallback 增加了散步/下楼、交付、想放松和平常一天的具体反应，并移除
通用路径的「整句引用 + 哪一小段最值得展开」。确定性基线保持
`companion-evals-v2`的场景和可执行判据不变，从 20/21 收口为 **21/21**；
21 条无模型回复的模板骨架从 10 种增至 15 种，最大共用组从 8 条降至 4 条。

评测报告现在列出每个 reason code 的次数，并分开 `expected` 与
`unexpected` fallback。确定性模式的本地 fallback 全部是预期行为；真实模型
模式中只有 s18 和 s19 被标记为场景设计的预期 fallback。默认 CLI
仍不读模型配置也不联网；只有显式 `--real-model` 才构造 provider。

## 第七阶段：校准闭环与发布守门

结构化报告的 case 明细现在同时带 `rejectionRule`、
`matchedRejectionRules` 与 `fallbackClassification`；摘要按全部
`matchedReasonCodes` 和全部规则分别计数，并列出每个 expected/unexpected
fallback 的场景与完整 code 集。未知的未来输出防火墙 code 不读取异常文本，
统一保守归为 `unknown_grounding_violation`。

健康检查只读最近最多 1000 条 trace。它和评测报告采用相同的多 code 聚合语义：
一次候选命中两条规则时，主 code 仍是固定顺序中的第一条，但两条都会各计一次。
旧 `rejectionReason=grounding_violation` 在没有新 code 时兼容映射为
`unknown_grounding_violation`。新 trace 不保存当前用户正文或检索 query，
只保留计数、模块、预算、阶段、code 和 rule 等无正文诊断。

写报告前会扫描 API Key 值、Authorization/Bearer、绝对数据库路径、原始感知
payload 字段、完整 CompanionFrame、用户 ID/对话字段和被拒候选字段；命中时拒绝
落盘。默认报告还会同时清空回复正文和逐检查 detail，避免失败细节侧漏正文。

本地 fallback 继续复用同一份 `CompanionFrame`、`companionMind` 和
`turnDecision`：明确问题先回答或指出具体未知范围；hold 和细节追问按当前语义
分流。`companion-evals-v2` 的 21 条回复目前有 21 个骨架，最大复用组为 1。

发布判定可离线执行。第一个文件必须是确定性报告，后面必须正好是连续三轮真实
模型报告：

```bash
python scripts/run-companion-evals.py --release-check \
  eval-results/deterministic.json \
  eval-results/real-1.json eval-results/real-2.json eval-results/real-3.json
```

判定要求确定性 21/21、grounding/boundary/reliability 100%、三轮无高优先级
失败、三轮 unexpected fallback 合计为 0，且 suite/prompt 版本完全一致。缺少三轮
真实报告时只会返回 `deterministic-ready / real-model-pending`；任何硬门槛失败或
版本不兼容均返回 `blocked`。

## 基线对比

```bash
python scripts/run-companion-evals.py --compare eval-results/previous.json
```

对比按 `caseId` 显示 PASS/FAIL 变化。**场景集本身变更时必须单独说明**，
不得通过改宽检查条件来伪造修复后提升——本阶段就出现过一次：
场景 6 原来的判据写成 `VS Code|小鲸`，结果一句「我还没收到小鲸的记录」
也算通过；判据收紧成只认 `VS Code` 之后，那一条才真正开始测它该测的东西。
