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
- 执行 schema 升级（迁移，见 §5）
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
| `RATE_LIMIT_LOGIN_USERNAME_PER_MINUTE` | `5` | Web 登录/邀请消费单用户名每分钟失败上限（仅计失败） |
| `RATE_LIMIT_LOGIN_IP_PER_MINUTE` | `20` | Web 登录/邀请消费单 IP 每分钟失败上限（仅计失败） |
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

## 5. Schema 升级（迁移）

### 机制

项目**不用 Alembic**，用 `hub/db/migrations/` 里的轻量自研机制（单文件 SQLite、
单实例、单写入者，没有多方言负担，只需「版本表 + 有序步骤 + 幂等」三件事）：

| 组成 | 说明 |
|---|---|
| `schema_migrations` 表 | 版本表，记录已应用的 `version` / `name` / `applied_at` |
| `MIGRATIONS` | 有序迁移步骤，`version` 从 1 起严格递增 |
| `run_migrations(engine, *, backup=True)` | 幂等执行入口，返回 `MigrationReport` |

`init_db` 的顺序是 **`run_migrations` → `Base.metadata.create_all`**。顺序不能反：
`create_all` 只会补建缺失的表，**永远不会修改已存在的表**，所以新加的列/约束
必须靠迁移步骤来落地。

### 执行时机与备份

- 每次服务启动（`init_db`）自动执行；已是最新版本时不做任何事
- **迁移前自动备份**：待升级版本非空且 `backup=True`（默认）时，先调用
  `scripts/backup_db.py` 的 `backup_database`（`sqlite3.Connection.backup()`，
  WAL 安全、不停服），备份写到 **数据库文件同级的 `backups/` 目录**
- 备份失败 → **整体中止**，一个迁移步骤都不执行
- 某个步骤失败 → 该步骤事务回滚（不留半成品），已成功的版本保持有效，
  版本号只前进到最后一次成功的步骤；错误以 `MigrationError` 抛出，
  带 `version` / `name`，原始异常挂在 `__cause__`

### 存量库的 baseline 认领

存量库**没有版本表**，无法知道它是哪个版本。判定策略：

> 无版本表 + 业务表已存在（探测 `users` 表）→ 认领为 **baseline v1**
> （v1 是 no-op 标记，仅表示「`create_all` 当时产出的 schema」）

**失效条件**（此时该假设判断错误）：

- 库来自**更旧/不完整的文件备份**，schema 比当前 `create_all` 输出更早（比如
  `stances` 缺整列，而不只是缺 CHECK）——后续步骤会假设 baseline 的形状而失败
- 版本表被删/丢失但 schema 实际是**更新版本**——会被重新认领为 v1，旧步骤重跑
- `create_all` 中途失败的**半初始化库**：恰好有 `users` 但表不全

这些情况在原理上无法自动区分，只能靠**迁移前的自动备份**兜底（见上）。

### 手工检查与回滚

```bash
# 看当前版本（0 表示无版本表 / 未迁移）
uv run python -c "
import sqlite3; c=sqlite3.connect('hub.db')
print(c.execute('SELECT version,name,applied_at FROM schema_migrations ORDER BY version').fetchall())"

# 回滚：停服 → 用 §4 的备份文件覆盖 hub.db → 删掉 -wal/-shm → 重启
```

### 新增迁移步骤

1. 在 `hub/db/migrations/migrations.py` 写一个 `Callable[[sqlite3.Connection], None]`，
   自行 `BEGIN`/`COMMIT`，并在内部做**幂等守卫**（例如「约束已存在就直接返回」）
2. DDL 写成**冻结快照**，不要读 `hub/db/models.py`——模型会继续演进，迁移必须
   永远复现同一结果；后续变更走新步骤
3. 在 `MIGRATIONS` 末尾追加 `Migration(version=N, name="...", upgrade=...)`

### ⚠️ 导入 `hub.main` 就会触发迁移

`hub/main.py` 末尾有模块级的 `app = create_app()`，而 `create_app` 会调用 `init_db`
（迁移入口）。所以 **`import hub.main` 这个动作本身就会用默认配置跑一次迁移**：
模块级那行的 `settings` 来自 `load_settings()`，即默认库 `sqlite:///./hub.db`。

触发条件是「import `hub.main`」，不是「import conftest」。以 `tests/conftest.py:63`
的 `from hub.main import create_app` 为例 —— 它在 **`client` fixture 的函数体内**，
不是模块级，因此：**只导入 conftest 不触发；只有用到 `client` fixture 才触发**。
（随后 fixture 自己那次 `create_app(settings)` 用的是测试库，与默认库无关。）

后果：跑 web / api 测试仍会**顺带对默认库执行迁移**。默认库按**当前工作目录**解析，
所以从仓库根跑测试时，被迁移的就是仓库根的 `hub.db`，同时会在它同级生成
`backups/`（该目录已在 `.gitignore` 中忽略）。

- 部署与本地开发**建议显式设置 `DATABASE_URL`**，避免误动默认库
- 已是最新版本时不备份、不写任何东西（幂等路径零副作用）
- 不要为了规避这一点把 `app` 改成惰性 —— 会破坏 `uvicorn hub.main:app`
  以及依赖该属性的子进程测试

## 6. 健康检查

- `GET /login` — 返回 200 表示服务正常
- `GET /admin/ops` — 管理员查看调度器状态、队列深度（需登录管理员账号）

## 7. 已知限制

- **单实例部署**：SQLite 单写入者，不支持多进程/多实例。WAL 模式允许读写并发。
- **限流内存态**：进程重启后限流计数器清零，属可接受行为（PRD 9.1）。
- **调度器重启延迟**：超时判定最坏延迟一个扫描周期（默认 60 秒），PRD 10.4 已接受。
- **无自动故障转移**：数据库文件损坏时需手动从备份恢复。
- **迁移只认版本表**：见 §5「存量库的 baseline 认领」——无版本表的库一律按
  baseline v1 处理，schema 与之不符时需人工介入。
- **迁移是单写入者的**：不支持多实例并发启动（与上一条单实例部署一致）。

## 8. 日志

应用使用 Python 标准 `logging`。建议配合 systemd 或 Docker 的日志收集。

关键日志来源：
- `hub.background` — 驱动器/扫描器异常
- `hub.api.scheduler` — 扫描周期统计
- `hub.graph` — LangGraph 图执行异常
- `hub.llm.client` — LLM 调用错误与重试
