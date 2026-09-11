# 立场层 Walking Skeleton 走通结论

日期：2026-09-11
范围：`hub/schemas/stance.py` / `hub/api/stances.py` / `hub/web/routes_api.py` /
`hub/domain/convergence_eval.py` / `hub/domain/audience.py`
验证方式：`pytest -q --basetemp=$(mktemp -d)` 全量 + `ruff check` + 变异检验

---

## 1. 一句话判定

**五个环节能在同一套代码里跑通，但不是全部都在生产代码里闭环。**

- **环节 1–3（立场模型 / HTTP 传输 / 权限）已经在生产代码里闭环**，端到端走真 HTTP
  请求、真 Bearer 鉴权、真落库、真审计。
- **环节 4–5（收敛判定 / 差异化分发）是纯函数，本次只被集成测试直接驱动**；
  「Stance 行 → `StanceInput` / `FactBase`」这段装配代码目前**只存在于测试里**，
  生产侧还没有任何模块调用它们。
- **最不可靠的一环是差异化分发（`hub/domain/audience.py`）**：出口扫描
  `scan_view` 有可复现的假阳性，会在正常中文文案上把合法消息拦下。

结论：**可以继续投入**，但下一步不该是加功能，而是先把 3→4→5 的装配落到生产代码里，
并把出口扫描的判定方式从「纯文本匹配整数」换掉。

---

## 2. 交付清单

| 项 | 内容 |
|---|---|
| 修改文件 | `hub/api/stances.py`、`hub/web/routes_api.py`、`hub/api/audit.py`、`tests/api/test_stances_api.py` |
| 新增文件 | `tests/integration/test_stance_e2e.py`、`outputs/stance-layer-walkthrough.md` |
| 新增测试数 | **12**（`tests/api/test_stances_api.py` 10 个，`tests/integration/test_stance_e2e.py` 2 个） |
| 全量测试 | **526 passed**（基线 514 → 526），耗时 61.7s |
| ruff | `All checks passed!` |
| 未改动 | `hub/domain/**`（纯函数一行未改）、`hub/schemas/stance.py`、`hub/main.py`、`hub/db/models.py` |

### 新增测试清单

`tests/api/test_stances_api.py`：

1. `test_参与方能读到另一参与方提交的立场`（B22）
2. `test_非参与方读取立场返回404`（B23）
3. `test_读取不存在的参与人立场返回404`（B24）
4. `test_跨事项读取立场返回404`（B25）
5. `test_读取立场写入审计事件`（B26）
6. `test_立场列表只返回本事项且按可见性过滤`（B27）
7. `test_非参与方提交立场返回404`（B28）
8. `test_提交到不存在的事项返回404`（B29）
9. `test_单人读取同样按可见性过滤`（验证时补的旁路边界）
10. `test_读取失败不写审计`（验证时补的审计噪声边界）

`tests/integration/test_stance_e2e.py`：

1. `test_立场层端到端从提交到差异化分发`
2. `test_出口扫描把标题里的版本号误判成未授权用户id_已知缺陷`

---

## 3. 逐环判定

| 环节 | 判定 | 证据 | 卡点 |
|---|---|---|---|
| ① 立场模型 | ✅ 通 | `Stance` 落库、`StanceRead` 出参回填、唯一约束 `(matter, round, user)` 生效 | 无 |
| ② 传输（HTTP） | ✅ 通 | 真 `POST/GET /api/items/{id}/stances[/{user_id}]`，201/200/404/422 均为统一错误形状 | 无 |
| ③ 权限 | ✅ 通 | 成员校验 + 可见性过滤 + 401/404 语义；**变异检验证明非空洞** | 语义裁定需产品确认（见 §6） |
| ④ 收敛判定 | ⚠️ 通但未接线 | e2e 里 support+oppose(risk_appetite) → `converged=False`、分歧类型 `risk_appetite` | 生产侧无调用方；`ConvergenceEvalError` 无兜底（见 §5） |
| ⑤ 差异化分发 | ❌ 接得上但不可靠 | e2e 里两份视图确实不同、`fact_version` 一致、bob 视图含「为什么这次没有采纳」 | `scan_view` 假阳性 + 无结论时消息自相矛盾（见 §5） |

