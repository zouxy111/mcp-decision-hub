# 立场层 Walking Skeleton 走通结论

日期：2026-09-11
范围：`hub/schemas/stance.py` / `hub/api/stances.py` / `hub/web/routes_api.py` /
`hub/domain/convergence_eval.py` / `hub/domain/audience.py`
验证方式：`pytest -q --basetemp=$(mktemp -d)` 全量 + `ruff check` + 变异检验

---

## 1. 一句话判定

> **2026-09-11 当日判定（原文保留）**：五个环节能在同一套代码里跑通，但不是全部
> 都在生产代码里闭环。
>
> - **环节 1–3（立场模型 / HTTP 传输 / 权限）已经在生产代码里闭环**，端到端走真
>   HTTP 请求、真 Bearer 鉴权、真落库、真审计。
> - **环节 4–5（收敛判定 / 差异化分发）是纯函数，本次只被集成测试直接驱动**；
>   「Stance 行 → `StanceInput` / `FactBase`」这段装配代码只存在于测试里，生产侧
>   还没有任何模块调用它们。
> - **最不可靠的一环是差异化分发**：出口扫描 `scan_view` 有可复现的假阳性，会在
>   正常中文文案上把合法消息拦下。
>
> 结论是：先接线、再换掉出口扫描的判定方式。

**2026-09-12 复核：上述三条卡点均已闭合。**

- **装配已落进生产代码**：`hub/api/stances.py:analyze_round` 是唯一的生产装配入口，
  串起 `to_stance_inputs`（封死 str/int 接缝）→ 宽容收敛判定 → `to_fact_base`
  → `build_audience_view` → `scan_view`，并新增只读接口
  `GET /api/items/{matter_id}/stances/analysis`。
- **出口扫描判定方式已收窄**：从「纯文本匹配裸整数」改为只认 id 形态
  （`hub/domain/audience.py:_ID_SHAPED`），「v2 / 3 个方案」不再被误判（详见 §5.1）。
- **无结论时给反对者发「未采纳」的自相矛盾已修掉**（详见 §5.2）；
  `ConvergenceEvalError` 的兜底已由宽容版 `evaluate_convergence_lenient` 接管
  （详见 §5.3）。

结论：**可以继续投入**。下一步不是加功能，而是把出口扫描「做不到什么」
（`SCAN_LIMITATIONS`，恰 3 条）继续往调用方透传，别让「没发现问题」被读成
「没有问题」——这是 C3 的落点，`RoundStanceAnalysis.limitations` 已随之暴露。

---

## 2. 交付清单

> 本节是 **2026-09-11 当日快照**（过程留痕）。2026-09-12 的收口又新增了
> `tests/integration/test_stance_closure.py`（跨线闭环），并改动了
> `hub/domain/**` 与 `hub/db/models.py`（见文末修订记录）。

| 项 | 内容 |
|---|---|
| 修改文件 | `hub/api/stances.py`、`hub/web/routes_api.py`、`hub/api/audit.py`、`tests/api/test_stances_api.py` |
| 新增文件 | `tests/integration/test_stance_e2e.py`、`outputs/stance-layer-walkthrough.md` |
| 新增测试数 | **12**（`tests/api/test_stances_api.py` 10 个，`tests/integration/test_stance_e2e.py` 2 个） |
| 全量测试 | **526 passed**（基线 514 → 526），耗时 61.7s（2026-09-11 当日快照） |
| ruff | `All checks passed!` |
| 未改动 | `hub/domain/**`（纯函数一行未改）、`hub/schemas/stance.py`、`hub/main.py`、`hub/db/models.py`（仅当日而言；09-12 收口已改动 `hub/domain/**` 与 `hub/db/models.py`） |

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
2. `test_标题里的版本号不再被判定为泄漏`（当日名为
   `test_出口扫描把标题里的版本号误判成未授权用户id_已知缺陷`；2026-09-12 假阳性
   修复后断言翻转并改名转正）

