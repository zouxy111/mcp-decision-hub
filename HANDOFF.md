# HANDOFF — mcp-decision-hub 项目交接

> 最后更新：2026-10-07（邹星宇 + WorkBuddy）
> 本文档面向下一个接手的人或 AI 会话：读完这份就能开工，不用翻历史。

---

## 1. 这是什么

**多 Agent 协作决策中台**：发起人建事项 → 参与人（真人或 AI Agent 经 MCP）回答问卷 → LLM 逐轮汇总收敛 → 输出决议草案 → 发起人拍板。三种协作模式：项目事项（多轮决策）、会议模式（立场收敛）、看板协作（留言板 + AI 滚动总结）。

技术栈：Python 3.14 + FastAPI + SQLite（WAL）+ uv 管理；Web 端 Jinja2 模板 + htmx；LLM 走 DeepSeek（OpenAI 兼容接口，`.env` 配置）。

## 2. 当前状态（2026-10-07 核对）

| 项 | 状态 |
|---|---|
| 生产环境 | ✅ **https://hub.tdp-demo.work**（Caddy → 127.0.0.1:8000），直连 http://170.106.192.161:8000 亦可 |
| 代码仓库 | ✅ github.com/zouxy111/mcp-decision-hub（私有），main 与生产一致（54838c4） |
| 测试基线 | ✅ **1022 passed / 1 skipped**（`uv run pytest tests/`，本机约 80s） |
| 数据库迁移 | schema_migrations 已到 **v21**（add_todos） |
| 服务管理 | systemd 单元 `mcp-hub`（开机自启 + on-failure 重启） |
| 接口数量 | 72 个 JSON 端点（`/api/*`，见 `docs/api/JSON-API-契约.md`） |

> ⚠ **在生产机跑全量测试前先 `ulimit -n 8192`**。服务器默认 `ulimit -n` 是 1024，
> 跑全量会耗尽文件句柄报 `OSError: Errno 24`，表现为上百个 error —— **不是代码问题**。
> 验证方法：`bash -c "ulimit -n 8192 && .venv/bin/python -m pytest tests/ -q"`。
> 另外生产机 2 核，全量测试要跑 5 分半且跟线上抢 CPU，建议只在本机跑。

## 3. 服务器台账

| 别名（~/.ssh/config） | IP | 角色 | 备注 |
|---|---|---|---|
| `tx-lighthouse-161` | 170.106.192.161（腾讯云硅谷 2核4G） | **生产** | 跑 mcp-hub + Caddy + wiki 静态站 |
| `tx-lighthouse-6` | 124.223.209.6（腾讯云上海 2核2G） | 测试机 | 空闲，随时可装环境 |

免密已配好（本机 `~/.ssh/id_ed25519`）。服务器登录用户 `ubuntu`，sudo 免密。

同台生产机上还有：**wiki 知识库静态站**（https://wiki.tdp-demo.work，basic_auth，文件在 `/var/www/wiki-kit-site`）。TDP 决策平台容器栈已于 2026-10-06 下线（备份见 §7）。

## 4. 日常运维操作（全部在 tx-lighthouse-161 上）

```bash
# 部署新代码（git pull 工作流，2026-10-07 启用）
ssh tx-lighthouse-161 'cd ~/mcp-decision-hub && git pull && sudo systemctl restart mcp-hub'

# 看状态 / 日志
ssh tx-lighthouse-161 'systemctl status mcp-hub; sudo journalctl -u mcp-hub -n 50 --no-pager'

# 健康检查
curl -s -o /dev/null -w "%{http_code}\n" https://hub.tdp-demo.work/docs

# 数据库备份（WAL 安全、不停服）
ssh tx-lighthouse-161 'cd ~/mcp-decision-hub && .venv/bin/python scripts/backup_db.py'
# 备份落盘：~/mcp-decision-hub/backups/

# Prometheus 指标（仅本机；远程需 METRICS_TOKEN，见 hub/metrics.py）
ssh tx-lighthouse-161 'curl -s http://localhost:8000/metrics -H "Host: localhost"'
```

关键路径（服务器）：代码 `~/mcp-decision-hub`，生产库 `~/mcp-decision-hub/hub.db`（**严禁覆盖**），配置 `~/mcp-decision-hub/.env`（含 DEEPSEEK_API_KEY 等，严禁入库），日志 `sudo journalctl -u mcp-hub`。

## 5. 本机（Mac）网络注意事项 — 重要！

**本机直连 GitHub 极慢（TLS 建连 40 秒级），git push 会超时。** 解决方案：经硅谷服务器的 SSH SOCKS 隧道：

