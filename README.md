# MCP 决策中台（mcp-decision-hub）

面向 2–5 人小团队的**异步共识工作台**。

一句话定位：每个人的个人决策模型留在本地，个人 Agent 通过 MCP 拉取待办并提交**经本人确认的纯文本输出**，平台负责多轮摘要、定向追问和决议草案，最终由事项发起人拍板。

## 要解决的问题

现有团队协作通常有三个断点：

1. 个人判断依据存在本地，其他成员无法在**不泄露个人模型**的前提下协作。
2. 参与人必须同时在线，异步任务容易丢失，工作状态难以追踪。
3. Agent 可以生成建议，但缺少明确的人类审核和最终决策落点，流程容易停在草稿状态。

## 架构分层

| 层 | 职责 |
|---|---|
| 本地层 | 个人决策模型 + 个人 Agent，模型不离开本机 |
| 中转层（立场层） | 接收经本人确认的纯文本立场，快照留痕 |
| 收敛层 | 多轮摘要、收敛判定、定向追问 |
| 拍板层 | 发起人拍板，决议落库 + 版本乐观锁 |

设计上明确不做：个人模型上传、服务器侧 Agent 文件读写、Agent 间直连/推送/WebSocket。详见 PRD 第 2 章。

## 技术栈

- **FastAPI** + Pydantic 校验层
- **LangGraph**（SqliteSaver）负责轮次编排
- **SQLite**（WAL 模式）+ SQLAlchemy
- **DeepSeek** 作为 LLM（出题 / 摘要 / 追问）
- **MCP** 作为 Agent 接入协议（会话式）
- 依赖用 **uv** 管理

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 配置环境变量
cp .env.example .env
# 至少填写 DEEPSEEK_API_KEY、ADMIN_USERNAME、ADMIN_INITIAL_PASSWORD，
# 并把 SESSION_SECRET 换成随机值（生产环境必须）

# 3. 启动
uv run uvicorn hub.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/login>。管理员账号由 `ADMIN_USERNAME` / `ADMIN_INITIAL_PASSWORD` 在首次启动时创建，首次登录强制改密。

## 测试与代码规范

```bash
uv run pytest tests -q     # 当前基线：567 passed
uv run ruff check .        # 零错误
```

提交前请确保两项都通过。

## 立场提交：content_hash 口径

立场提交（`POST /api/items/{matter_id}/stances`）要求客户端携带 `content_hash`，
服务端按同一口径重算并逐字比对，不匹配直接 422 `VALIDATION_FAILED`。口径冻结如下：

1. 取提交正文**除 `content_hash` 自身外**的全部字段，用 Pydantic 的
   `model_dump(mode="json")` 序列化为 JSON 可表示的值；
2. 对该对象做规范化 JSON：
   `json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`；
3. 对所得字符串按 UTF-8 编码取 `sha256`，`hexdigest()` 即 `content_hash`
   （64 位小写十六进制）。

参考实现见 `hub/domain/digest.py:compute_stance_content_hash`。`sort_keys` 保证
字段顺序无关；`ensure_ascii=False` 加紧凑分隔符保证与客户端按同一文本编码计算的
结果一致。

## 项目结构

```
hub/
  api/          HTTP 接口（账号、Token、审计、立场层）
  domain/       纯函数域（收敛判定、差异化分发）
  graph/        LangGraph 编排
  llm/          DeepSeek 客户端
  mcp_server/   MCP 接入层（工具定义、鉴权、限流）
  schemas/      Pydantic 校验层
  web/          Web 路由与 Jinja2 + htmx 模板
  db/           数据模型与会话
  background.py 后台管线（驱动、恢复、超时扫描）
  config.py     配置加载
  main.py       应用装配
tests/          测试（api / domain / db / graph / llm / schemas / web / integration / ops）
scripts/        运维与冒烟脚本
docs/           产品文档与运维指南
web-ui/         前端脚手架（React 19 + Vite 8，尚未接入后端）
```

## 文档

- [产品需求文档（PRD）](docs/superpowers/specs/2026-08-11-mcp-decision-hub-prd.md)
- [设计文档](docs/superpowers/specs/2026-08-11-mcp-decision-hub-design.md)
- [部署与运维指南](docs/ops/deployment.md)
- [前端（web-ui）说明](docs/web-ui.md)

## 参与贡献

欢迎提交 Issue 与 Pull Request。

1. Fork 本仓库
2. 从 `main` 切出特性分支（如 `feat/xxx`）
3. 本地确保 `uv run pytest tests -q` 全绿、`uv run ruff check .` 零错误
4. 提 PR，说明改动动机、影响范围与验证方式

## 许可证

[MIT](LICENSE)