---

## 3. 逐环判定

| 环节 | 判定 | 证据 | 卡点 |
|---|---|---|---|
| ① 立场模型 | ✅ 通 | `Stance` 落库、`StanceRead` 出参回填、唯一约束 `(matter, round, user)` 生效 | 无 |
| ② 传输（HTTP） | ✅ 通 | 真 `POST/GET /api/items/{id}/stances[/{user_id}]`，201/200/404/422 均为统一错误形状 | 无 |
| ③ 权限 | ✅ 通 | 成员校验 + 401/404 语义；**变异检验证明非空洞** | 语义裁定已按 PRD 第 3 章角色表裁定（A2，见 §6） |
| ④ 收敛判定 | ✅ 通且已接线 | `analyze_round` 串起装配；support+oppose(risk_appetite) → `converged=False`、分歧类型 `risk_appetite`；脏 stance 走宽容版降级 | 无；`ConvergenceEvalError` 已由宽容版接管（见 §5.3） |
| ⑤ 差异化分发 | ✅ 通 | 两份视图确实不同、`fact_version` 一致；无结论时反对者视图改发「现在还没有结论」 | 假阳性与无结论自相矛盾均已修复（见 §5.1 / §5.2） |

### 3.1 关键接缝：`user_id` 类型不一致（已证实存在，已处理）

任务预告的接缝是真的：`convergence_eval.StanceInput.user_id` 是 `str`，
`audience.FactBase.*_user_ids` 是 `int`，而 `Stance.user_id` 是 `int`。
Python 不会自动转换，e2e 里显式做了 `str(row.user_id)` 与 `int` 映射：

```python
StanceInput(user_id=str(row.user_id), stance=row.stance, ...)
```

**但要注意：当日这个转换只写在测试里**，没有任何生产模块做这件事。
如果下一步有人直接用 `Stance.user_id` 喂 `StanceInput`，`evaluate_convergence`
不会报错、只会静默产生完全不同的 `factions`（字符串和整数永不相等，
faction 分组会把同一个人拆成两组）。**这是一个安静失败的坑，比报错更危险。**

**2026-09-12 收口**：转换已封进 `hub/api/stances.py:to_stance_inputs`
（`str(user_id)` 只允许发生在这里），`to_fact_base` 再把它转回 `int`。接缝自此在
生产代码里闭合，调用方不再有自己转的机会；`tests/api/test_stances_api.py` 里
`test_装配接缝不做转换会切出不同的阵营` 固化了「绕开会静默出错」这条对照证据。

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

**2026-09-12 说明**：上表第二、三行验证的是当日 `_visible` 对 `visibility` 的
二次过滤。按 PRD 第 3 章角色表，A2 已取消这层细分（成员内不再按 `visibility`
区分，见 §6 第 3 条），`_visible` 现在恒为 True。对应的测试保留原名
`test_单人读取同样按可见性过滤`，但断言已改写为「成员可读、纯陌生人 404」。
第三行那次「旁路」是真实发现，结论仍然成立——两个端点现仍共用同一个 `_visible`
谓词，只是该谓词不再含 `visibility` 细分。

---

## 5. 缺陷与风险（2026-09-11 记录，2026-09-12 复核状态）

> 本节四条是当日走通时暴露的问题，保留原始分析作为决策上下文；每条标题后的
> 【】标注 09-12 复核后的状态。

### 5.1【已修复，2026-09-12】出口扫描的假阳性 —— `hub/domain/audience.py:scan_view`

（原文保留）`scan_view` 当日用 `_INT_TOKEN = re.compile(r"(?<!\d)\d+(?!\d)")` 在
**成品自然语言文本**里找整数，只要某个整数等于事实基座里的参与人 id 且不在
`visible_user_ids` 里，就判为「泄漏用户 id」。