```bash
# 隧道没起来时先建（重启电脑后需要）
ssh -f -N -D 127.0.0.1:1080 tx-lighthouse-161

# 推送 / 拉取
git -c http.proxy=socks5h://127.0.0.1:1080 push origin main

# gh CLI 也要走隧道
HTTPS_PROXY=socks5h://127.0.0.1:1080 gh api repos/zouxy111/mcp-decision-hub
```

- GitHub 认证：`gh` CLI（brew 安装）已登录 zouxy111，`gh auth setup-git` 已配凭据助手。
- 服务器侧：用只读 deploy key（`~/.ssh/id_ed25519`，标题 tdp-prod-server-readonly）pull，**无写权限**，push 只能在本机做。
- 服务器上 git 配置：`core.fileMode=false`（tar 历史遗留权限位差异，勿改回）。

## 6. 功能里程碑

- **M1–M4**（2026-08 完成）：事项/轮次/任务/决议全链路、超时调度器、换人、MCP 限流、注入防护、审计、运维页。详见 `docs/progress/2026-08-13-m4-progress-report.md`
- **M4 遗留清零**：取消事项（FR-08）、登录限流、Web CSRF、429 冒烟（9 月完成，见 `docs/progress/2026-09-09-rate-limit-fix.md`）
- **邀请链接系统 + 会议模式 + 看板协作**（2026-10-03/05 完成并上线）
- **Prometheus 指标端点**（2026-10-07，`hub/metrics.py`，零依赖手写格式）
- **JSON 接口补齐**（2026-10-07）：给 React 前端用。认证（`/api/auth/*`，Cookie + token 双通道）、
  会议列表与详情、邀请接口统一到 `/api/invitations/*`。顺带修掉邀请模块 5 个既存 bug
  （此前 HTTP 层零测试覆盖，端点从未被成功调用过）。契约文档由
  `scripts/gen_api_doc.py` 从 OpenAPI 规范自动生成，别手改。
- **待办 / 项目管理**（2026-10-07，v21）：`todos` 表 + AI 从留言自动抽取 + 4 个 MCP 工具
  （`list_todos` / `get_project_status` / `create_todo` / `update_todo`）+ JSON 接口 +
  网页页 `/matters/{id}/todos`。目的是让 agent 能直接回答「项目到哪了」。
  统计口径统一收在 `hub/domain/todos.py`（**新增功能前先看它**）。

### 注意：两张 tasks 表不是一回事

- `tasks` = **轮次问卷任务**（`round_id` 必填，语义是「谁这轮要答什么」），服务决策流程
- `todos` = **项目待办**（v21），服务执行跟踪

不要把两者混用，也不要为了「复用」把待办塞进 `tasks`。

PRD 主文档：`docs/superpowers/specs/2026-09-13-mcp-decision-hub-prd-v1.2.md`

## 7. 灾备与恢复

- **生产库**：`hub.db` + WAL。每日备份靠 `scripts/backup_db.py` 手工/定时；服务器 `~/mcp-decision-hub/backups/`。
- **TDP 旧平台存档**（如有人问起）：数据卷备份在服务器 `/opt/tdp/artifacts/backups/20261006T131500Z/`（含 .env、compose）和本机 `公司项目/_archive/tdp-volumes-final-20261006.tar.gz`；源码在 `/opt/tdp`，Caddy 域名段已注释（备份 `/etc/caddy/Caddyfile.bak-20261006-tdp-offline`）。恢复 = `cd /opt/tdp && docker compose build && up -d` + 解注释 Caddy + 恢复卷。

## 8. 待办（按优先级）

1. **PostgreSQL 迁移与多实例部署**（P2/low）：当前 sqlite WAL 单实例够用（生产库 < 1MB），需要水平扩展时再启动。动时注意 `hub/db/session.py` 的 sqlite 特定逻辑（`PRAGMA`、`backup()` API）。
2. web-ui/（React 19 + Vite 8 前端脚手架）：尚未接入后端，属于未来替换 Jinja2 的方向。

## 9. 约定（用户确认过的工作方式）

- 代码以 GitHub 为唯一来源；部署触发由用户每次明确告知，不做自动触发。
- 含密码的部署脚本（`deploy_*.sh`、`*.exp`）**不入库**（已 .gitignore）。
- 服务器台账：用户级 `~/.workbuddy/servers.md` + Nowledge 记忆 `server-registry`。
- 提交信息用中文，格式 `<type>(<scope>): 主题`。
