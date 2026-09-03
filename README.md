# whale-companion-service

小鲸与大鲸的权威共享大脑。这个仓库只负责：

- L1/L2/L3 记忆协议与事实校验
- SQLite 持久化、幂等同步和开场领取
- 记忆生命周期与对话关联
- 大鲸人格提示词和受约束模型调用
- 手机大鲸 PWA

它不包含桌宠窗口、前台应用采集、Agent Office 页面或视觉搬家逻辑。

## 本机启动

```bash
cd /Users/yuyangwei/DeepSeek/whale-companion-service
python3 scripts/run-memory-server.py
```

默认地址为 `http://127.0.0.1:47821/`，数据库仍使用
`~/.dsh-whale-memory/memory.sqlite3`，因此迁移不会丢失已有记忆和对话。

模型配置通过 `WHALE_LLM_BASE_URL`、`WHALE_LLM_CHAT_PATH`、
`WHALE_LLM_MODEL`、`WHALE_LLM_API_KEY` 注入。未配置时自动使用严格事实型回复。

桌宠旧命令 `./.venv/bin/python scripts/run-memory-server.py` 仍可用，它只是兼容启动器。

## 测试

```bash
/Users/yuyangwei/DeepSeek/dsh-pet-indesktop/.venv/bin/python -m pytest -q
```

当前测试同时覆盖服务自身行为和同级桌宠客户端的 API v1 契约。详细边界见
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