问题在于：参与人 id 是自增小整数（1、2、3…），而中文文案里裸整数遍地都是。
**当日最小复现**（当时固化为 `test_出口扫描把标题里的版本号误判成未授权用户id_已知缺陷`）：

```
事项标题 = "是否上线推荐系统 v2"
用户 1（支持方）的视图 -> scan_view(...).blocked == True
violations == ["出现了未授权的用户 id: 2"]     # 2 来自 "v2"，不是用户 id
```

后果：**合法的消息会被拦下**，而且是随机拦——文案里出现「v2」「3 个方案」
「提升 2 成」这类内容就会触发。这不是接线引入的问题，是 `scan_view`
自身的设计缺陷，端到端串联把它暴露了出来。

当日也验证过：简单改成 `(?<!\w)\d+(?!\w)` 能修掉「v2」，但会漏掉「用户2」
（中文无空格）这类真实泄漏，是按下葫芦浮起瓢；根因确认为「用文本扫描判断结构化事实」。

**修复（2026-09-12）**：判定方式收窄为「只认 id 形态」——新增
`_ID_SHAPED = re.compile(r"(?:用户|用戶|使用者|user|uid|id)\s*[:#＝=号]?\s*(\d+)", re.IGNORECASE)`，
裸整数一概不判；权限交由结构化数据（`visible_user_ids`）负责，正则不再承担这个职责，
已死的 `_INT_TOKEN` 删除。原「已知缺陷」用例转正为
`test_标题里的版本号不再被判定为泄漏`（保留「文本确实含 v2」的前提断言，再断言
`blocked is False`），e2e 主用例的标题恢复为「是否上线推荐系统 v2」。同时 `scan_view`
随结果返回 `SCAN_LIMITATIONS`（恰 3 条），把「只认 id 形态、不判裸整数、不做语义判断」
写成对外契约。真实的 id 形态（「用户 2」）仍会被抓住，见 `tests/domain/test_audience.py`。

### 5.2【已修复，2026-09-12】无结论时的分发消息自相矛盾 —— `build_audience_view`

（原文保留）`FactBase.decision` 允许 `None`，但当日 `build_audience_view` 对反对者的
分支不检查它。实测输出（`decision=None`，即**收敛未达成、尚无结论**）：

```
结论: 这事目前还没定下来。
为什么这次没有采纳: 这次结论的依据是：收益明确，但风险尚未有兜底方案
```

同一份消息里既说「还没定下来」又解释「为什么没被采纳」。语义上「差异化分发」
的前提是**已经形成结论**；在收敛未达成时给反对者发「未采纳」通知是错的。

**修复（2026-09-12）**：`decision is None` 时不再发「为什么这次没有采纳」与
「什么情况下会重新考虑」，改发新节「现在还没有结论」（"这事目前还没定下来，
你的意见还在桌面上。"）；同一条件下也不再输出「为什么这么定」——无结论就不能
紧接着解释结论依据。

### 5.3【已缓解，2026-09-12】`ConvergenceEvalError` 在生产侧无兜底

（原文保留）`evaluate_convergence` 遇到未知 `stance` 值直接抛 `ConvergenceEvalError`。
而当日 `stances.stance` 在库里是自由 `String(32)`，没有 CHECK 约束、没有枚举兜底。
一旦库中出现非法值（历史数据、其他写入方、后续新增立场类型忘了同步
`_STANCE_WEIGHTS`），调用方会拿到未处理异常；当日 `hub/api/` 还没有任何代码调用它。

**处置（2026-09-12）**：新增宽容版 `evaluate_convergence_lenient`——跳过未知 stance、
置 `degraded=True`，并让 `converged` 追加 `and not degraded`（脏数据不许冒充收敛），
如实登记 `skipped_stance_user_ids`。生产装配入口 `analyze_round` 用宽容版，并对每个
被跳过的脏行写一条 `convergence_degraded` 审计（`detail={"stance", "user_id"}`）。
同时给 `stances` 表补了 4 条 CHECK 约束（stance / acting_as / urgency / visibility）。
**但 CHECK 只对新建库生效**：本仓库无迁移工具，存量库的 stances 表没有该约束，
历史脏行 / 外部直写 / 新增立场类型忘了同步都会产生未知 stance，所以宽容分支不是死代码。
严格版 `evaluate_convergence` 保留，仍对未知 stance 抛错，供「宁可炸也不要静默」的调用方用。

