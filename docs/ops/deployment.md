# MCP 决策中台 · 部署与运维指南

## 1. 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | ≥ 3.13 | sqlite3.backup API 需要 3.12+ |
| uv | 最新 | 依赖管理与运行 |
| SQLite | ≥ 3.35 | WAL 模式 + RETURNING 子句 |
| NTP | 必须 | PRD 10.4 要求服务端时钟同步（超时判定使用 UTC） |

## 2. 安装与启动

```bash
# 安装依赖
uv sync

# 创建 .env（参考下方变量表）
cp .env.example .env
# 编辑 .env 设置 DEEPSEEK_API_KEY、SESSION_SECRET 等

# 启动服务
uv run uvicorn hub.main:app --host 0.0.0.0 --port 8000
```

服务启动后自动：
- 创建数据库表（如不存在）
- 种子管理员账号（首次启动时）
- 启动 drive_worker / resume_worker / timeout_worker
- 恢复中断的轮次与决议（reconciler）

## 3. 环境变量完整表

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./hub.db` | SQLite 数据库路径 |
| `SESSION_SECRET` | `dev-secret-change-me` | Web 会话密钥（生产环境必须修改） |
| `ADMIN_USERNAME` | 无 | 管理员用户名（首次启动创建） |
| `ADMIN_INITIAL_PASSWORD` | 无 | 管理员初始密码（首次登录后强制修改） |
| `LLM_PROVIDER_NAME` | `DeepSeek` | 显示用的服务商名称 |
| `DEEPSEEK_API_KEY` | 无 | DeepSeek API Key |
| `LLM_BASE_URL` | `https://api.deepseek.com` | LLM API 基础 URL |
| `LLM_MODEL` | `deepseek-chat` | 模型名称 |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `120` | 单次 LLM 请求超时（秒） |
| `TASK_TIMEOUT_SECONDS` | `259200` (72h) | 任务默认超时时长（秒） |
| `MAX_ROUNDS` | `10` | 自动推进轮次上限 |
| `INVITE_TTL_SECONDS` | `604800` (7d) | 邀请凭证有效期（秒） |
| `TIMEOUT_SCAN_INTERVAL_SECONDS` | `60` | 超时扫描周期（秒） |
| `RATE_LIMIT_TOKEN_PER_MINUTE` | `60` | 单 Token 每分钟请求上限 |
| `RATE_LIMIT_SUBMIT_PER_MINUTE` | `10` | 单 Token submit_output 每分钟上限 |
| `RATE_LIMIT_ACCOUNT_PER_MINUTE` | `120` | 单账号全部 Token 合计每分钟上限 |
| `POLL_SECONDS_IDLE` | `300` | 无待办时建议轮询间隔（秒） |
| `POLL_SECONDS_ACTIVE` | `30` | 有待办时建议轮询间隔（秒） |
| `CONTENT_ITEM_LIMIT` | `16384` (16KiB) | 单个 answer content 字节数上限 |
| `CONTENT_TOTAL_LIMIT` | `65536` (64KiB) | answers 全部 content 字节数上限 |
| `NOTES_LIMIT` | `8192` (8KiB) | notes 字节数上限 |
| `REQUEST_BODY_LIMIT` | `98304` (96KiB) | 单次 submit_output 请求体字节数上限 |

## 4. 备份与恢复

### 备份

```bash
# 自动备份（应用运行中安全执行，WAL 模式兼容）
uv run python scripts/backup_db.py --src sqlite:///./hub.db --out ./backups

# 输出示例：
# 备份成功: ./backups/hub-backup-20260813-143022.db
# 大小: 1.23 MB
# 完整性检查: OK
```

备份脚本使用 `sqlite3.Connection.backup()` API，不停服即可执行。

### 恢复

```bash
# 1. 停止服务
# 2. 替换数据库文件
cp ./backups/hub-backup-20260813-143022.db ./hub.db
# 3. 如有 WAL 文件，删除（备份已包含完整数据）
rm -f hub.db-wal hub.db-shm
# 4. 重启服务
```

### 定期备份建议

使用 cron 每小时备份一次：
```cron
0 * * * * cd /path/to/mcp-decision-hub && uv run python scripts/backup_db.py --out ./backups 2>&1 | logger -t hub-backup
```

## 5. 健康检查

- `GET /login` — 返回 200 表示服务正常
- `GET /admin/ops` — 管理员查看调度器状态、队列深度（需登录管理员账号）

## 6. 已知限制

- **单实例部署**：SQLite 单写入者，不支持多进程/多实例。WAL 模式允许读写并发。
- **限流内存态**：进程重启后限流计数器清零，属可接受行为（PRD 9.1）。
- **调度器重启延迟**：超时判定最坏延迟一个扫描周期（默认 60 秒），PRD 10.4 已接受。
- **无自动故障转移**：数据库文件损坏时需手动从备份恢复。

## 7. 日志

应用使用 Python 标准 `logging`。建议配合 systemd 或 Docker 的日志收集。

关键日志来源：
- `hub.background` — 驱动器/扫描器异常
- `hub.api.scheduler` — 扫描周期统计
- `hub.graph` — LangGraph 图执行异常
- `hub.llm.client` — LLM 调用错误与重试
