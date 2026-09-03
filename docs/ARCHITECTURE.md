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