### 5.4【仍存在，已降级】`convergence_eval` 的分布是封闭硬编码集合

`_STANCE_WEIGHTS` 是硬编码闭集。新增立场类型必须同时改三处（schema 枚举 +
权重表 + `stances` 的 CHECK 约束），否则产生未知 stance。09-12 之后该情形由 5.3 的
宽容分支接住（降级 + 审计），不再直接炸穿调用方；但「新增类型要同步多处」这个
结构性风险仍在，属于仍需留意项。

---

## 6. 我做的语义裁定（2026-09-11 记录）

任务没有明确定义读权限边界，我按「复用既有唯一事实源」的原则裁定如下：

1. **【维持】读 = 事项成员**（发起人 **或** 参与人），复用
   `matters.get_matter_for_user`。非成员一律 **404**（不用 403，避免泄露
   「该事项存在」）。
2. **【维持】写 = 必须是参与人**。发起人若未勾选参与（`initiator_participates=False`），
   也不能提交立场。写侧 404 文案与「事项不存在」保持**同一句**，不区分。
3. **【2026-09-12 已被推翻，按 PRD 第 3 章角色表修正】可见性在成员之上再做一层
   过滤**：`visibility="participants"` 的立场仅真正的参与人可见；
   `visibility="all"` 的立场事项成员都可见。这让 `visibility` 字段有了可观测语义
   （否则该字段是死的），代价是**发起人但非参与人默认看不到任何立场**。
4. **【维持】单人读返回该 `(matter, user)` 下 `round_number` 最大的那条。**

第 3 条是本次唯一的自由发挥，也是最可能需要推翻的假设：
如果产品期望「发起人应该能看到全部立场」，那 `visibility` 的语义要重新定义。

**推翻记录（2026-09-12）**：逐字核对 PRD 第 3 章角色表后确认，发起人本就应看到本事项
全部摘要与原始回答；上面那句「发起人但非参与人默认看不到任何立场」正是被推翻的假设。
按 A2 修正为：**成员（发起人 ∪ 参与人）内不再按 `visibility` 细分**，`visibility`
只保留留痕/审计语义，读侧不再产生权限差异——`hub/api/stances.py:_visible` 现在恒为
True，非成员仍由 `matters.get_matter_for_user` 一律 404。原「自由发挥」的取舍与代价
记录保留在上方，作为决策上下文。

---

## 7. 下一步该先打哪（2026-09-11 提出，2026-09-12 复核）

**优先顺序（按投入产出）：**

1. **把 3→4→5 的装配写进生产代码**（最重要）。
   现在「Stance 行 → `StanceInput`」「Stance 行 → `FactBase`」这两段转换只活在测试里，
   是最容易在真实使用中静默出错的地方（见 §3.1 的 str/int 陷阱）。
   建议落成 `hub/api/stances.py` 里的 `to_stance_inputs(session, matter_id)` /
   `to_fact_base(...)`，并把 `str(user_id)` 转换**封在这里**，不让调用方自己转。
   —— **✅ 已完成（2026-09-12）**：`to_stance_inputs` / `to_fact_base` /
   `analyze_round` 落在 `hub/api/stances.py`，并新增只读接口
   `GET /api/items/{matter_id}/stances/analysis`。
2. **修出口扫描的判定方式**（§5.1）。这是唯一会「拦下正确消息」的缺陷，
   属于安全性功能里的可用性事故。要么改成结构化比对，要么让 `AudienceView`
   携带 id 白名单而不是靠正则猜。
   —— **✅ 已完成（2026-09-12）**：收窄为只认 id 形态，裸整数不判（§5.1）。
