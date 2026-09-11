# whale-companion-service

可独立运行和开发的 AI 陪伴大脑。它持有唯一的记忆、PersonaCard、CompanionFrame、关系、
情绪线程、主动陪伴、日常生活与对话状态，并通过统一 API 服务 Web、手机、桌宠或其他客户端。

桌宠不是启动前提。未安装、未启动或未配置桌宠时，服务仍可正常聊天；桌面活动与窗口观察
只是一种可选输入。没有对应的已验证事实时，系统会保持未知，不向模型暗示它看见了桌面。

## 架构边界

```text
Web / 手机 PWA / 可选桌宠 / 其他客户端
                  │ API v1
                  ▼
         MemoryApiServer
                  │
    SQLite 权威状态与现有陪伴模块
                  │
       ContextFragment 投影
                  ▼
         ContextAssembler       ← 唯一上下文组装入口
                  │
          CompanionFrame v5
                  │ model_view()
                  ▼
       LLM 表达或事实型兜底
```

所有客户端共用同一套对话接口，**不存在第二套聊天通道**。这套接口的正式契约由服务自己
提供：`GET /v1/contract`（OpenAPI 3.1，无需口令），细则见
[docs/CLIENT-CONTRACT.md](docs/CLIENT-CONTRACT.md)。

三条不可越的线：

1. 原始事件只进存储；进模型的只有模块处理过、带来源的 `ContextFragment`。
2. `ContextAssembler` 是唯一上下文组装入口，不存在第二套记忆或对话系统。
3. 日程、故事、记忆等模块只能生成候选；只有 `proactive` 可以授权发送。

桌宠 memory protocol v1 和现有适配接口继续保留。桌宠重新接入时仍通过
`POST /v1/memory/batches` 提交 L1/L2/L3，不拥有第二份权威记忆。

## 独立启动

要求 Python 3.10+。服务运行时没有必需的第三方包。

```bash
cd whale-companion-service
python -m whale_companion_service.cli
```

也可以使用兼容启动器：

```bash
python scripts/run-memory-server.py
```

默认地址是 `http://127.0.0.1:47821/`，默认数据库仍是
`~/.dsh-whale-memory/memory.sqlite3`，因此现有用户数据不需要迁移。默认开发口令是
`local-dev-token`；非本机开发环境请显式修改。

未配置 LLM 时，服务使用严格事实型本地回复，健康状态仍为 `ready`。要使用本服务自己的
OpenAI-compatible 模型配置，PowerShell 示例：

```powershell
$env:WHALE_MEMORY_TOKEN = "replace-me"
$env:WHALE_LLM_BASE_URL = "https://api.example.com"
$env:WHALE_LLM_MODEL = "your-model"
$env:WHALE_LLM_API_KEY = "your-key"
python -m whale_companion_service.cli
```

Bash 示例：

```bash
export WHALE_MEMORY_TOKEN=replace-me
export WHALE_LLM_BASE_URL=https://api.example.com
export WHALE_LLM_MODEL=your-model
export WHALE_LLM_API_KEY=your-key
python -m whale_companion_service.cli
```

服务不会读取桌宠配置、桌宠密钥或桌宠虚拟环境。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WHALE_SERVICE_HOST` | `127.0.0.1` | 仅允许回环地址；远程访问应使用 HTTPS 反向代理 |
| `WHALE_SERVICE_PORT` | `47821` | 服务端口；测试可设为 `0` 使用随机端口 |
| `WHALE_DATABASE_PATH` | `~/.dsh-whale-memory/memory.sqlite3` | SQLite 路径，兼容现有数据 |
| `WHALE_MEMORY_TOKEN` | `local-dev-token` | API Bearer token |
| `WHALE_LLM_ENABLED` | `1` | `0` 时强制使用事实型兜底 |
| `WHALE_LLM_PROVIDER_ID` | `openai-compatible` | Provider 标识 |
| `WHALE_LLM_NAME` | `OpenAI compatible` | 健康状态中显示的名称 |
| `WHALE_LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible API 根地址 |
| `WHALE_LLM_CHAT_PATH` | `/v1/chat/completions` | Chat Completions 路径 |
| `WHALE_LLM_MODEL` | `deepseek-v4-flash` | 模型名 |
| `WHALE_LLM_API_KEY` | 空 | 远程模型通常必填；localhost 模型可为空 |
| `WHALE_LLM_TIMEOUT` | `60` | 请求超时秒数，范围 3–300 |
| `WHALE_LLM_TEMPERATURE` | `0.7` | 温度，范围 0–2 |
| `WHALE_LLM_MAX_TOKENS` | `2048` | Provider 上限；陪伴回复仍受内部上限约束 |
| `WHALE_LLM_VERIFY_SSL` | `1` | 是否校验 TLS 证书 |

