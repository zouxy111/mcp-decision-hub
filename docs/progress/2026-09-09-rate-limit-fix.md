# MCP 决策中台 · 限流键热修复记录（M4.1）

- 日期：2026-09-09
- 分支：`fix/rate-limit-token-key`（commit 34bc62c）
- 来源：M4 任务 11 手工冒烟（测试机真实 uvicorn 进程）发现的缺陷

## 缺陷

`hub/mcp_server/auth.py` 中 `token_id = token_obj.id`，但 `find_user_by_token` 返回的是
User 对象，token 维度限流键 `token:{token_id}` 实际为 `token:{user_id}`：

- 同账号多 Token 共享 token 维度额度，新 Token 会被误 429
- account 维度在 token 上限更小时永不生效，PRD 9.1 双维度设计落空
- submit 维度键同源受影响
- 方向 fail-closed（偏严），无被打穿风险

## 修复

- `hub/api/tokens.py` 新增 `resolve_user_and_token()` 返回 `(User, AgentToken)`；
  `find_user_by_token()` 保留原签名委托新函数
- `auth.py` 改取真实 `AgentToken.id` 作为 token/submit 维度限流键

## 回归

- 新增 `tests/integration/test_mcp_rate_limit_process.py`（4 项进程级：token 429+Retry-After /
  双 Token 独立桶 / account 跨 Token 共享 / submit ToolError）
- 全量 458 passed（454+4），ruff 零错误
- 测试机（tx-shanghai-2c2g）真实 uvicorn 冒烟 4/4 PASS：
  account 维度证据 dave1=[200,200,200] dave2=[200,200,429]

## 影响范围

仅 MCP 端限流键选择逻辑；阈值、审计、Web 端不变。行为变化：多 Token 账号的 token 维度
恢复 PRD 9.1 设计的按 Token 独立计数。