3. **定义「无结论时不发未采纳消息」**（§5.2）。
   —— **✅ 已完成（2026-09-12）**：改发「现在还没有结论」（§5.2）。
4. **给 `ConvergenceEvalError` 定策略**（§5.3）。
   —— **✅ 已完成（2026-09-12）**：宽容版跳过 + 降级 + 审计（§5.3）。
5. 确认 §6 的三条权限裁定，特别是可见性语义。
   —— **✅ 已确认（2026-09-12）**：逐字核对 PRD 第 3 章角色表，第 3 条被推翻并
   修正为 A2（§6 推翻记录）。

---

## 8. 诚实边界

**2026-09-11 当日没有做的事（原文保留）：**

- 端到端测试走的是**进程内 ASGI（`TestClient`）**，不是 `tests/integration/` 里
  那种真 uvicorn 子进程。理由：本文档验证的是「五个环节能否串起来」，
  进程内 ASGI 已经覆盖路由/序列化/鉴权/落库全链路，且确定性更好。
  `tests/integration/` 里另有需要后台 worker 的用例仍走真 uvicorn。
- 环节 4、5 **在生产侧没有调用方**。本次只证明了它们能接上，没有证明它们被接上。
  —— **✅ 已接上（2026-09-12）**：见 `analyze_round`（§3.1 / §7 第 1 条）。
- §5.1 的缺陷**没有修**，只固化了复现。修它属于设计改动，超出「接线走通」范围，
  硬修会有按下葫芦浮起瓢的风险。
  —— **✅ 已修（2026-09-12）**：判定收窄为只认 id 形态（§5.1）。
- 未做并发/幂等验证：同一 `(matter, round, user)` 并发提交的竞态行为本次未测。

**2026-09-12 收口仍未做的事（诚实边界延续）：**

- **没有一条用例同时走「真 HTTP + 四层闭环」**：新增的
  `tests/integration/test_stance_closure.py` 直接调用服务层 `analyze_round`，
  不经 ASGI 路由；路由层只由 `tests/api` 覆盖。两层各自成立，但未合为一条。
- 并发/幂等仍未验证：同一 `(matter, round, user)` 的并发提交竞态依旧未测。
- `SCAN_LIMITATIONS` 里的「不覆盖调用方在 sections 之外附加的内容」与「不做语义
  判断」两条，目前只是如实声明，没有对应防护。

---

## 附：2026-09-12 收口修订记录

跨线集成收口时补记，列出对本文档中被改动推翻的陈述所做的修正。原始判定与分析
一律保留在正文，未删除——过程留痕是刚需。

- **§1**：09-11 的三条卡点（装配只在测试里 / `scan_view` 假阳性 / 无结论自相矛盾）
  均已闭合，改为「复核」叙述；原判定以引文保留。
- **§2**：标注为 09-11 当日快照；「新增测试清单」第 2 条补记改名与转正；补记新增
  `tests/integration/test_stance_closure.py`。
- **§3**：④ 由「通但未接线」→「通且已接线」；⑤ 由「接得上但不可靠」→「通」；
  ③ 卡点改为已按 PRD 裁定。§3.1 补记 `to_stance_inputs` 已封死 str/int 接缝。
- **§4**：补记 A2 取消了 `_visible` 对 `visibility` 的二次过滤；原变异检验记录保留。
- **§5**：5.1 / 5.2 标为已修复，5.3 标为已缓解，5.4 标为已降级；原始复现与根因
  分析保留。
- **§6**：第 3 条（可见性二次过滤）标为已被推翻，按 PRD 第 3 章角色表修正为 A2；
  「自由发挥」的取舍与代价作为决策上下文原文保留。
- **§7**：五条待办逐条标注完成情况。
- **§8**：补记 09-12 仍未做的事（无「真 HTTP + 四层闭环」单条用例、并发未验证等）。