命令行 `--host`、`--port`、`--db`、`--token` 会覆盖相应环境变量；`--no-llm` 会强制关闭模型。

## 健康检查

`GET /health` 不需要鉴权，也不会探测或等待桌宠：

```bash
curl http://127.0.0.1:47821/health
```

返回内容包括 SQLite readiness、当前对话模式、`ContextAssembler`/CompanionFrame 版本、
支持的客户端和可选桌面观测的缺失策略。模型未配置时 `chat.mode` 为
`grounded_fallback`；只要数据库和对话主链可用，整体仍是 `ready`。

## 无桌宠聊天验证

```bash
curl -X POST http://127.0.0.1:47821/v1/conversation/messages \
  -H 'Authorization: Bearer local-dev-token' \
  -H 'Content-Type: application/json' \
  -d '{"userId":"local-user","deviceId":"web","messageId":"hello-1","role":"user","text":"你好"}'
```

响应中的 `assistantMessage` 是同一轮回复。手机 PWA 位于 `/`；它只连接本服务 API，
无需桌宠。调试台位于 `/debug/`，其中桌宠 `127.0.0.1:47890` 面板是可选联调视图，
桌宠离线不影响服务健康或聊天。

## 客户端契约

```bash
curl http://127.0.0.1:47821/v1/contract
```

返回一份 OpenAPI 3.1 文档，是 Web、手机、控制台、未来桌宠和任何其它客户端共用的
唯一接入面。完整说明见 [docs/CLIENT-CONTRACT.md](docs/CLIENT-CONTRACT.md)。
下面是接客户端时最需要先知道的三件事。

### 幂等键

`messageId`、`deliveryId`、`claimId` 都**由客户端生成**，作用域各不相同：

| 标识符 | 作用域 | 重放会发生什么 |
| --- | --- | --- |
| `messageId` | `(userId)` | 同内容 → `duplicate: true`，回复是同一条；异内容 → 409 |
| `deliveryId` | `(userId)` | 拿回逐字节相同的结果，不会多出第二条主动消息 |
| `claimId` | `(userId, deviceId)` | 响应多出 `duplicateClaim: true` |

`afterMessageSeq` 是**服务端**分配的排他游标，全用户单调递增、跨客户端共享。
断线恢复就是把上次的 `nextMessageSeq` 原样递回来，不需要本地重放或按时间戳猜。

翻页有两个信号：`hasMoreExact` **精确**（服务端多取一条判定，真的还有才为 true），
新客户端只看它即可；`hasMore` 是语义不变的**保守**信号，取满一页就为 true，
保留给旧客户端——只看它的话必须同时要求游标前进，否则最后一页刚好取满时会空转一轮。

### 错误与重试

错误响应统一形状；`error` 是给旧客户端的兼容字段，新客户端读 `code` 和 `retryable`：

```json
{"error": "...", "code": "conversation.message_id_conflict",
 "message": "同一个 messageId 已经用于另一段内容，换一个新的 messageId",
 "retryable": false, "status": 409, "apiVersion": 1}
```

| 情况 | 做什么 |
| --- | --- |
| 网络错误、回执丢了 | **带同一个幂等键原样重放** |
| `retryable: true` | 退避后原样重放；有 `retryAfterSeconds` 就按它退避 |
| `retryable: false` | **不要重试**。改请求或告诉用户 |
| `conversation.message_id_conflict` | 换一个新 `messageId` |

**只看 `retryable`，不要按状态码区间推断。** 绝大多数 4xx 等再久也不会成功，
但 408 与 429 说的是「此刻不行，等一下再来」——它们是可重试的 4xx。
当前没有路径返回这两个码；契约里先写好，将来接限流时客户端不必改重试逻辑。

### 可选的感知输入

`POST /v1/perception/observations` 是一条**完全可选**的输入：桌宠、日历、天气这类
外部来源可以上报带时刻的观察。一个来源都不接，聊天、关系、主动陪伴一切照旧。

