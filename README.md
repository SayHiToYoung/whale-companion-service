# whale-companion-service

小鲸与大鲸的权威共享大脑。这个仓库只负责：

- L1/L2/L3 记忆协议与事实校验
- SQLite 持久化、幂等同步和开场领取
- 记忆生命周期与对话关联
- 有原话证据的长期用户事实、冲突修正与陪伴边界
- 大鲸人格提示词和受约束模型调用
- 可独立调优、试演、发布和回滚的版本化 PersonaCard
- 手机大鲸 PWA

它不包含桌宠窗口、前台应用采集、Agent Office 页面或视觉搬家逻辑。

## 本机启动

```bash
cd /Users/yuyangwei/DeepSeek/whale-companion-service
python3 scripts/run-memory-server.py
```

后台常驻启停：

```bash
./scripts/start-memory-server.sh
./scripts/stop-memory-server.sh
```

默认地址为 `http://127.0.0.1:47821/`，数据库仍使用
`~/.dsh-whale-memory/memory.sqlite3`，因此迁移不会丢失已有记忆和对话。

## 回声闭环控制台

服务启动后访问 `http://127.0.0.1:47821/debug/`。控制台会直接读取：

- 桌宠 `127.0.0.1:47890` 暴露的真实采集、每日汇总与 outbox 状态
- 本服务 SQLite 中的批次、记忆、生命周期、消费游标与对话引用
- 当前大鲸人格版本、完整规则和实际模型记忆上下文
- 长期用户事实与边界的状态、置信度和原话证据

控制台支持手动“整理今天”、强制重新同步、无副作用地重新生成开场，以及删除
`debug_` 开头的测试记忆。修改代码后需要重启桌宠和共享服务，两个状态均显示在线后即可按
“整理今天 → 重新同步 → 生成开场”的顺序验证整条链路。

长期记忆采用可审计状态机：`candidate → confirmed → corrected/paused/forgotten`。
调试台可以纠正、暂停、恢复和软删除事实；边界先于个性化事实进入回复约束。
对应 API 为 `GET /v1/profile-memories` 与 `POST /v1/profile-memories/actions`。

模型配置通过 `WHALE_LLM_BASE_URL`、`WHALE_LLM_CHAT_PATH`、
`WHALE_LLM_MODEL`、`WHALE_LLM_API_KEY` 注入。未配置时自动使用严格事实型回复。

桌宠旧命令 `./.venv/bin/python scripts/run-memory-server.py` 仍可用，它只是兼容启动器。

## 测试

```bash
/Users/yuyangwei/DeepSeek/dsh-pet-indesktop/.venv/bin/python -m pytest -q
```

当前测试同时覆盖服务自身行为和同级桌宠客户端的 API v1 契约。详细边界见
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
