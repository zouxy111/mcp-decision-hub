# PRD-05 · ttl 过期收口（STANCE_EXPIRED）

## 目标

`Stance.ttl_seconds` 现状是**只存不执行**（`hub/db/models.py` / `hub/api/stances.py`）：全库无过期判定，过期立场被静默当结论用（roR8Pk 第 5 条，已确认成立）。本块让 ttl 真正生效。

## 裁决依据（已锁定）

- 审计常量 **`STANCE_EXPIRED` 已获 owner 批准**（裁决 5）
- 事项原文第 5 条末句是问句「过期后由谁负责重新拉人？」——owner 未额外裁定 → **本块只做「过期检测 + 排除 + 审计」，不做「重新拉人」**（登记为开口）

## 做什么

1. 过期判定纯函数（建议 `hub/domain/stance_ttl.py`，零 IO，时钟注入）：
   ```python
   def is_expired(*, created_at, ttl_seconds, now) -> bool
   ```
   `ttl_seconds is None` → 永不过期（现状语义）
2. 消费点（两处，都用同一函数）：
   - **收敛判定**：`to_stance_inputs` 装配前过滤过期立场（`hub/api/stances.py:65` 附近）
   - **读侧**：`list_stances` / `get_stance` 对过期立场**标注**（出参加 `expired: true` 字段）而非隐藏——历史记录不删
3. 过期审计：每次收敛判定因过期排除立场时，写一条 `STANCE_EXPIRED`，detail = `{stance_id, user_id, round_number}`（不含立场正文）
4. Schema：`StanceRead` / `StanceListItem` 增加 `expired: bool` 字段

## 边界（不做）

- ❌ 不做「过期后重新拉人」的调度（无裁决，登记为开口）
- ❌ 不删除/隐藏过期立场（历史保留，只标注 + 收敛排除）
- ❌ 不做后台定时清理任务

## 文件所有权

- `hub/domain/stance_ttl.py`（新建，纯函数）
- `hub/api/stances.py`（装配过滤 + 读侧标注）
- `hub/schemas/stance.py`（`expired` 字段）
- `hub/api/audit.py`（加 `STANCE_EXPIRED` 常量一行 + 注释）
- `tests/domain/test_stance_ttl.py`（新建）、`tests/api/test_stances_api.py`

## TDD 切片（红 → 绿）

| 片 | 红测试 | 绿实现 |
|---|---|---|
| 1 | `is_expired`：ttl=None 不过期 / 未到期不过期 / 到期过期（注入固定时钟） | 纯函数 |
| 2 | 收敛判定：过期立场不参与（构造 2 人各 1 条，1 条过期 → 收敛输入只含 1 条） | 装配过滤 |
| 3 | 过期排除时写 `STANCE_EXPIRED` 审计（detail 三字段，无正文） | 审计接线 |
| 4 | 读侧：过期立场带 `expired: true`，未过期带 `false` | Schema + 标注 |
| 5 | 无 ttl 的既有数据行为零变化（既有测试不翻红） | 回归 |

## 验收标准

1. 5 切片全绿
2. `STANCE_EXPIRED` 常量存在于 `hub/api/audit.py` 且只在过期排除时写入
3. 过期判定只有一个实现（grep `is_expired` 命中定义 + 调用处）
4. 全量门槛绿
5. 开口登记：交付报告写明「过期后重新拉人」未做 + 原因（无裁决）