```bash
curl -X POST http://127.0.0.1:47821/v1/perception/observations \
  -H 'Authorization: Bearer local-dev-token' -H 'Content-Type: application/json' \
  -d '{"userId":"local-user","observations":[{"observationId":"o1",
       "sourceId":"pet-a","sourceType":"desktop_activity",
       "payload":{"app":"VS Code","context":"写代码"}}]}'
```

原始 `payload` 只进存储；进模型的只有按 `sourceType` 白名单投影出来的最小事实，
过期观察进不了上下文，来源在线也不等于用户在场。
`GET /v1/perception/state` 是只读的来源状态与丢弃轨迹。
细则见 [docs/PERCEPTION-INPUTS.md](docs/PERCEPTION-INPUTS.md)。

## 客户端身份（可选）

请求体 `client` 或 `X-Whale-Client-*` 请求头，可以声明 `clientId / kind / version /
apiVersion / capabilities`。**整块都是可选的**——一个新字段都不发的旧客户端仍然完全合法，
行为与升级前逐字节相同。服务端只记录身份，从不要求任何 capability，
未知的 `kind` 归一为 `other` 而不是报错。

## 测试

```bash
python -m pip install -e ".[test]"
python -m pytest -q
node --test tests/mobile_sync.test.cjs
```

AI 陪伴质量评测默认完全离线，使用临时数据库和 21 个合成场景：

```bash
python scripts/run-companion-evals.py
```

只有显式加 `--real-model`、显式允许 `WHALE_LLM_ENABLED` 且配置非空安全 Key
时才会读取模型配置并请求外部模型；judge 只是自然度建议，不能替代事实、边界
和可靠性的确定性检查。JSON 报告只允许写入项目内已存在的 `eval-results/` 或
`.run/`，默认不保留回复与检查细节，并在落盘前执行敏感数据扫描。
完整用法见 [docs/COMPANION-EVALS.md](docs/COMPANION-EVALS.md)。

`tests/test_standalone_service.py` 会在无桌宠包、无桌面批次的条件下启动真实 HTTP 服务，
验证健康检查、Web 对话、ContextAssembler/CompanionFrame 链路、事实型兜底与服务自有 LLM
环境变量。`tests/test_client_contract.py` 用真实 HTTP 服务锁住客户端契约：契约文档与路由
一致、六个核心接口的请求响应都过 schema、三类幂等键、多客户端读同一份有序历史、
断线从游标恢复、旧客户端零新字段仍可用，以及以上全部在没有桌宠时同样成立。
`tests/test_desktop_contract.py` 只在同级桌宠仓库存在时运行，用于锁定未来重新接入时的
memory protocol v1 兼容性。

`tests/test_state_advancement_audit.py` 锁住状态推进语义：固定时钟重复调用、服务重启
一致性、多客户端交错读取、并发读写，以及事件/账本/故事/主动投递/对话消息五样副作用
一件都不重复累计。`tests/test_perception_inputs.py` 锁住可选感知输入的边界：
没有来源时聊天照旧、原始 payload 只进存储、过期观察进不了 CompanionFrame、
来源离线不得声称实时观看、观察不升格成用户亲口的事实。

## 进一步文档

| 文档 | 内容 |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构全景与子系统所有权 |
| [docs/CONTEXT-PIPELINE.md](docs/CONTEXT-PIPELINE.md) | 原始事件到模型上下文的权威流程 |
| [docs/COMPANION-POLICIES.md](docs/COMPANION-POLICIES.md) | 主动发送、关系权限与 veto 规则 |
| [docs/CLIENT-CONTRACT.md](docs/CLIENT-CONTRACT.md) | 客户端接入面：幂等键、错误码、重试与兼容规则 |
| [docs/STATE-ADVANCEMENT.md](docs/STATE-ADVANCEMENT.md) | 哪些入口改变状态、以什么语义改变 |
| [docs/PERCEPTION-INPUTS.md](docs/PERCEPTION-INPUTS.md) | 可选外部观察的来源状态与新鲜度模型 |
| [docs/COMPANION-EVALS.md](docs/COMPANION-EVALS.md) | 21 个陪伴质量场景、确定性基线与可选真实模型评测 |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | 发布验收矩阵 |
| [docs/DESIGN-living-companion.md](docs/DESIGN-living-companion.md) | 已废弃的历史设计底稿 |
