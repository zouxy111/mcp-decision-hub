# PRD-07 · confidence 分档外发（roR8Pk 第 2 条）

## 目标

`confidence` 原值（float）目前直出到 API（`hub/db/models.py` → `hub/schemas/stance.py`），且「0.7 硬阻断」语义公开（`hub/domain/convergence_eval.py:22,167`）——组合起来可反推「谁在卡结论」。本块把外发粒度从原值收窄为**分档**。

## 裁决依据（已锁定）

- owner 认可私有字段清单（§8.4-5）：`confidence` 在列表端点**已被裁剪**（`StanceListItem` 不含它）
- 本块处理的是**单读端点**（`StanceRead`）与内部语义的残留泄露面
- 分档是 owner 认可的方向（评估文档建议「分档而非原值」，私有字段清单确认时未反对）

## 做什么

1. 分档纯函数（建议 `hub/domain/stance_ttl.py` 旁新建或并入 domain 合适模块）：
   ```python
   def confidence_band(confidence: float) -> str  # "low" | "medium" | "high"
   ```
   档位边界建议：`[0, 0.4)` low / `[0.4, 0.8)` medium / `[0.8, 1.0]` high（**写进代码注释：档位是显示粒度，与收敛判定的 0.7 硬阻断无对应关系**——避免档位边界泄露阈值）
2. `StanceRead` 单读出参：`confidence` 原值**移除**，替换为 `confidence_band`
3. 数据库存储**不动**（原值保留，收敛判定内部继续用原值——只收外发）

## 边界（不做）

- ❌ 不改收敛判定逻辑（0.7 阈值内部继续用原值）
- ❌ 不改数据库列
- ❌ 列表端点已经裁掉 confidence，本块不重复处理

## 文件所有权

- `hub/schemas/stance.py`（`StanceRead` 字段替换）
- `hub/api/stances.py`（装配处分档）
- `tests/api/test_stances_api.py`、`tests/api/test_stance_analysis_response.py`

**⚠️ 与 PRD-04 同文件 → 串行。**

## TDD 切片（红 → 绿）

| 片 | 红测试 | 绿实现 |
|---|---|---|
| 1 | `confidence_band` 边界：0.39→low / 0.4→medium / 0.79→medium / 0.8→high | 纯函数 |
| 2 | 单读响应**不含** `confidence` 数值键、含 `confidence_band` | 出参替换 |
| 3 | 收敛判定行为零变化（内部仍用原值）：既有 convergence 测试不翻红 | 回归 |

## ⚠️ 断言翻转

凡断言了 `confidence` 原值出参的既有测试 → 翻转为断言 `confidence_band`，逐条登记。

## 验收标准

1. 3 切片全绿
2. 全库 grep：API 出参路径上不再有 float confidence 直出（`StanceRead` 无 `confidence: float` 字段）
3. 档位边界与 0.7 阈值**不重合**（防反推），且有注释说明
4. 全量门槛绿 + 翻转台账