### 3.1 关键接缝：`user_id` 类型不一致（已证实存在，已处理）

任务预告的接缝是真的：`convergence_eval.StanceInput.user_id` 是 `str`，
`audience.FactBase.*_user_ids` 是 `int`，而 `Stance.user_id` 是 `int`。
Python 不会自动转换，e2e 里显式做了 `str(row.user_id)` 与 `int` 映射：

```python
StanceInput(user_id=str(row.user_id), stance=row.stance, ...)
```

**但要注意：这个转换目前只写在测试里**，没有任何生产模块做这件事。
如果下一步有人直接用 `Stance.user_id` 喂 `StanceInput`，`evaluate_convergence`
不会报错、只会静默产生完全不同的 `factions`（字符串和整数永不相等，
faction 分组会把同一个人拆成两组）。**这是一个安静失败的坑，比报错更危险。**

---

## 4. 纪律执行：变异检验（证明测试不是空洞的）

TDD 过程中有 3 处实现是「顺着前一环一起写下去」的，为避免自欺，逐条做了变异检验：
临时破坏实现，确认对应测试**真的会失败**，再恢复。

| 变异 | 结果 | 说明 |
|---|---|---|
| 摘掉 `create_stance` 的事项存在性 + 参与人守卫 | B29 抛 `sqlite3.IntegrityError: FOREIGN KEY constraint failed`（→ HTTP 500）；B28 非参与方拿到 **201** | 证实 B28/B29 捕获的正是任务点名的「本该 404 却 500」 |
| 短路 `_visible`（列表端点） | B27 中发起人 carol 看到 **2** 条而非 1 条 | 证实可见性过滤断言非空洞 |
| 摘掉 `get_stance` 的 `_visible` | `test_单人读取同样按可见性过滤` 中 carol 拿到 **200** 而非 404 | 证实单人端点曾是可绕过列表过滤的**旁路**，已修复 |

第三项是本次验证阶段**新发现的真实权限旁路**：只给列表加可见性过滤、
不给单人端点加，攻击者换个端点就能读到不该读的立场。两处现在共用同一个
`_visible` 谓词。

---

## 5. 已知缺陷与风险（最不可靠的一环）

### 5.1【高风险】出口扫描的假阳性 —— `hub/domain/audience.py:scan_view`

`scan_view` 用 `_INT_TOKEN = re.compile(r"(?<!\d)\d+(?!\d)")` 在**成品自然语言文本**里
找整数，只要某个整数等于事实基座里的参与人 id 且不在 `visible_user_ids` 里，
就判为「泄漏用户 id」。

问题在于：参与人 id 是自增小整数（1、2、3…），而中文文案里裸整数遍地都是。
**最小复现**（已固化为测试 `test_出口扫描把标题里的版本号误判成未授权用户id_已知缺陷`）：

```
事项标题 = "是否上线推荐系统 v2"
用户 1（支持方）的视图 -> scan_view(...).blocked == True
violations == ["出现了未授权的用户 id: 2"]     # 2 来自 "v2"，不是用户 id
```

后果：**合法的消息会被拦下**，而且是随机拦——文案里出现「v2」「3 个方案」
「提升 2 成」这类内容就会触发。这不是接线引入的问题，是 `scan_view`
自身的设计缺陷，端到端串联把它暴露了出来。

改成 `(?<!\w)\d+(?!\w)` 能修掉「v2」，但会漏掉「用户2」（中文无空格）这类真实泄漏，
是按下葫芦浮起瓢。**根因是「用文本扫描判断结构化事实」，正确做法是让视图引用
结构化 id 列表、扫描比对结构而不是比对字符串**——这是设计改动，不适合塞进本次接线任务，
所以本次**没有修**，只固化了复现。

### 5.2【中风险】无结论时的分发消息自相矛盾 —— `build_audience_view`

`FactBase.decision` 允许 `None`，但 `build_audience_view` 对反对者的分支不检查它。
实测输出（`decision=None`，即**收敛未达成、尚无结论**）：

```
结论: 这事目前还没定下来。
为什么这次没有采纳: 这次结论的依据是：收益明确，但风险尚未有兜底方案
```

同一份消息里既说「还没定下来」又解释「为什么没被采纳」。语义上「差异化分发」
的前提是**已经形成结论**；在收敛未达成时给反对者发「未采纳」通知是错的。
接线层必须保证：`decision is None` 时不走「未采纳」分支（或干脆不调用分发）。

### 5.3【中风险】`ConvergenceEvalError` 在生产侧无兜底

`evaluate_convergence` 遇到未知 `stance` 值直接抛 `ConvergenceEvalError`。
但 `stances.stance` 在库里是自由 `String(32)`，没有 CHECK 约束、没有枚举兜底。
一旦库中出现非法值（历史数据、其他写入方、后续新增立场类型忘了同步
`_STANCE_WEIGHTS`），调用方会拿到未处理异常。
`hub/api/` 目前**没有任何代码调用它**，所以这个雷还没被踩，但接线时必须先决定：
是「脏数据跳过并记审计」还是「整个收敛判定失败」。

### 5.4【低风险】`convergence_eval` 的分布是封闭硬编码集合

`_STANCE_WEIGHTS` 是硬编码闭集。新增立场类型必须同时改两处（schema 枚举 +
权重表），否则触发 5.3。

---

## 6. 我做的语义裁定（需要产品/设计确认）

任务没有明确定义读权限边界，我按「复用既有唯一事实源」的原则裁定如下：

1. **读 = 事项成员**（发起人 **或** 参与人），复用 `matters.get_matter_for_user`。
   非成员一律 **404**（不用 403，避免泄露「该事项存在」）。
2. **写 = 必须是参与人**。发起人若未勾选参与（`initiator_participates=False`），
   也不能提交立场。写侧 404 文案与「事项不存在」保持**同一句**，不区分。
3. **可见性在成员之上再做一层过滤**：`visibility="participants"` 的立场
   仅真正的参与人可见；`visibility="all"` 的立场事项成员都可见。
   这让 `visibility` 字段有了可观测语义（否则该字段是死的），
   代价是**发起人但非参与人默认看不到任何立场**。
4. **单人读返回该 `(matter, user)` 下 `round_number` 最大的那条。**

第 3 条是本次唯一的自由发挥，也是最可能需要推翻的假设：
如果产品期望「发起人应该能看到全部立场」，那 `visibility` 的语义要重新定义。

---

## 7. 下一步该先打哪

**优先顺序（按投入产出）：**

1. **把 3→4→5 的装配写进生产代码**（最重要）。
   现在「Stance 行 → `StanceInput`」「Stance 行 → `FactBase`」这两段转换只活在测试里，
   是最容易在真实使用中静默出错的地方（见 §3.1 的 str/int 陷阱）。
   建议落成 `hub/api/stances.py` 里的 `to_stance_inputs(session, matter_id)` /
   `to_fact_base(...)`，并把 `str(user_id)` 转换**封在这里**，不让调用方自己转。
2. **修出口扫描的判定方式**（§5.1）。这是唯一会「拦下正确消息」的缺陷，
   属于安全性功能里的可用性事故。要么改成结构化比对，要么让 `AudienceView`
   携带 id 白名单而不是靠正则猜。
3. **定义「无结论时不发未采纳消息」**（§5.2）。
4. **给 `ConvergenceEvalError` 定策略**（§5.3）。
5. 确认 §6 的三条权限裁定，特别是可见性语义。

---

## 8. 诚实边界（本次没有做的事）

- 端到端测试走的是**进程内 ASGI（`TestClient`）**，不是 `tests/integration/` 里
  那种真 uvicorn 子进程。理由：本文档验证的是「五个环节能否串起来」，
  进程内 ASGI 已经覆盖路由/序列化/鉴权/落库全链路，且确定性更好。
  `tests/integration/` 里另有需要后台 worker 的用例仍走真 uvicorn。
- 环节 4、5 **在生产侧没有调用方**。本次只证明了它们能接上，没有证明
  它们被接上。
- §5.1 的缺陷**没有修**，只固化了复现。修它属于设计改动，超出「接线走通」范围，
  硬修会有按下葫芦浮起瓢的风险。
- 未做并发/幂等验证：同一 `(matter, round, user)` 并发提交的竞态行为本次未测。
