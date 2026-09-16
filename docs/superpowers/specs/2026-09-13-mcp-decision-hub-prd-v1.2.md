# MCP 决策中台产品需求文档（PRD）v1.2 修订稿

- 日期：2026-09-13
- 版本：v1.2（定稿）
- 状态：**定稿 · 已入库**（owner 2026-09-14 按草案定稿）
  - **「定稿」的范围**：仅指**条款正文** —— §1 逐章新条款全文、§2 变更日志、§3 未采纳项。
    **§4.2「待确认」项不计入定稿范围**，其存在不使本稿变成草稿。
  - 2026-09-16 owner 已关闭 §4.2 中的 4 条（见 §4.1）；剩余开口见 §4.2。
- 基准文档：v1.1 `docs/superpowers/specs/2026-08-11-mcp-decision-hub-prd.md`
- 关联设计：`docs/superpowers/specs/2026-08-11-mcp-decision-hub-design.md`
- 面向读者：产品、研发、测试、运维
- 本稿性质：**条款全文稿**，供评审后逐节替换 v1.1 对应章节；不含仓库代码改动

---

## 0. 修订说明

### 0.1 修订范围

v1.2 只做一件事：把 v1.1「平台不承认 Agent 有承诺能力」这一硬约束，改为**一条受控的口子**，并补上锁。具体为两组相互咬合的机制：

1. **`agent_authority`**：挂在「事项 × 参与人」关系上的代理承诺开关，两档 `propose_only`（默认）/ `can_commit`。
2. **`irreversible`**：事项级「不可逆」标记，为 true 时该事项**不接受任何代理提交**。

两组机制在**两条互不相干的提交路径**上必须语义一致：

| 路径 | 入口 | v1.1 现用的字段 |
|---|---|---|
| ① MCP 工具 | `submit_output`（`hub/mcp_server/methods.py`） | `human_approved` / `approved_at` / `content_digest` |
| ② HTTP 接口 | `POST /api/items/{matter_id}/stances`（`hub/web/routes_api.py` → `hub/api/stances.py`） | `acting_as` / `authority` / `visibility` |

本次修订**不扩大**产品范围：不引入委托审批人、不引入多级授权、不引入租户 RBAC（与 2.2「明确不做」一致）。

### 0.2 修订依据

- owner 2026-09-11 决策（本稿决策编号 Q1–Q12、O1、O2，逐条对照见 0.4）。
- 待办 `rRSEcS` 的描述文本（`irreversible` 与 `agent_authority` 的语义来源）。

### 0.3 未比对声明（必须随稿保留）

> **本稿依据 owner 2026-09-11 决策与 `rRSEcS` 待办描述；未与仓库外「方案」文档逐字比对。**

补充说明（如实登记，不得删除）：

- 待办 `rRSEcS` 的描述引用了仓库外「方案第 2.4 节」。该「方案」文档在本次修订可用的全部渠道均**未找到**：仓库内 0 命中、资料库 4 份候选全部 0 命中、历史会话 0 条。
- 因此本稿中标为「方案第 2.4 节」语义的条款，**来源是待办描述本身**，不是那份方案原文。
- 本稿**未做**、也**不能声称做过**与该方案的逐字一致性核对。若后续拿到该方案，必须补做一次逐字比对并出比对结论（列入 §4.2 待办 2）。

### 0.4 决策编号对照

| 编号 | 决策 | 落到本稿位置 |
|---|---|---|
| Q1 | `agent_authority` 挂在「事项 × 参与人」关系上，落库 `matter_participants` 加列 | §1.2 2.4.1、§1.11 |
| Q2 | 两档：`propose_only`（默认）/ `can_commit`，无第三档 | §1.2 2.4.1 |
| Q3 | 两条提交路径都要生效，规则必须一致 | §1.2 2.4.3、§1.6 9.2、§1.7 9.3 |
| Q4 | `can_commit` 免 `human_approved` + 免 `approved_at`，**保留** `content_digest` | §1.2 2.4.3、§1.7 9.3 |
| Q5 | 接受改表：`outputs.approved_at` 改为可空，以「`approved_at` 为空」为留痕依据 | §1.11、§1.7 9.3 |
| Q6 | MCP 路径也要能区分代理提交：`acting_as` 进入 Output 侧，实体表与方法表同步增列 | §1.5 FR-12、§1.6 9.2、§1.11 |
| Q7 | 开关只能本人开、必须本人事先明示同意、随时可撤销 | §1.2 2.4.2、§1.3 角色表 |
| Q8 | ① 4.3 文案增列代理提交提示；② 开通 `can_commit` 时一次性告知 | §1.4 4.3 |
| Q9 | 不可逆判定 = 发起人建事项时勾选，落库 `matters.irreversible` | §1.2 2.4.4、§1.11 |
| Q10 | 默认不勾、建完可改、只能发起人改、必须记审计；已发生代理提交后**禁止**改为不可逆 | §1.2 2.4.4 |
| Q11 | `irreversible = true` 时「强制拉人」= 拉**本人**出 `human_approved` | §1.2 2.4.5 |
| Q12 | 被拒错误码复用 `422 HUMAN_APPROVAL_REQUIRED`，不新增错误码 | §1.2 2.4.5、§1.8 9.5 |
| O1 | `visibility` 读侧维持 A2 结论；「按 visibility 过滤」表述改掉；「列表响应不含私有字段」保留并强化 | §1.9 9.6、§1.10 15.1 |
| O2 | 对外契约边界全部用 Pydantic（4 个 JSON API 端点 + 4 个 MCP 工具返回值），不含 LangGraph state 与 HTML 模板上下文 | §1.9 9.6 |

### 0.5 本轮回合补充拍板（owner 2026-09-13，共 10 条，全部按 team-lead 建议执行）

| 编号 | 拍板 | 落到本稿位置 |
|---|---|---|
| R1 | `Stance` 新增 `approved_at`（可空），与 `outputs.approved_at` 语义对齐，消除两路径留痕不对称 | §1.2 2.4.6、§1.6 9.2、§1.7 9.3、§1.11 实体表、§1.12 15.1 |
| R2 | 两条路径**统一**为「客户端声明 `acting_as` + 服务端闸门校验三项」，不做派生 | §1.2 2.4.3、§1.6 9.2、§1.7 9.3 |
| R3 | `Stance.authority` 由 `VARCHAR(255)` 自由文本**收敛为枚举** `propose_only` / `can_commit` | §1.2 2.4.3、2.4.6、§1.12 15.1 |
| R4 | 4.3 两处文案给出**可直接粘贴的草案**，均标注【待 owner 定稿】 | §1.4 4.3 |
| R5 | 新需求编号保留 `FR-12b` / `FR-12c`（FR-27 / FR-28 方案不再采纳） | §1.5 |
| R6 | 存量 `stances.authority` **禁止启发式回填**：新枚举列一律置 `NULL`（语义=历史值不可识别），原值整体搬迁到只读列 `authority_legacy`，写一条审计；读侧不得推断档位 | §1.2 2.4.6、§1.7 9.3、§1.11 实体表、§1.12 术语表与 15.1 |
| R7 | **路径① 也加 `authority`**：`outputs` 新增同名同义可空枚举列，两条路径字段完全对称 | §1.2 2.4.3、2.4.6、§1.6 9.2、§1.7 9.3、§1.11 实体表、§1.12 术语表 |
| R8 | 历史空值只在**内部审计/运维视图**标注，措辞固定为「该记录早于本版本，无确认时间留痕」，**禁止**任何暗示代理的措辞 | §1.7 9.3（落点理由见该节） |
| R9 | 迁移审计按**三层**落地：`schema_migrations` 为主留痕；数据语义变更才进 `audit_events`（新增常量 `SCHEMA_MIGRATED`，**不受**错误码禁令约束）；写入前先探测表存在（迁移先于 `create_all`），不存在只写 logging 不报错 | §1.7 9.3「系统级事件」、§1.12 术语表、§1.10 场景 34 |
| R10 | `authority_legacy` **不设自动过期**（唯一授权约定载体，成本可忽略）+ 提供**只读导出** + 标注「只读兼容列，未来大版本可评估下线」 | §1.11（§2.3 追加）、§1.12 术语表 |

**迁移合并判断**：R1、R3、R6、R7 涉及的表结构变更**与 R1 之前的四项变更合并为同一条 v3 迁移步骤**，理由见 2.4.6「合并理由」（核心：`matters`、`matter_participants`、`outputs`、`stances` 四张表都要重建，拆分只会让同一次部署重复多次高风险的「重建 → 换名 → 重建索引」；且 `stances` 的加列与收紧列类型物理上是同一次重建）。R9 只改变迁移的**留痕方式**，不改变 v3 的步骤顺序（表结构变更在前、审计写入在后、版本记录由 runner 收尾）。

---

## 1. 逐章新条款全文

> 约定：每节先给「v1.1 替换范围」并**逐字**引用原文，再给「v1.2 新条款全文」。评审通过后按节替换。

### 1.1 §2.1 V1 范围（替换两条并列项，并新增一条）

**v1.1 原文（逐字，第 45 行）**

> - `submit_output` 强制携带 `human_approved: true`、`approved_at` 与 `content_digest`，服务端校验摘要一致性但不宣称能证明真实人工操作。

**v1.2 替换为（逐字，可直接替换该行）**

```markdown
- `submit_output` 默认强制携带 `human_approved: true`、`approved_at` 与 `content_digest`，服务端校验摘要一致性但不宣称能证明真实人工操作。
- 允许一条受控例外：当本次提交所属的「事项 × 参与人」关系已开通 `agent_authority = can_commit`，**且**该事项 `irreversible = false` 时，`human_approved` 与 `approved_at` 可一并免填，`content_digest` 一律必填并强校验（见 2.4、9.3）。
- 参与人可在事项内为自己开通或撤销 `agent_authority`（默认 `propose_only`），该开关只能由本人操作且随时可撤销；发起人创建事项时可勾选「不可逆事项」，该标记默认不勾选、仅发起人可变更（见 2.4）。
```

**连带一致性影响（需同步，本稿未展开全文）**

- §1.1 一句话定位中「提交经本人确认的纯文本输出」需加括注：默认经本人确认；已开通 `can_commit` 且事项可逆时可由 Agent 代本人承诺（见 2.4）。**是否改写定位句由 owner 定（见 §4.2 待办 6）。**
- §1.3 核心价值「有落点」与「人审后的输出正文会发送至平台配置的第三方 LLM 服务商」两处措辞不受影响。
- §12.1 P0 清单中的「人审声明（含 `approved_at` 与 `content_digest` 校验）」需补「与 `can_commit` 代理提交例外（2.4）」。

### 1.2 新增小节 §2.4「`agent_authority` 与两条提交路径」

**位置**：插在 v1.1 §2.3「数据留存边界」之后，成为 §2.4。**新增，无替换对象。**

```markdown
### 2.4 代理承诺开关与两条提交路径

#### 2.4.1 开关定义（Q1、Q2）

`agent_authority` 是挂在**「事项 × 参与人」关系**上的开关，不是账号级、不是 Token 级属性：

- 落库位置：`matter_participants` 表新增列 `agent_authority`，默认 `propose_only`。
- 存在前提：该行关系存在，即该用户是本事项的参与人。非参与人没有该属性。
- 取值**只有两档**，无第三档：
  - `propose_only`（默认）：Agent 只能提议；提交**必须**携带本人 `human_approved: true`。
  - `can_commit`：Agent 可代本人承诺；在**可逆事项**上允许免 `human_approved` 与免 `approved_at` 提交（见 2.4.3）。
- 同一个账号在不同事项上可以处于不同挡位；同一事项上不同参与人各自独立。
- 发起人若勾选「我也参与作答」（3.0），则该账号在本事项上同时是发起人与参与人，其 `agent_authority` 只作用于**自己那份参与人身份**，不改变拍板权。

#### 2.4.2 开通、生效与撤销（Q7）

- **只能本人开通**：`agent_authority` 的开通与撤销，唯一合法操作者是**被代理的那位参与人本人**（通过本人 Web 会话）。发起人不能替参与人开通；平台管理员不能替参与人开通；Agent（含持有该用户 Token 的 Agent）**不能**自行开通。
- **必须本人事先明示同意**：开通动作本身即为同意，且必须是开通在前、代理提交在后；不存在「先由 Agent 代承诺、事后追认」的路径。
- **打开时的挡位**：开通即置为 `can_commit`；关闭即回到 `propose_only`。不提供「仅开通但保持只提议」的中间态（只提议就是默认档，无需开通）。
- **随时可撤销**：本人可在任意时刻撤销。撤销**立即生效**，无需等待任何异步流程；撤销后同一 Agent 再声明 `acting_as = agent_on_behalf` 立即被拒（`422 HUMAN_APPROVAL_REQUIRED`，见 2.4.3）。
- **一次性告知**：本人开通 `can_commit` 时，Web 端必须做一次性告知，文案至少覆盖三点——① 开启后本人在本事项中的提交可由 Agent 代理、**不再逐次要求本人确认**；② 本人可随时撤销；③ 不可逆事项上代理提交会被拒绝。**可直接粘贴的最终文案见 4.3（owner 2026-09-14 定稿，采用交接稿 §8.2-S4 的现有草案措辞）。**
- **审计**：开通与撤销均写入审计（`AuditEvent`），记录操作者、事项、被代理的参与人、变更前后挡位与时间。
- **开通/撤销是否限于事项未进入终态**：本稿未设定额外限制；事项已 `completed` / `cancelled` 时按 7.1 终态只读处理。**此项列为待确认（见 §4.2 待办 5）。**

#### 2.4.3 两条提交路径的统一闸门（Q3、Q4、Q6）

**两条路径必须语义一致，下面这张表是唯一口径。**路径 ① 为 MCP 工具 `submit_output`，路径 ② 为 HTTP 接口 `POST /api/items/{matter_id}/stances`。

**两条路径采用同一分工：`acting_as` 一律由客户端声明，服务端只做闸门校验**，不做「一条派生、一条声明」的差异化处理。理由：两条路径对外都**不宣称能证明真实人工操作**（9.3 末条），若一条号称派生、另一条号称声明，会制造「路径① 更强」的假象，而实际上派生也只是把同一个不可证明的声明从请求体挪到了服务端。

设：
- `acting_as` = 本次请求声明的提交形态 ∈ {`human`, `agent_on_behalf`}
- `A` = 提交人（被代理的参与人）在本事项上的 `agent_authority` ∈ {`propose_only`, `can_commit`}
- `I` = 本事项的 `irreversible` ∈ {`false`, `true`}

| 声明 `acting_as` | `A` | `I` | 结果 | 落库 `approved_at` |
|---|---|---|---|---|
| `human` | 任意 | 任意 | 允许（须携带本人 `human_approved: true` 与 `approved_at`） | 必填 |
| `agent_on_behalf` | `propose_only` | 任意 | 拒绝 `422 HUMAN_APPROVAL_REQUIRED` | — |
| `agent_on_behalf` | `can_commit` | `true` | **拒绝 `422 HUMAN_APPROVAL_REQUIRED`**（见 2.4.5） | — |
| `agent_on_behalf` | `can_commit` | `false` | **允许**（免 `human_approved`） | **留空** |

**服务端到底校验什么（这是闸门的实质）**——声明 `agent_on_behalf` 时，服务端逐项校验：

1. **开关挡位**：`matter_participants.agent_authority` 当前值必须为 `can_commit`。读当前值即可判定，因此撤销后立即失效，无需额外的作废流程。
2. **事项可逆性**：`matters.irreversible` 必须为 `false`；为 `true` 则拒绝，返回 `422 HUMAN_APPROVAL_REQUIRED`（不新增错误码）。
3. **授权有效性**：该开关必须由**被代理的本人开给自己**，且未被撤销。判定依据是本事项上该参与人最近一次 `agent_authority` 变更审计的 `actor_user_id` 等于被代理用户本人；审计链路缺失或 actor 非本人时视为授权无效，拒绝。

**服务端的边界（必须如实表述）**：服务端**只校验声明与授权状态**，**不证明**提交确实出自本人真实意愿，也不证明本人读过内容。不得在文档、错误信息或界面上写成「服务端能证明是本人」。声明 `human` 的提交与 `human_approved: true` 是同一回事、同一效力边界（见 9.3 末条）。

其余规则：

- **`content_digest` 一律必填并强校验**（Q4）：摘要的作用是防止「本地确认过的内容」与「实际提交的内容」不一致，与「谁承诺」无关。`agent_on_behalf` 路径下，摘要不匹配仍返回 `422 HUMAN_APPROVAL_REQUIRED`。
- **`approved_at` 留空即留痕**（Q5）：两条路径共用同一语义。通过闸门的 `agent_on_behalf` 提交 `approved_at` 留空，服务端以 `approved_at IS NULL` 作为「本次提交未经本人逐次确认」的事实依据。`outputs.approved_at` 与 `stances.approved_at` **两列都因此为可空**（见 2.4.6）。
- **`acting_as` 与 `authority` 的分工**（Q3、Q6）：`acting_as` 回答「**谁**提交的」（`human` / `agent_on_behalf`），`authority` 回答「**凭什么**能提交」（`propose_only` / `can_commit`）。`authority` 记录**提交时刻的授权状态**：请求体携带该值时，服务端校验其与当前 `agent_authority` 一致，不一致返回 `422 HUMAN_APPROVAL_REQUIRED`；因此声明 `agent_on_behalf` 时 `authority` 必为 `can_commit`。
- **两条路径的字段对应（差异只在字段名，不在规则）**：

  | 概念 | 路径① MCP `submit_output` | 路径② HTTP `POST /api/items/{matter_id}/stances` |
  |---|---|---|
  | 提交形态（谁） | `acting_as`（请求体声明） | `acting_as`（请求体声明） |
  | 授权状态（凭什么） | `authority`（请求体声明，服务端校验一致性） | `authority`（请求体声明，服务端校验一致性） |
  | 本人确认 | `human_approved: true` | 由 `acting_as = human` 表达 |
  | 内容摘要校验 | `content_digest`（9.3 口径） | `content_hash`（服务端按同一口径重算比对） |
  | 确认时间留痕 | `approved_at`（`agent_on_behalf` 时留空） | `approved_at`（`agent_on_behalf` 时留空） |

  **两条路径字段完全对称**（R7）：三组字段同名同义同校验，服务端侧不存在「只有一条路径才有的判定输入」。此前「路径① 无 `authority`」的缺口已关闭。

- **`propose_only` 下的提交不产生代理标记**：即使由 Agent 执行，只要本人已确认（`acting_as = human`），落库即 `human`。

#### 2.4.4 「不可逆事项」的判定与变更（Q9、Q10）

- **判定方案（Q9）**：**I2 口径**——事项发起人在创建事项时勾选「不可逆事项」。落库 `matters` 表新增列 `irreversible`。
- **默认不勾（Q10）**：`irreversible` 默认 `false`，即默认可逆。
- **建完可改**：事项创建后允许变更 `irreversible`。
- **只能发起人改**：变更操作者唯一为**本事项发起人**；管理员、参与人、Agent 均无权变更（管理员例外见 7.1 状态只读约束）。
- **必须记审计**：每次变更写入审计，记录操作者、事项、变更前后的值、时间与变更理由（理由是否必填**列为待确认，见 §4.2 待办 4**）。
- **变更方向不受限，但有一条硬禁止（防赖账）**：**一旦该事项已发生过代理提交（即已存在 `acting_as = agent_on_behalf` 的 Output），就不允许再把它改成 `irreversible = true`。** 目的是防止「先让代理承诺、事后把事项改成不可逆来主张承诺无效」。反向变更（`true` → `false`）不设此限制。
- **不新增事项状态**：不可逆只是标记，不改变 7.1 的 Matter 状态机，不新增状态。

#### 2.4.5 不可逆事项上的代理提交被拒（Q11、Q12）

当 `irreversible = true` 且客户端声明 `acting_as = agent_on_behalf`（即请求免本人逐次确认）时：

- **拒绝**，返回 `422 HUMAN_APPROVAL_REQUIRED`。
- **错误码复用，不新增**（Q12）：沿用 9.5 既有码，`hub/api/errors.py` 文本「Do NOT add others」保持不变。错误信息需说明拒绝原因是「该事项为不可逆事项，提交必须由本人确认」，但**不得**新增错误码，也不得把该原因塞进新的 `error_code` 取值。
- **「强制拉人」的准确含义（Q11）**：拉的是**被代理的那位本人**，即必须由**本人自己出具 `human_approved`**（及配套 `approved_at`）。
  - **不是**拉事项发起人；
  - **不是**退回 `propose_only` 挡位；
  - **不是**仅提示、仍放行。
- **对 Agent 的可预期性**：Agent 必须在提交前就能知道本事项是否不可逆、自己是否 `can_commit`，以便选择「提交」或「转交本人确认」。据此 `get_task` 必须返回这两个事实（见 9.2）。

#### 2.4.6 字段与落库要求（Q1、Q5、Q6、Q9、R1、R3、R6、R7）

本节锁定**产品契约层面**的字段要求；DDL 由设计文档承接。

| 表 | 变更 | 说明 |
|---|---|---|
| `matters` | 新增 `irreversible`，布尔，默认 `false`，非空 | 事项级不可逆标记（Q9） |
| `matter_participants` | 新增 `agent_authority`，枚举，默认 `propose_only`，非空，带 CHECK（取值 `propose_only` / `can_commit`） | 「事项 × 参与人」关系上的开关（Q1、Q2） |
| `outputs` | `approved_at` 由 `NOT NULL` 改为**可空** | `agent_on_behalf` 提交以该列为空留痕（Q5） |
| `outputs` | 新增 `acting_as`，枚举，非空，带 CHECK（取值 `human` / `agent_on_behalf`） | 区分代理提交（Q6） |
| `outputs` | 新增 `authority`，枚举，**可空**，带 CHECK（取值 `propose_only` / `can_commit`） | 与路径② 对称，回答「凭什么能提交」（R7） |
| `stances` | 新增 `approved_at`，**可空** | 补上与 `outputs.approved_at` 对齐的确认时间留痕，消除两路径留痕不对称（R1） |
| `stances` | `authority` 由 `VARCHAR(255)` 自由文本**收敛为枚举**，可空，带 CHECK（取值 `propose_only` / `can_commit`） | 与 `agent_authority` 同源，回答「凭什么能提交」（见 15.1）（R3） |
| `stances` | 新增 `authority_legacy`，自由文本（≤255），**可空、只读**，不参与任何判定 | 承接历史 `authority` 原值，**禁止启发式解析**（R6） |

**两条路径至此字段完全对称**（R7）：都有 `acting_as`（谁提交的）+ `authority`（凭什么能提交）+ `approved_at`（确认时间留痕），三者在两条路径上同名同义同校验。此前「路径① 无 `authority`」的缺口已由本表第 5 行关闭。

迁移要求：

- 本次字段变更**必须走迁移**，不能只靠 `Base.metadata.create_all`（`create_all` 不会改已存在的表）。项目已有迁移机制（`hub/db/migrations/`，现有版本 1 baseline、2 add_stance_check_constraints），本次为**新增版本 3**，名称建议 `add_agent_authority_and_irreversible`。
- **上述八项变更合并为同一条 v3 步骤**，不拆成多条。理由见下「合并理由」。
- SQLite **不支持**为已有列改可空性、**不支持** `ADD CHECK`、也**不支持**直接收紧列类型，因此 `outputs`、`matter_participants`、`stances` 三张表需按既有 `upgrade_add_stance_check_constraints` 同法**重建表**（12 步流程：临时表 → 灌数 → 换名 → 重建索引），并在迁移前后保持外键与唯一约束（`outputs.task_id` 唯一、`stances(matter_id, round_number, user_id)` 唯一、`stances.supersedes` 自引用外键）。
- 存量数据回填规则：
  - `outputs.acting_as` 一律回填 `human`（v1.1 下全部提交都走强制人审，不存在代理提交）。
  - `stances.acting_as` 为 v1.1 既有列，**不回填**（保留原值）。
  - `stances.approved_at` 回填为空（V1 未记录该时间，不得伪造）；`outputs.approved_at` 保持原值不变。
  - `stances.authority`（新枚举列）**历史行一律置 `NULL`**，其语义是「**历史值不可识别**」——**不是** `propose_only`，**不是**「未授权」，**不是**「无代理权限」。不得给历史行填任何猜测出的挡位。
  - `stances.authority` 的原自由文本内容**整体搬到新列 `authority_legacy`**，字节级保留，不解析、不改写、不裁剪。
  - 迁移执行时的**留痕按三层处理**（R9，细则见 9.3「系统级事件的留痕」）：① 主留痕 = `schema_migrations` 表（version / name / applied_at），不再重复塞 `audit_events`；② 数据语义变更才进 `audit_events`，事件类型为新增的 `SCHEMA_MIGRATED`（系统事件，`matter_id = None`、`actor_user_id = None`）；③ 写入前先探测 `audit_events` 表是否存在（迁移跑在 `create_all` 之前，全新库上该表可能还不存在），不存在就**只写 logging、不报错**。
  - 本条审计的内容大意：「历史 `authority` 自由文本未做语义解析，原值保留在 `authority_legacy`」，`detail` 里放 `version` / `name` / 受影响行数 / 是否有 `authority_legacy` 回填等数据语义变更标记。
- **禁止启发式回填（硬红线，R6）**：`authority` 记录的是**授权归属**。用「看起来像代理授权的就置 `can_commit`」这类启发式规则去猜历史值的语义，**猜错就等于伪造授权记录**，比留一个空值危险得多。**授权记录宁可留洞，不可猜。**
- **读侧规则（R6）**：凡遇 `authority IS NULL` 且 `authority_legacy IS NOT NULL`，即为**历史数据**，**不得推断其授权档位**，也不得在页面或接口上把它显示成 `propose_only`、`can_commit` 或任何一档。展示口径见 9.3「历史空值的标注」。
- 迁移步骤本身必须幂等（存在性守卫即可重复执行）与自带事务，与现有约定一致。
- 升级前自动备份由现有 `run_migrations(backup=True)` 承担，无需新机制。

**合并理由（R1 的 `stances.approved_at`、R3 的 `authority` 收敛、R6 的 `authority_legacy`、R7 的 `outputs.authority` 是否合并为同一条 v3）**：

- **合并**。四条强制理由：
  1. `matter_participants`、`outputs`、`stances` 三张表**都要重建**，而重建是同一种昂贵且高风险的 DDL 操作。拆成多条会让同一次部署里出现多轮「重建 → 换名 → 重建索引」，失败面与耗时成倍放大。
  2. `stances` 这**一张表**同时要加列（`approved_at`、`authority_legacy`）与收紧列类型（`authority`），SQLite 下三者走的是**同一次重建**，物理上无法拆开——拆开就必须重建两次以上。
  3. `outputs` 新增 `authority` 与它已有的 `authority` 之外的两处变更（`approved_at` 可空、`acting_as` 新增）同属**同一次重建**，同样无法拆开。
  4. 四者属于**同一批语义**（`agent_authority` 开关的落库、`acting_as` 的区分、`authority` 的对称与收敛、`approved_at` 的留痕对齐、历史值的保守承接）。同一条步骤便于一次性回滚与一次性备份，也便于迁移报告中记录一句完整的版本语义。
- 若评审坚持拆开，则建议拆成「v3 = 新增列（`matters`/`matter_participants`/`outputs.acting_as`）」与「v4 = 重建 `stances` 并回填」，但**不推荐**：`outputs` 与 `matter_participants` 同样需要重建（改可空性、加 CHECK），拆开后 `stances` 仍然要与 `outputs` 各重建一次，没有省下任何重建。

### 1.3 §3 用户与角色（替换角色表 4 处单元格）

**v1.1 原文（逐字，第 69–75 行角色表相关单元格）**

> | 平台管理员 | 初始化账号、邀请或停用成员、处理账号恢复、维护服务配置 | 账号和系统运维信息；不默认查看事项原始输出 | 不能代替参与人提交人审输出；不能绕过发起人拍板 |
> | 事项发起人 | 创建事项、选择参与人、查看过程、处理阻塞、最终拍板 | 本事项全部摘要和原始回答 | 不能修改参与人的 `human_approved` 声明；不能以他人身份提交 Agent 输出 |
> | 参与人 | 通过个人 Agent 完成本人任务并确认输出 | 本事项摘要、本人任务和本人原始回答 | 不能读取其他参与人的原始回答；不能拍板 |
> | 个人 Agent | 使用用户 Token 拉取、读取和提交任务 | 仅 Token 所属用户的任务和参与事项状态 | 不能访问他人任务、个人模型、原始资料或其他 Agent；不能主动推送 |
> | 平台编排服务 | 出题、汇总、收敛判断、生成决议草案、推进状态 | 处理所需的事项记录和人审输出 | 不能代替发起人完成决议；不能把失败伪造为成功 |

**v1.2 替换为（逐字，可直接整表替换）**

```markdown
| 角色 | 主要职责 | 可见范围 | 禁止操作 |
|---|---|---|---|
| 平台管理员 | 初始化账号、邀请或停用成员、处理账号恢复、维护服务配置 | 账号和系统运维信息；不默认查看事项原始输出 | 不能代替参与人提交人审输出；不能替参与人开通或撤销 `agent_authority`；不能绕过发起人拍板 |
| 事项发起人 | 创建事项、选择参与人、查看过程、处理阻塞、最终拍板、标记或变更本事项是否不可逆 | 本事项全部摘要和原始回答 | 不能修改参与人的 `human_approved` 声明；不能以他人身份提交 Agent 输出；不能替参与人开通或撤销 `agent_authority`；不能代本人出具 `human_approved` |
| 参与人 | 通过个人 Agent 完成本人任务；默认挡位 `propose_only`（提交须本人确认），可选择**为自己**开通 `can_commit` 由 Agent 代本人承诺，并可随时撤销 | 本事项摘要、本人任务和本人原始回答 | 不能读取其他参与人的原始回答；不能拍板；不能替他人开通或撤销 `agent_authority`；不能代他人出具 `human_approved` |
| 个人 Agent | 使用用户 Token 拉取、读取和提交任务；当本人已开通 `can_commit` 且本事项可逆时，可免本人逐次确认代本人提交（`acting_as = agent_on_behalf`）；在 `propose_only` 或不可逆事项下必须先取得本人 `human_approved` | 仅 Token 所属用户的任务和参与事项状态 | 不能访问他人任务、个人模型、原始资料或其他 Agent；不能主动推送；不能自行开通或提升 `agent_authority`；不能伪造本人 `human_approved` |
| 平台编排服务 | 出题、汇总、收敛判断、生成决议草案、推进状态 | 处理所需的事项记录和人审输出 | 不能代替发起人完成决议；不能代替本人出具 `human_approved`；不能把失败伪造为成功 |
```

**补充规范段（新增在 3.0 之后，编号 3.0.1）**

```markdown
#### 3.0.1 「不能以他人身份提交」的细化口径

v1.1 角色表中「不能以他人身份提交 Agent 输出」的准确含义，v1.2 起明确为**三条**，缺一不可：

- **身份不可借**：Token 只能代表 Token 所属用户；账号之间不能互相代提交。
- **承诺能力不可借**：`agent_authority` 属于「事项 × 参与人」关系，不能跨人、跨事项转移；A 的 `can_commit` 不能让 B 的 Agent 承诺。
- **本人同意不可代出**：`human_approved` 只能由被代理的本人出具。Agent 可以代本人**承诺**（`agent_on_behalf`），但不能代本人**确认**（冒充 `human`）。
```

### 1.4 §4.3 第三方 LLM 数据流告知（替换第 3 条并列项）

**v1.1 原文（逐字，第 146 行）**

> - 告知义务：`/login` 首次登录页与 `/matters/new` 创建页必须以可见文案说明"本事项中提交的回答正文将发送至平台配置的第三方大模型服务商用于生成摘要与决议草案"，并标注当前服务商名称。该文案由服务端配置项渲染，更换服务商时同步更新。

**v1.2 替换为（逐字，可直接替换该行并追加两条）**

```markdown
- 告知义务：`/login` 首次登录页与 `/matters/new` 创建页必须以可见文案说明"本事项中提交的回答正文将发送至平台配置的第三方大模型服务商用于生成摘要与决议草案"，并标注当前服务商名称。该文案由服务端配置项渲染，更换服务商时同步更新。
- 告知义务增列（Q8①）：上述文案必须一并说明"**本事项中可能存在未经本人逐次确认的代理提交**"，且代理提交的内容与本人提交的内容一样会送往第三方大模型服务商。该句与主告知同址展示、同样由服务端配置项渲染，不得放进入口折叠区或需展开才能看到的位置。
- 开通告知（Q8②）：参与人在事项内开通 `agent_authority = can_commit` 时，必须做**一次性告知**，文案至少覆盖：① 开启后本人在本事项中的提交可由 Agent 代理、不再逐次要求本人确认；② 本人可随时撤销；③ 不可逆事项上代理提交会被拒绝。开通与撤销动作均写入审计。
```

**可直接粘贴的文案（owner 2026-09-14 定稿）**

以下两段为**文案正文**。措辞已由 owner 2026-09-14 定稿（采用交接稿 §8.2-S4 的现有草案措辞），可直接使用。

> 仍**未定**的只有一件事：是否还需「**我已阅读**」勾选确认 —— 那是交互确认方式，与文案措辞分开处理，见 §4.2 待办 **1b**。

> **4.3 告知义务增列句（追加在现有第三方 LLM 告知之后）｜owner 2026-09-14 定稿**
>
> 「本事项中可能存在未经本人逐次确认的代理提交；此类内容与本人确认后提交的内容一样，会一并发送至上述第三方大模型服务商。」

> **参与人开通 `can_commit` 的一次性告知（弹层正文）｜owner 2026-09-14 定稿**
>
> 「开启后，你在**本事项**中的提交可由你的 Agent 代表你直接提交，**不再逐次要求你本人确认**。
> 不可逆事项除外——在标记为不可逆的事项中，代理提交会被拒绝。
> 你可以随时撤销本授权；撤销后立即生效。」

**说明**：增列句为「可能存在」，对无代理提交的事项同样成立，不设显示条件（不引入按事项状态隐藏的分支，避免出现「该提示可被隐藏」的合规漏洞）。

### 1.5 §6.2 FR-12（替换该行，并新增 FR-12b / FR-12c）

**v1.1 原文（逐字，第 208 行）**

> | FR-12 | Agent 提交经本人确认的输出 | P0 | 缺少或为 `false` 的 `human_approved` 返回 `422`；`approved_at` 缺失或 `content_digest` 与提交正文不一致返回 `422`（见 9.3）；未知或重复题号拒绝 |

**v1.2 替换为（逐字，可直接替换该行并在表尾追加两行）**

```markdown
| FR-12 | Agent 提交输出（默认经本人确认，含受控的代理承诺例外） | P0 | 默认要求 `human_approved: true` 且携带 `approved_at`、`content_digest`；缺少或为 `false` 的 `human_approved` 返回 `422 HUMAN_APPROVAL_REQUIRED`；`approved_at` 缺失或 `content_digest` 与提交正文不一致返回 `422 HUMAN_APPROVAL_REQUIRED`（见 9.3）；未知或重复题号拒绝。**例外**：客户端声明 `acting_as = agent_on_behalf`，且该「事项 × 参与人」关系已开通 `agent_authority = can_commit`、本事项 `irreversible = false`、授权由本人开给自己且未撤销时，可免 `human_approved` 与免 `approved_at`，`content_digest` 仍必填并强校验（见 2.4.3） |
| FR-12b | 参与人自助管理代理承诺开关 | P0 | `agent_authority` 挂在「事项 × 参与人」关系上，两档 `propose_only`（默认）/ `can_commit`，无第三档；**只能由本人**开通与撤销，开通即本人明示同意，撤销立即生效；开通与撤销均写入审计，且审计的 `actor_user_id` 必须是**被代理的本人**（闸门校验依据，见 2.4.3）；开通时做一次性告知（见 2.4.2、4.3） |
| FR-12c | 不可逆事项的标记与变更 | P0 | 发起人创建事项时可勾选 `irreversible`，默认不勾选；仅发起人可变更且必须记审计；**已存在代理提交（`acting_as = agent_on_behalf`）的事项禁止改为 `irreversible = true`**；`irreversible = true` 时任何代理提交返回 `422 HUMAN_APPROVAL_REQUIRED`（见 2.4.4、2.4.5） |
```

**说明**：沿用 v1.1 已有的后缀编号惯例（FR-08b / FR-14b / FR-18b / FR-21b / FR-23b）。**owner 已认可保留该编号（FR-27 / FR-28 方案不再采纳）。**

### 1.6 §9.2 方法清单（替换 3 行 + 新增 2 段）

**v1.1 原文（逐字，第 442–447 行相关行）**

> | `list_pending_tasks` | `limit?`, `cursor?` | 当前用户的 pending 任务摘要、游标、`next_poll_after` | 只返回 Token 所属用户处于 `pending` 的任务 |
> | `get_task` | `task_id` | 主题、背景、本轮问题、上一轮摘要、截止时间、LLM 服务商标识 | 仅任务所属用户可调用；不返回他人原始回答、个人模型和本地资料 |
> | `submit_output` | `task_id`, `answers[]`, `notes?`, `human_approved`, `approved_at`, `content_digest`, `idempotency_key` | 首次提交结果、任务状态、提交时间 | 目标用户匹配；声明缺失/为 false 拒绝；摘要校验见 9.3；幂等见 9.4 |
> | `get_matter_status` | `matter_id`, `rounds_before?` | 事项状态、轮次进度、最近 3 轮摘要、`rounds_total`、决议阶段与版本 | **参与人或发起人**可调用；参与人不见他人原始回答；发起人视角额外返回各参与人提交状态 |

**v1.2 替换为（逐字，可整表替换）**

```markdown
| 方法 | 请求 | 成功返回 | 权限与限制 |
|---|---|---|---|
| `list_pending_tasks` | `limit?`, `cursor?` | 当前用户的 pending 任务摘要、游标、`next_poll_after` | 只返回 Token 所属用户处于 `pending` 的任务；不返回他人原始回答、私有字段或任何凭据 |
| `get_task` | `task_id` | 主题、背景、本轮问题、上一轮摘要、截止时间、LLM 服务商标识、**本人在本事项上的 `agent_authority`**、**本事项的 `irreversible`** | 仅任务所属用户可调用；不返回他人原始回答、个人模型和本地资料 |
| `submit_output` | `task_id`, `answers[]`, `notes?`, `human_approved?`, `approved_at?`, `content_digest`, `idempotency_key`, **`acting_as`**, **`authority`** | 首次提交结果、任务状态、提交时间、`acting_as`、`authority` | 目标用户匹配；`acting_as` 与 `authority` 均由客户端声明、服务端按 2.4.3 闸门校验（挡位 / 不可逆 / 授权有效性三项，外加 `authority` 与当前 `agent_authority` 的一致性）；校验不过或摘要不匹配复用 `422 HUMAN_APPROVAL_REQUIRED`；摘要校验见 9.3；幂等见 9.4 |
| `get_matter_status` | `matter_id`, `rounds_before?` | 事项状态、轮次进度、最近 3 轮摘要、`rounds_total`、决议阶段与版本 | **参与人或发起人**可调用；参与人不见他人原始回答；发起人视角额外返回各参与人提交状态（含各参与人的 `agent_authority` 与最近一次提交的 `acting_as`、`authority`） |
```

**新增段落（紧随 9.2 表后）**

```markdown
`human_approved` 与 `approved_at` 在 v1.2 起标注为**条件必填**：仅当客户端声明 `acting_as = agent_on_behalf` 并通过 2.4.3 闸门校验时可省略；其余情形下缺失即 `422 HUMAN_APPROVAL_REQUIRED`。`content_digest` 在所有情形下均为必填。

`get_task` 必须返回 `agent_authority` 与 `irreversible`，使本地 Agent 能在提交前判断「可以直接提交」还是「必须转交本人确认」，避免先提交、再被拒的无效往返。

`submit_output` 新增 `acting_as` 与 `authority` 两个请求字段，与路径② 的 `acting_as`、`authority` **同名同义同校验**；两条路径都不以「服务端派生」的方式代替客户端声明（理由见 2.4.3）。至此两条路径的字段完全对称（见 2.4.6）。
```

### 1.7 §9.3 人审声明的留痕要求（整节替换）

**v1.1 原文（逐字，第 449–457 行）**

> ### 9.3 人审声明的留痕要求
>
> `human_approved: true` 本身无法被服务端证明，但可以要求可核对的留痕，防止本地实现直接硬编码为 true：
>
> - `approved_at`：本地确认时间（ISO 8601 UTC）。缺失返回 `422 HUMAN_APPROVAL_REQUIRED`。若 `approved_at` 晚于服务端接收时间，或早于服务端接收时间超过 24 小时，返回 `422 HUMAN_APPROVAL_REQUIRED`。
> - `content_digest`：本地展示给用户确认的最终文本的 SHA-256 十六进制摘要。计算口径为：按 `question_id` 升序拼接每项 `question_id + "\n" + content + "\n"`，末尾追加 `notes`（无 `notes` 时追加空字符串），整体 UTF-8 编码后取 SHA-256。
> - 服务端以相同口径重算摘要，与 `content_digest` 不一致时返回 `422 HUMAN_APPROVAL_REQUIRED`，并在错误信息中说明是摘要不匹配。
> - `approved_at` 与 `content_digest` 一并持久化在 Output 记录中，随原始回答一起对发起人可见。
> - 本要求的效力边界必须如实描述：它只能保证"提交内容与本地声明确认过的内容一致"，不能证明确实有人阅读过。

**v1.2 替换为（逐字，可整节替换）**

```markdown
### 9.3 人审声明的留痕要求

`human_approved: true` 本身无法被服务端证明，但可以要求可核对的留痕，防止本地实现直接硬编码为 true：

- **适用顺序**：本节规则先于字段校验应用于全部提交。先按 2.4.3 判定本次提交是否为「代理提交」，再按下列规则处理 `approved_at` 与 `content_digest`。
- `approved_at`：本地确认时间（ISO 8601 UTC）。**非代理提交时**缺失返回 `422 HUMAN_APPROVAL_REQUIRED`；若 `approved_at` 晚于服务端接收时间，或早于服务端接收时间超过 24 小时，返回 `422 HUMAN_APPROVAL_REQUIRED`。**代理提交（`acting_as = agent_on_behalf` 且通过闸门）时 `approved_at` 留空**，服务端以 `approved_at IS NULL` 作为「本次提交未经本人逐次确认」的留痕依据。
- **`approved_at` 两处落库必须对齐**：`outputs.approved_at`（路径①）与 `stances.approved_at`（路径②）**语义完全相同、且都改为可空**（`stances.approved_at` 为本次新增列，见 2.4.6 与 15.1）。本人提交填本人确认时间；代理提交留空。两处同义是本次修订消除「两路径留痕不对称」的核心动作，后人不要只改一处。
- `content_digest`：本地展示给用户确认的最终文本的 SHA-256 十六进制摘要。计算口径为：按 `question_id` 升序拼接每项 `question_id + "\n" + content + "\n"`，末尾追加 `notes`（无 `notes` 时追加空字符串），整体 UTF-8 编码后取 SHA-256。
- 服务端以相同口径重算摘要，与 `content_digest` 不一致时返回 `422 HUMAN_APPROVAL_REQUIRED`，并在错误信息中说明是摘要不匹配。**该要求与是否为代理提交无关：代理提交同样必填并强校验**（防篡改校验的作用是保证提交内容与本地已生成并摘要过的内容一致，与「谁承诺」无关）。路径② 的对应字段为 `content_hash`，口径与校验强度相同。
- `acting_as` 与 `authority`：两条路径的落库表（`outputs` 与 `stances`）**都有这两个字段**，取值同源（`acting_as` ∈ `human` / `agent_on_behalf`；`authority` ∈ `propose_only` / `can_commit`）。**两条路径一律由客户端声明 `acting_as` 与 `authority`，服务端按 2.4.3 校验挡位、事项可逆性、授权有效性三项，服务端不派生。**`acting_as` 回答「谁提交的」，`authority` 回答「凭什么能提交」；`authority` 记录**提交时刻的授权状态**，须与数据库当前值一致，不一致即 `422 HUMAN_APPROVAL_REQUIRED`。
- `approved_at`、`content_digest`、`acting_as` 与 `authority` 一并持久化，随原始回答一起对发起人可见。**发起人可见 `acting_as` 与 `approved_at` 是否为空，即能识别某条提交是否属于未经本人逐次确认的代理提交。**
- **历史空值的标注（R8，措辞固定）**：`stances` 迁移到 v3 后，历史行的 `authority` 一律为 `NULL`、原值存于 `authority_legacy`（见 2.4.6）。凡遇 `authority IS NULL` 且 `authority_legacy IS NOT NULL` 的记录，**只在内部审计 / 运维视图**中以固定措辞标注：「**该记录早于本版本，无确认时间留痕**」。
  - **明令禁止**写成「**可能是代理提交**」，或任何暗示代理、暗示未授权、暗示 `propose_only` 的措辞。
  - **理由（必须随条款保留）**：新条款下「`approved_at` 为空」正是**代理代承诺的特征**；若按新语义去解读历史空值，会把**历史本人提交误记成代理承诺**——这是**语义污染**，比不标注更糟。
  - **只在内部审计 / 运维视图标注，不放进对外消息**（不出现在 MCP 响应、对外接口与参与人可见的页面上）。
- **系统级事件的留痕（R9，三层）**：迁移类系统事件不挂在任何事项或用户上，按三层留痕：
  1. **主留痕 = `schema_migrations` 表**（`version` / `name` / `applied_at`）——迁移本身的权威记录，**不必**再往 `audit_events` 塞一份。
  2. **数据语义变更才进 `audit_events`**：新增审计事件类型 **`SCHEMA_MIGRATED`**（`matter_id = None`、`actor_user_id = None`，系统事件）。`detail`（JSON 列）里放：`version` / `name` / 受影响行数 / 是否有 `authority_legacy` 回填等数据语义变更标记。**该常量不受「不许新增」约束**——那条禁令在 `hub/api/errors.py` 的 docstring 里，管的是**错误码**；审计事件类型是另一个集合（本项目本轮已经加过 `CONVERGENCE_DEGRADED`）。
  3. **写入时序守卫**：迁移跑在 `create_all` **之前**，全新库上 `audit_events` 可能**还不存在**；写入前先探测表是否存在，不存在就**只写 logging、不报错**，不得把「审计表不存在」视为迁移失败。
- **拒绝路径的错误码**：`irreversible = true` 事项上的代理提交、未开通 `can_commit` 的代理提交、摘要不匹配、`authority` 与当前授权状态不一致，均复用 `422 HUMAN_APPROVAL_REQUIRED`，**不新增错误码**（见 9.5）。
- 本要求的效力边界必须如实描述：它只能保证"提交内容与本地声明确认过的内容一致"，不能证明确实有人阅读过。**代理提交路径进一步弱化了这一点**：该路径下平台不主张任何人曾逐次确认内容，只主张「本人事先授权 Agent 代自己承诺」，且该主张以 `matter_participants.agent_authority` 的当前值与「本人开给自己」的审计记录为依据——服务端**只校验声明与授权状态，不证明是本人**。
```

### 1.8 §9.5 错误码（替换 1 行，表内其余不变）

**v1.1 原文（逐字，第 489 行）**

> | 422 | `HUMAN_APPROVAL_REQUIRED` | `human_approved` 缺失或为 false；`approved_at` 缺失或超出允许时间窗；`content_digest` 与正文不匹配 |

**v1.2 替换为（逐字，可直接替换该行）**

```markdown
| 422 | `HUMAN_APPROVAL_REQUIRED` | `human_approved` 缺失或为 false（含未开通 `can_commit` 的代理提交）；`irreversible = true` 事项上的代理提交；`approved_at` 缺失或超出允许时间窗；`content_digest`（路径② 为 `content_hash`）与正文不匹配；声明的 `authority` 与 `matter_participants.agent_authority` 当前值不一致 |
```

**表后补充（新增一句）**

```markdown
v1.2 的执行方式与歧义纠正：`irreversible` 与 `agent_authority` 的拒绝路径**不新增任何错误码**，统一复用 `HUMAN_APPROVAL_REQUIRED`。既有的同义写法（表内旧表述 `human_approved` 为 false）统一记为 `human_approved` **缺失或为 false**。
```

### 1.9 §9.6 新增小节「对外契约边界与信息最小化」（O1、O2）

**位置**：插在 9.5 之后，成为 §9.6。**新增，无替换对象。**

```markdown
### 9.6 对外契约边界与信息最小化

#### 9.6.1 契约边界（O2）

对外可调用契约全部以 Pydantic 模型声明，**不得裸返回 `dict`**。

> ⚠️ **2026-09-17 更新（本小节原写「限定为两组 = 4 端点 + 4 工具」，与事实不符）**
> 本节最初起草时立场层尚未落地，之后 `/api/items/*` 与 7 个立场层工具陆续补入，
> **本节一直没跟着更新** —— 而 `get_digest` 没有 `DigestOut` 契约这件事，
> 很可能就是因为**它不在下面那份名单里**，没人按契约边界去核它。
> 下列清单按 2026-09-17 实测（12 个 JSON 端点 / 11 个 MCP 工具）重写。
> **变更纪律**：新增任一对外接口，必须同步登记到本节，否则视为未完成。

**1. JSON API 端点（请求/响应均以 Pydantic 模型声明）**

- `/api/items/*`（立场层，`hub/web/routes_api.py`）：
  `POST /api/items`、`GET /api/items`、`POST /api/items/{id}/stances`、
  `GET /api/items/{id}/stances`、`GET /api/items/{id}/stances/analysis`、
  `GET /api/items/{id}/stances/{user_id}`、`POST /api/items/{id}/ask`、
  `GET /api/items/{id}/summary`、`GET /api/items/{id}/digest`、
  `POST /api/items/{id}/decide`
- `/api/agent/*`（任务流水线，`hub/web/routes_agent_rest.py`）：
  `GET /api/agent/tasks`、`GET /api/agent/tasks/{id}`、
  `POST /api/agent/tasks/{id}/output`、`GET /api/agent/matters/{id}/status`

**2. MCP 工具返回值**

- 任务流水线 4 个：`list_pending_tasks`、`get_task`、`submit_output`、`get_matter_status`
- 立场层 7 个：`declare_item`、`submit_stance`、`read_stance`、`get_summary`、
  `ask_participant`、`decide_item`、`get_digest`
- 另 1 个：`list_items`（REST `GET /api/items` 的同源实现，一并注册）
  —— **合计 12 个**，以 `tools/list` 的实际返回为准
  （2026-09-17 真进程实测：12 个全部在线路上列出，见
  `outputs/2026-09-17-真进程冒烟记录.md`）

> **注**：`methods` 层里另有仅供 REST 使用的实现函数（如 `mcp_list_stances`、
> `mcp_read_stance_analysis`），它们**不注册为 MCP 工具** —— 「唯一实现层」与
> 「工具壳」是两层，不必一一对应（`get_digest` 也曾长期只有实现层、没有工具壳，
> 直到 2026-09-16 才补注册）。
>
> ⚠️ **数量口径的教训**：本节的工具数改过两次（4 → 11 → 12），两次都是**凭记忆数**。
> 数工具/端点这类清单，**一律以 `tools/list` 或路由表的实际输出为准**，
> 不要手数。

**明确不在契约边界内**（不要为它们引入 Pydantic 模型）：

- **LangGraph 节点 state**：框架要求 `dict`，不适用 Pydantic 模型约定。
- **Jinja2 HTML 模板上下文**：服务端内部渲染数据，不是对外契约。页面可见性由服务端渲染逻辑与本节 9.6.2 约束保证。

变更纪律：任一契约字段的新增或改型，必须同步更新 Pydantic 模型、§15.1 字段表与 §13.1 追溯矩阵。

#### 9.6.2 信息最小化（O1）

- **`visibility` 只作留痕/审计属性，读侧不产生权限差异**（维持 A2 结论）：`Stance.visibility` 取 `participants` 或 `all`，两者在 V1 读侧**同义**；不得因 `visibility = participants` 而把事项发起人挡在任何列表之外。任何「按 `visibility` 过滤读结果」的实现都属回归，**后世不要"修复"成再挡发起人**。
- **列表响应不含私有字段（保留并强化）**：所有列表类响应（`list_pending_tasks`、`get_matter_status`、`GET /api/items/{matter_id}/stances`、Web 列表页）只返回调用方有权看到的字段；**任何情形下**不含他人原始回答（`answers`）、个人模型、本地资料、Token 明文、密钥与凭据。条目级鉴权通过时，也不得以「列表已鉴权」为由放宽字段级最小化。注：`GET /api/items/{matter_id}/stances/analysis` 与 `get_matter_status` 按既有设计返回差异化的**摘要型**视图（如立场摘要、分歧方立场摘要），这是「摘要可见」而非「原始回答可见」，两者不得混同。
- **术语边界提示**：§6.2 FR-07 与第 8 章页面表中的「可见性过滤」指的是**参与关系可见性**（发起人 ∪ 参与人），与 `Stance.visibility` 字段**不是同一回事**。本次修订**不改动** FR-07 与第 8 章的相关表述；两者混用会导致误改，见 §15.1 的字段说明。

> **以下两条为 2026-09-17 追加**，用于把两处**既有分叉正式文档化** ——
> 它们不是新裁定，是把实现里已经存在、且各自有据的口径写下来，免得后人
> 读到两处矛盾去「修」其中一处。

- **非成员的状态码在两层不同，且这是有意的**：
  - 立场层（`/api/items/*`）非成员一律 **404 `RESOURCE_NOT_FOUND`**，文案
    「事项不存在」——**不泄露事项是否存在**（本小节上一段的要求）。
  - 任务流水线（`/api/agent/*`，含 `get_matter_status`）非成员保持 **403
    `FORBIDDEN_SCOPE`** 并按 §9.5 错误码表执行。
  - 两层是不同时期的产物：403 沿用 v1.1，404 是隐私评估（roR8pK）之后所立，
    **没有一份文档同时管两层**，分叉因此长期未被发现。若要统一，属**行为
    变更**（会翻转既有断言），须走修订流程，不得顺手改。

- **`get_summary` 的读侧闸门 = 事项成员**（发起人 ∪ 参与人），非成员 404、
  文案同「事项不存在」，与立场层其余读口同口径。
  该闸门是 **2026-09-16 补上的**（原实现**没有任何闸门**，任何持令牌用户都能读到
  任意事项的摘要 —— 属越权读缺陷），此前本节对读权限未作规定。**不得放宽回去。**
```

### 1.10 §13 验收场景（追加 26–35，替换 §13.1 矩阵相关行）

**追加场景（接在 v1.1 第 25 条之后）**

```markdown
26. **代理承诺开关默认与开通前行为**：新事项的参与人 `agent_authority` 默认为 `propose_only`；未开通时，MCP `submit_output` 与 HTTP 立场接口在声明 `acting_as = agent_on_behalf` 时均返回 `422 HUMAN_APPROVAL_REQUIRED`，任务保持 `pending`，不产生 Output；声明 `acting_as = human` 但缺 `human_approved` 同样返回 `422`。
27. **代理提交的两路径一致性**：本人为自己开通 `can_commit` 后，在 `irreversible = false` 的事项上分别用 MCP `submit_output` 与 HTTP 立场接口提交，且**两条路径都显式声明** `acting_as = agent_on_behalf`、`authority = can_commit`：两次均成功；`outputs.approved_at` 与 `stances.approved_at` **均为空**，两处 `acting_as = agent_on_behalf`、`authority = can_commit`。对照用例：`content_digest` / `content_hash` 与正文不匹配时两条路径均返回 `422 HUMAN_APPROVAL_REQUIRED`；`authority` 声明与数据库当前值不一致时同样返回 `422`。
28. **开关撤销立即生效**：撤销后同一 Agent 再声明 `acting_as = agent_on_behalf` 被拒（`422 HUMAN_APPROVAL_REQUIRED`）；随后改声明 `acting_as = human` 并携带本人 `human_approved` 提交成功，两处 `acting_as = human`、`approved_at` 非空；开通与撤销均可在审计中查到，且审计的 `actor_user_id` 为被代理的本人。
29. **未开通时不产生代理标记**：`propose_only` 下由 Agent 执行但声明 `acting_as = human` 且携带本人 `human_approved` 的提交成功，落库为 `human`（不得记成 `agent_on_behalf`）。
30. **不可逆事项拒绝代理提交（Q9–Q12）**：事项 `irreversible = true` 时，`can_commit` 参与人声明 `acting_as = agent_on_behalf` 的提交被拒，返回 `422 HUMAN_APPROVAL_REQUIRED`（**不得出现新错误码**）；改由**本人自己**提交（`acting_as = human` + 本人 `human_approved`）后成功。
31. **不可逆标记的权限与防赖账**：非发起人（管理员、其他参与人、Agent）变更 `irreversible` 一律被拒；发起人变更写入审计；该事项已存在 `acting_as = agent_on_behalf` 的提交后，改为 `irreversible = true` 的请求被拒，且原代理提交不被追溯撤销。
32. **契约边界与信息最小化**：4 个 JSON API 端点与 4 个 MCP 工具返回值均有 Pydantic 模型（无裸 `dict`、无 `Any` 兜底）；列表响应中不含他人原始回答、私有字段、Token 与密钥；`visibility = participants` 不会把发起人挡在立场列表之外。
33. **告知文案**：`/login` 首次登录页与 `/matters/new` 创建页同时展示主告知、「可能存在未经本人逐次确认的代理提交」提示与当前服务商名称；参与人开通 `can_commit` 时出现一次性告知，且告知内容包含「不再逐次要求本人确认」「可随时撤销」「不可逆事项上会被拒绝」三点（文案措辞以 owner 定稿版本为准）。
34. **迁移与存量数据**：在带存量数据的库上执行迁移 v3：`matters.irreversible`、`matter_participants.agent_authority`、`outputs.acting_as`、`outputs.authority`、`stances.approved_at`、`stances.authority_legacy` 六列就位且默认值/可空性正确，`stances.authority` 已收紧为枚举列；存量 `outputs.acting_as` 全部为 `human`；存量 `stances.authority` **全部为 `NULL`**，原自由文本**逐字节**保留在 `authority_legacy`；三张重建表的外键与唯一约束（`outputs.task_id`、`stances(matter_id, round_number, user_id)`、`stances.supersedes` 自引用）保持有效；重复执行迁移不产生重复变更。留痕按三层验证：`schema_migrations` 记录版本 3；`audit_events` 存在时新增一条 `SCHEMA_MIGRATED`（`matter_id` / `actor_user_id` 均为 NULL，`detail` 含 `version` / `name` / 受影响行数 / `authority_legacy` 回填标记）；在**全新库**（`audit_events` 尚不存在）上执行同样成功，且只写 logging、不报错、不产生 `SCHEMA_MIGRATED` 记录。
35. **禁止启发式回填与历史空值标注**：构造历史 `stances` 行，其 `authority` 原文包含形如「代理授权」「agent_on_behalf」「can_commit」等字样；迁移后断言 `authority` 为 `NULL` 且 `authority_legacy` 与原值完全一致（**不得**因文本内容被置为 `can_commit`）；内部审计/运维视图中该行标注为「该记录早于本版本，无确认时间留痕」，且文案中**不出现**「代理」「未授权」「propose_only」等暗示档位的词；同一标注**不出现在** MCP 响应与参与人可见页面上。
```

**§13.1 追溯矩阵替换与追加（逐字）**

v1.1 原文（第 667 行）：

> | FR-12 人审提交 | 4、14 |

替换为：

```markdown
| FR-12 人审提交（含代理承诺例外） | 4、14、26、27、28、29、30 |
| FR-12b 代理承诺开关自助管理 | 26、27、28、33 |
| FR-12c 不可逆事项标记与变更 | 30、31 |
| 2.4.6 字段与迁移（v3） | 34、35 |
| 2.4.6 / 9.3 禁止启发式回填与历史空值标注（R6、R8） | 35 |
| 9.3 系统级事件留痕与 2.3 legacy 留存（R9、R10） | 34 |
```

并在矩阵表尾「新增需求或修改需求时必须同步更新本矩阵」之前追加一行：

```markdown
| O1/O2 契约边界与信息最小化 | 32 |
```

**同时需替换的 v1.1 矩阵/场景行**：场景 4「人审闸门」的表述需补「默认路径」限定，避免与 26–30 冲突。建议改为：

```markdown
4. **人审闸门（默认路径）**：在未开通 `can_commit` 或事项为不可逆时，`human_approved` 缺失或为 false 返回 `422`，不改变任务状态。
```

### 1.11 §4 核心概念与数据边界（替换 2 行 + 新增 2 行）

**v1.1 原文（逐字，第 116、119 行）**

> | Matter | 协作事项，包括主题、背景、目标、参与人和状态 | 保存 |
> | Output | 参与人经确认后提交的结构化纯文本回答 | 保存 |

**v1.2 替换为（逐字，可直接替换这两行并追加 MatterParticipant、Stance 行）**

```markdown
| Matter | 协作事项，包括主题、背景、目标、参与人、状态与是否不可逆（`irreversible`） | 保存 |
| MatterParticipant | 事项与参与人的关系，含该参与人在本事项上的代理承诺开关（`agent_authority`） | 保存 |
| Output | 参与人提交的结构化纯文本回答，含人审留痕（`approved_at` 可为空、`content_digest`）、提交形态标注（`acting_as`）与授权状态（`authority`，可为空） | 保存 |
| Stance | 参与人在某轮次对议题的结构化立场，含提交形态（`acting_as`）、授权状态（`authority`，可为空）与确认时间（`approved_at`，可为空）；历史授权原值保留在只读的 `authority_legacy`，不参与任何判定；字段定义见 15.1 | 保存 |
```

**§4.1「允许进入平台的数据」追加一条**

```markdown
- 代理承诺开关值（`agent_authority`）、不可逆标记（`irreversible`）、提交形态（`acting_as`）、授权状态（`authority`）与本人确认时间（`approved_at`，代理提交时为空）。
- 历史授权原值（`authority_legacy`，只读，仅承接迁移前的自由文本，不参与任何判定）。
```

**§4.2「禁止进入平台的数据」不变**（`agent_authority` 是关系属性，不含个人模型或资料）。

**附：§2.3「数据留存边界」追加一段（R10）**

**v1.1 原文（逐字，第 65 行）**

> 归档后的事项、轮次、摘要、经人审声明的输出、决议和审计默认长期保留，V1 不做自动删除。删除、导出和按组织配置留存周期列入后续版本。

**v1.2 替换为（逐字，可在原段后追加）**

```markdown
归档后的事项、轮次、摘要、经人审声明的输出、决议和审计默认长期保留，V1 不做自动删除。删除、导出和按组织配置留存周期列入后续版本。

`authority_legacy`（历史授权原值列，见 2.4.6、15.1）**不设自动过期**：它是**唯一**能证明历史授权约定的载体，清掉就永久丢失，而它只是一列文本，成本可忽略。提供**只读导出**（运维脚本或只读接口读取），不出现在对外接口与参与人可见页面。该列标注为「**只读兼容列，未来大版本可评估下线**」——给未来留一条下线通道，但默认不主动删除。
```

**说明**：本段落在 2.3 而不是 9.x，因为「数据留存边界」在 PRD 中的单一事实源是 2.3，第 9 章是接口契约章节，不留存策略。

### 1.12 §15 术语表（替换 1 条 + 追加 12 条 + 新增 §15.1 字段表）

**v1.1 原文（逐字，第 710 行）**

> | 人审声明 | Agent 提交时携带的 `human_approved: true` 及配套 `approved_at`、`content_digest`，表示本地用户已确认内容；服务端可校验摘要一致性与时间合理性，但仍不能证明确实有人阅读过 |

**v1.2 替换为**

```markdown
| 人审声明 | Agent 提交时携带的 `human_approved: true` 及配套 `approved_at`、`content_digest`，表示本地用户已确认内容；服务端可校验摘要一致性与时间合理性，但仍不能证明确实有人阅读过。仅适用于**非代理提交**；代理提交（`acting_as = agent_on_behalf`）不产生人审声明（见 2.4.3、9.3） |
```

**追加术语（表尾，逐字）**

```markdown
| `agent_authority` | 挂在「事项 × 参与人」关系上的代理承诺开关，仅两档：`propose_only`（默认）/ `can_commit`。只能由被代理的本人开通与撤销，落库 `matter_participants.agent_authority`（见 2.4） |
| `propose_only` | `agent_authority` 的默认挡位：Agent 只能提议，提交必须携带本人 `human_approved: true` |
| `can_commit` | `agent_authority` 的打开挡位：本人事先授权后，在**可逆事项**上允许 Agent 免本人逐次确认提交；不可逆事项上该挡位不生效（见 2.4.3、2.4.5） |
| 代理提交 | `acting_as = agent_on_behalf` 的提交（Output 或 Stance）：提交时未携带本人确认，依据 `can_commit` 由 Agent 代本人承诺；以 `approved_at IS NULL` 留痕 |
| `acting_as` | 提交形态标注，取值 `human` / `agent_on_behalf`，与 `hub.schemas.stance.ActingAs` 的枚举取值同源；**两条路径一律由客户端声明、服务端按 2.4.3 校验**（见 9.3）。回答「**谁**提交的」 |
| `authority` | 授权状态标注，取值 `propose_only` / `can_commit`，与 `agent_authority` 同源；**`outputs` 与 `stances` 两处同名同义、均可空**；记录**提交时刻的授权状态**，须与数据库当前值一致。回答「**凭什么**能提交」（见 2.4.3、9.3） |
| `authority_legacy`（本次修订变更） | `stances` 新增的**只读**自由文本列，仅承接迁移前 `authority` 的原值，**不参与任何判定**。迁移后历史行的 `authority` 一律为 `NULL`，语义是「历史值不可识别」；**禁止启发式解析，禁止推断授权档位**（见 2.4.6、9.3）。**不设自动过期**（它是唯一能证明历史授权约定的载体，清掉就永久丢失，而一列文本的成本可忽略）；提供**只读导出**（运维脚本或只读接口），不出现在对外接口与参与人可见页面；标注为「**只读兼容列，未来大版本可评估下线**」，默认不主动删除（见 2.3） |
| 历史值标注 | 仅在**内部审计 / 运维视图**对 `authority IS NULL` 且 `authority_legacy IS NOT NULL` 的记录使用的固定措辞：「该记录早于本版本，无确认时间留痕」。**禁止**写成「可能是代理提交」或任何暗示代理/未授权的措辞（见 9.3） |
| `SCHEMA_MIGRATED`（本次修订新增） | 迁移类**系统级审计事件类型**，仅当迁移涉及数据语义变更时写入 `audit_events`（`matter_id = None`、`actor_user_id = None`，`detail` 含 `version` / `name` / 受影响行数 / `authority_legacy` 回填标记）。**不受错误码「不许新增」约束**（那条管错误码，不管审计事件类型）；写入前必须探测 `audit_events` 表存在性，不存在只写 logging 不报错（见 9.3「系统级事件的留痕」） |
| `approved_at`（本次修订变更） | 本人确认时间留痕。在 `outputs` 与 `stances` 两处**同义且均可为空**：本人提交填确认时间，代理提交留空。见 2.4.6、9.3、15.1 |
| `irreversible` | 事项级布尔标记，落库 `matters.irreversible`；为 true 时该事项不接受任何代理提交（代理提交一律 `422 HUMAN_APPROVAL_REQUIRED`） |
| 不可逆事项 | `irreversible = true` 的协作事项。默认不勾选（默认可逆），仅发起人可变更且必须记审计；已存在代理提交后禁止改为不可逆（见 2.4.4） |
```

**新增 §15.1「Stance 字段」（本节是 `hub/schemas/stance.py` 所指的「PRD『Stance 字段』」）**

```markdown
### 15.1 Stance 字段（立场字段）

本节是 `Stance`（立场）字段定义的**单一事实源**：`hub/schemas/stance.py` 模块 docstring 所称「字段定义以 PRD『Stance 字段』为单一事实源」，指向的就是本节。任何字段增删改型必须先改本节，再改代码。

**基本约束**

- 唯一约束：`(matter_id, round_number, user_id)` —— 同一事项同一轮同一人只有一条立场。
- 提交前提：提交人必须是**本事项参与人**；非参与人、非事项成员一律 `404 RESOURCE_NOT_FOUND`，且文案与「事项不存在」一致，不泄露事项是否存在。
- 出参/入参分离：`stance_id`、`matter_id`、`user_id`、`created_at` 为**服务端派生**，不接受客户端注入。
- 提交形态与授权状态的一致性约束：`acting_as = agent_on_behalf` ⇒ `authority = can_commit` 且 `approved_at` 留空；`acting_as = human` ⇒ `approved_at` 必填。三者矛盾的请求返回 `422 HUMAN_APPROVAL_REQUIRED`（口径与 9.3 一致）。

**字段表**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `stance_id` | string | 出参 | 服务端生成，前缀 `stn` |
| `matter_id` | string | 出参 | 来自路径参数，不在请求体中 |
| `round_number` | integer ≥ 1 | 入参 | 轮次号 |
| `user_id` | integer | 出参 | 来自 Bearer 身份，不在请求体中 |
| `stance` | 枚举 `support` / `oppose` / `conditional` / `abstain` / `need_info` | 入参 | 立场指向 |
| `confidence` | float 0.0–1.0 | 入参 | 置信度 |
| `position_summary` | string (1–2000) | 入参 | 立场摘要 |
| `rationale_summary` | string (1–5000) | 入参 | 依据摘要 |
| `non_negotiables` | string[] ≤ 50 项，每项 1–500 | 入参 | 不可让步项 |
| `conditions` | string[] ≤ 50 项，每项 1–500 | 入参 | 附带条件 |
| `open_questions` | string[] ≤ 50 项，每项 1–500 | 入参 | 待解问题 |
| `depends_on` | string[] ≤ 50 项，每项 1–500 | 入参 | 依赖项 |
| `questions_for` | `{participant_id, question}`[] ≤ 50 项 | 入参 | 向指定参与人提问 |
| `disagreement_kind` | 枚举 `goal` / `fact` / `risk_appetite` / `resource`，可为空 | 入参 | 分歧类型 |
| `supersedes` | string ≤ 48，可为空 | 入参 | 取代的既有立场 `stance_id` |
| `acting_as` | 枚举 `human` / `agent_on_behalf` | 入参 | 本次提交的形态。**与 Output 的 `acting_as` 同源但独立落库**。由请求体声明，服务端按 2.4.3 闸门校验（挡位 / 事项可逆性 / 授权有效性三项）；声明 `agent_on_behalf` 但校验不通过时 `422 HUMAN_APPROVAL_REQUIRED`。回答「谁提交的」 |
| `authority` | 枚举 `propose_only` / `can_commit`，可为空 | 入参 | **本次修订由 `VARCHAR(255)` 自由文本收敛为枚举**（列类型收紧，随 v3 迁移重建表）。取值与 `agent_authority` 一致，记录提交时刻的授权状态，须与数据库当前值一致；不一致时 `422 HUMAN_APPROVAL_REQUIRED`。回答「凭什么能提交」。**`outputs.authority` 为同名同义列**（见 2.4.6） |
| `authority_legacy` | string ≤ 255，可为空 | **只读，不入参** | **本次修订新增列**，仅承接迁移前 `authority` 的自由文本原值，**不参与任何判定**，也不出现在任何提交/读取接口的入参或出参中。迁移后历史行的 `authority` 一律为 `NULL`（语义=历史值不可识别），**禁止启发式解析或推断档位**；内部视图的标注措辞见 9.3（R6、R8） |
| `approved_at` | datetime (UTC)，**可空** | 入参 | **本次修订新增列**，与 `outputs.approved_at` 语义相同：本人提交填本人确认时间（`acting_as = human`）；代理提交留空（`acting_as = agent_on_behalf`），以 `IS NULL` 作为「未经本人逐次确认」的留痕依据。该列时间窗校验口径与 9.3 对 `outputs.approved_at` 的规定一致 |
| `ttl_seconds` | integer ≥ 60，可为空 | 入参 | 有效期 |
| `urgency` | 枚举 `low` / `normal` / `high`，默认 `normal` | 入参 | 紧急度 |
| `visibility` | 枚举 `participants` / `all`，默认 `participants` | 入参 | **只作留痕/审计属性，读侧不产生权限差异**（A2 结论，见 9.6.2）；`all` 为保留值，V1 与 `participants` 同义 |
| `content_hash` | 64 位小写十六进制 | 入参 | 服务端按同一口径重算并强校验（即路径② 的 `content_digest` 对应物）；不匹配返回 `422 HUMAN_APPROVAL_REQUIRED`（摘要不匹配） |
| `created_at` | datetime (UTC) | 出参 | 服务端生成 |

**枚举与 CHECK 的落地说明**：上表枚举取值由 `stances` 表的 CHECK 约束与 `hub.schemas.stance` 的 `StrEnum` 两侧保持一致。本次修订新增或收紧的字段（`matters.irreversible`、`matter_participants.agent_authority`、`outputs.acting_as`、`outputs.authority`、`outputs.approved_at` 可空、`stances.approved_at` 新增可空、`stances.authority` 收紧为枚举、`stances.authority_legacy` 新增只读）**都随 2.4.6 的同一条 v3 迁移落地**，同样要求「PRD 本节（或 2.4.6）— ORM 枚举 — CHECK 约束」三处一致。`authority_legacy` 不受 CHECK 约束（自由文本），但**必须保证只读**：写入路径不得更新它。
```

**关于「悬空引用」的处理选择（要求二选一，本稿选 A）**

- **选择：A —— 补一节定义 Stance 字段（§15.1），使 `hub/schemas/stance.py:3` 所引的「PRD『Stance 字段』」真实存在。**
- 理由：
  1. 硬约束要求本稿**不改仓库文件**。方案 B（改注释指向 `hub/schemas/stance.py` 自己）必须编辑 `hub/schemas/stance.py:3`，超出本稿范围且会污染仓库。
  2. PRD 已被两份文档公认为产品契约的单一事实源（§14「两份文档冲突时以本 PRD 为准」）。把字段表放进 PRD，引用即刻成立，且不需要任何代码改动，是零风险修复。
  3. 反向风险可封住：若评审最终更认可 B，则应把注释改为指向 `hub/schemas/stance.py` 自身，并**删除 §15.1**，避免同一事实出现两个来源。该取舍列入待办（见 §4.2 待办 3）。

### 1.13 §14 技术附录索引（追加一句，不替换既有内容）

```markdown
本次修订新增的字段与迁移（2.4.6）在实现上由设计文档承接；设计文档需同步补充：`agent_authority` 闸门的校验位置（挡位 / 不可逆 / 授权有效性三项）、`acting_as` 与 `authority` 的声明与校验点（两条路径同名同义，均不派生）、迁移版本 3 的 DDL 与存量数据回填规则（`outputs.acting_as` 回填 `human`；`stances.authority` 一律置 `NULL` 且原值搬到只读的 `authority_legacy`）、迁移留痕的三层机制（`schema_migrations` 主留痕 + `SCHEMA_MIGRATED` 数据语义事件 + 表存在性探测守卫，见 9.3）、`authority_legacy` 的只读导出通道、以及 MCP 4 个工具返回值从裸 `dict` 改为 Pydantic 模型（9.6.1）。设计文档与 PRD 冲突时仍以 PRD 为准。
```

---

## 2. 变更日志（旧 → 新）

| # | 章节 | v1.1 原文（逐字） | v1.2 新文 | 决策 |
|---|---|---|---|---|
| 1 | 2.1 V1 范围 | `submit_output` 强制携带 `human_approved: true`、`approved_at` 与 `content_digest`，服务端校验摘要一致性但不宣称能证明真实人工操作。 | 改为「默认强制携带」，并新增 2 条并列项：受控例外与开关自助管理 | Q1–Q7 |
| 2 | 2.4（新增） | 无 | 新增 §2.4：开关定义、开通/撤销、统一闸门判定表、不可逆判定与变更、被拒语义、字段与落库要求 | Q1–Q12 |
| 3 | 3 角色表 | 平台管理员禁止操作：不能代替参与人提交人审输出；不能绕过发起人拍板 | 追加「不能替参与人开通或撤销 `agent_authority`」 | Q7 |
| 4 | 3 角色表 | 事项发起人禁止操作：不能修改参与人的 `human_approved` 声明；不能以他人身份提交 Agent 输出 | 追加「不能替参与人开通或撤销 `agent_authority`；不能代本人出具 `human_approved`」，并纳入职责列「标记或变更本事项是否不可逆」 | Q7、Q9、Q10 |
| 5 | 3 角色表 | 参与人职责：通过个人 Agent 完成本人任务并确认输出 | 改为「默认挡位 `propose_only`……可选择为自己开通 `can_commit`……并可随时撤销」 | Q2、Q7 |
| 6 | 3 角色表 | 个人 Agent 职责：使用用户 Token 拉取、读取和提交任务 | 补「`can_commit` 且可逆时可代本人提交（`acting_as = agent_on_behalf`）；否则须先取得本人 `human_approved`」，禁止列补「不能自行开通或提升 `agent_authority`；不能伪造本人 `human_approved`」 | Q4、Q7 |
| 7 | 3.0.1（新增） | 无 | 细化「不能以他人身份提交」为三条：身份不可借、承诺能力不可借、本人同意不可代出 | Q7 |
| 8 | 4 实体表 | Matter / Output 两行 | Matter 加 `irreversible`；Output 加 `acting_as` 与 `approved_at` 可空说明；新增 MatterParticipant、Stance 两行 | Q1、Q5、Q6、Q9 |
| 9 | 4.1 允许数据 | 无对应条目 | 追加「`agent_authority`、`irreversible`、`acting_as`、`authority`、`approved_at`」 | Q1、Q5、Q6、Q9 |
| 10 | 4.3 告知义务 | 单条告知义务 | 追加 Q8①「可能存在未经本人逐次确认的代理提交」与 Q8② 开通一次性告知 | Q8 |
| 11 | 6.2 FR-12 | 缺少或为 `false` 的 `human_approved` 返回 `422`…… | 改为「默认路径 + 受控例外」的完整表述 | Q3–Q6 |
| 12 | 6.2（新增） | 无 | 新增 FR-12b（开关自助管理）、FR-12c（不可逆标记与变更） | Q1、Q2、Q7、Q9、Q10 |
| 13 | 9.2 方法表 | `submit_output` 请求含 `human_approved`, `approved_at`（无 `?`） | 两者标为条件必填（`?`）；请求**新增 `acting_as`**；成功返回增 `acting_as`；限制列指向 2.4.3 | Q3–Q6 |
| 14 | 9.2 方法表 | `get_task` 成功返回：主题、背景、本轮问题、上一轮摘要、截止时间、LLM 服务商标识 | 追加 `agent_authority`、`irreversible` | Q3、Q11 |
| 15 | 9.2 方法表 | `get_matter_status` 发起人视角「额外返回各参与人提交状态」 | 补明含各参与人 `agent_authority` 与最近一次 `acting_as` | Q1、Q6 |
| 16 | 9.2 表后 | 无 | 新增两段：条件必填口径；`get_task` 必须返回开关与不可逆标记的理由 | Q3、Q11 |
| 17 | 9.3 留痕要求 | `approved_at` 缺失返回 `422`……；`approved_at` 与 `content_digest` 一并持久化 | 重写为「先判闸门、再校验字段」；`approved_at` 在 `outputs` 与 `stances` **两处同义且均可空**、代理提交留空即留痕；`acting_as` / `authority` 均为客户端声明、服务端校验（不派生）；效力边界弱化说明 | Q4、Q5、Q6、Q11、Q12 |
| 18 | 9.5 错误码 | `HUMAN_APPROVAL_REQUIRED`：`human_approved` 缺失或为 false；`approved_at` 缺失或超出允许时间窗；`content_digest` 与正文不匹配 | 触发条件扩写 5 项（未开通 `can_commit` 的代理提交、不可逆事项上的代理提交、`approved_at`、`content_digest`/`content_hash` 不匹配、`authority` 与当前授权状态不一致）；**不新增错误码** | Q11、Q12 |
| 19 | 9.6（新增） | 无 | 新增 §9.6 契约边界（O2）与信息最小化（O1） | O1、O2 |
| 20 | 13 验收场景 | 场景 4「人审闸门」 | 改标题与表述为「默认路径」，避免与新增场景冲突 | Q4 |
| 21 | 13 验收场景 | 共 25 条 | 追加 26–34 共 9 条 | Q1–Q12、O1、O2 |
| 22 | 13.1 追溯矩阵 | FR-12 人审提交 → 4、14 | 改覆盖为 4、14、26、27、28、29、30；新增 FR-12b、FR-12c、2.4.6 字段与迁移、O1/O2 共四行 | Q1–Q12 |
| 23 | 14 技术附录索引 | 无本次相关内容 | 追加设计文档需承接的四项同步清单 | Q1、Q6、O2 |
| 24 | 15 术语表 | 人审声明一条 | 该条补「仅适用于非代理提交」；追加 12 条（`agent_authority`、`propose_only`、`can_commit`、代理提交、`acting_as`、`authority`、`authority_legacy`、历史值标注、`SCHEMA_MIGRATED`、`approved_at`（本次变更）、`irreversible`、不可逆事项） | Q1–Q11 |
| 25 | 15.1（新增） | 无 | 新增 §15.1「Stance 字段」，作为 `hub/schemas/stance.py:3` 所指「PRD『Stance 字段』」的真实落点；含 A2 可见性语义、`acting_as` 闸门校验、新增 `approved_at` 与 `authority` 枚举 | O1 |
| 26 | 附录 A.5 待评审确认项 3 | 3. **`content_digest` 强校验**（9.3）：会对本地 Agent 实现提出额外要求，需确认是否接受该实现成本，或降级为"记录但不强制校验"。 | **本项由 Q4 关闭**：摘要强校验确定保留，且在代理提交路径下同样强制 | Q4 |

### 2.1 本轮回合追加的条款变更（owner 2026-09-13 拍板，针对本稿前一版）

| # | 章节 | 本稿前一版 | 本稿现版 | 依据 |
|---|---|---|---|---|
| 27 | 2.4.6、9.3、15.1 | 路径② 无 `approved_at` 对应物，该问题列为待办 | `stances` **新增 `approved_at`（可空）**，与 `outputs.approved_at` 语义对齐；三处（实体表、字段表、留痕要求）同步；明确为表结构变更，随 v3 迁移加列 | owner 拍板 1 |
| 28 | 2.4.3、9.2、9.3 | 路径① 服务端派生 `acting_as`；路径② 客户端声明 | **两条路径统一为「客户端声明 + 服务端闸门校验」**，并列出校验三项（挡位 / 不可逆 / 授权有效性）；明确服务端只校验不证明 | owner 拍板 2 |
| 29 | 2.4.6、9.3、15.1 | `stances.authority` 保留 `VARCHAR(255)` 自由文本 | **收敛为枚举 `propose_only` / `can_commit`**；写明与 `acting_as` 的分工（谁提交 / 凭什么提交）；随 v3 迁移收紧列类型 | owner 拍板 3 |
| 30 | 4.3 | 只列「必须覆盖的三要素」，未给措辞 | 给出**可直接粘贴的两处文案草案**，均标注【待 owner 定稿】 | owner 拍板 4 |
| 31 | 2.4.6 | 只说明需要一条 v3 迁移 | 明确**多项变更合并为同一条 v3**，并给出合并理由与不推荐的拆分方案；新增存量回填规则 | 本轮新判 |
| 32 | 1.10、13.1、4 | 追加场景 26–33 | 改写 26–30 以匹配「客户端声明」口径，追加场景 34–35；§4 改为「已关闭 / 待确认」两段式 | 本轮新判 |
| 33 | 2.4.6、9.3、15.1 | `stances.authority` 回填采用启发式映射（可识别为代理的置 `can_commit`） | **改为禁止启发式猜测**：新枚举列一律 `NULL`（语义=历史值不可识别），原值整体搬迁到只读列 `authority_legacy`，迁移写审计；读侧禁止推断档位；理由（猜错=伪造授权记录）写入条款 | owner 拍板 R6 |
| 34 | 2.4.3、2.4.6、9.2、9.3、实体表、术语表、15.1 | 路径① 无 `authority` 对应字段（列为待办） | **`outputs` 新增 `authority` 枚举列（可空）**，两条路径字段完全对称；该遗留待办已关闭 | owner 拍板 R7 |
| 35 | 9.3、2.4.6、术语表 | 存量 `approved_at` 为空的标注语义未定（列为待办） | 固定措辞「该记录早于本版本，无确认时间留痕」，只在内部审计/运维视图；**禁止**任何暗示代理的措辞，理由（避免语义污染）写入条款 | owner 拍板 R8 |
| 36 | 9.3、2.4.6、术语表、场景 34 | 迁移审计只写「记一条审计」，未落地 | **三层落地**：`schema_migrations` 为主留痕；`audit_events` 新增 `SCHEMA_MIGRATED` 常量（不受错误码禁令约束，系统事件 matter_id/actor 均为 NULL）；写入前探测表存在，不存在只写 logging 不报错；`detail` 含 version / name / 受影响行数 / legacy 回填标记 | owner 拍板 R9 |
| 37 | 2.3、术语表 `authority_legacy` | `authority_legacy` 的保留期限与导出策略未定（列为待办） | **不设自动过期**（唯一授权载体，成本可忽略）+ 提供**只读导出** + 标注「只读兼容列，未来大版本可评估下线」 | owner 拍板 R10 |

### 2.2 2026-09-14 / 2026-09-16 追加的条款变更（owner 后续裁定，针对本稿）

> 本表为**追加变更**，不修改 §2 / §2.1 的历史记录行。依据：owner 2026-09-14《裁决答复》与 owner 2026-09-16 更正裁定。

| # | 章节 | 本稿原状 | 追加变更 | 依据 |
|---|---|---|---|---|
| 38 | **6.2 FR-20（v1.1 条款）** | 本稿沿用 v1.1 的 FR-20「仅发起人可以拍板｜参与人和 Agent 无拍板权限」，**未列出该行的修订**（漏项） | **FR-20 语义修订**：`can_commit` 成为**显式例外** —— 普通事项下 Agent 经 `decide_item` 可代本人终裁；`irreversible` 事项仍强制拉本人、Agent 无终裁权。**本条即为该修订的正文**（授权开关的机制见 §2.4，拍板闸门的落地见 `hub/api/resolutions.py`）。v1.1 的 FR-20 行已加注「已被 v1.2 修订」，**其条款正文不改** | owner 2026-09-14 裁决 2 |
| 39 | 4.3 告知义务（§1.4） | 两处文案草案均标注【待 owner 定稿】 | **文案定稿**：采用交接稿 §8.2-S4 的现有草案措辞；摘除两处【待 owner 定稿】标记（§2.1 / §0.5 R4 的历史记录行不改） | owner 2026-09-14 §8.4-1 |
| 40 | §4.2 待办 1 / 4（前半）/ 5 / 6 | 列为待确认 | **四条关闭**，移入 §4.1：① 4.3 文案定稿；② `irreversible` 变更理由**必填**；③ 开通 `can_commit` **限非终态事项**；④ §1.1 定位句**维持现状** | owner 2026-09-14 §8.4-1/2/3/4 |
| 41 | §4.2 待办 1 / 4 的**后半问句** | 与各自前半并列写在同一条内 | **拆出并保留在 §4.2**，因为两问都**没被答**：① 开通 `can_commit` 的一次性告知是否还需「我已阅读」勾选确认 —— 09-14 §8.4-1 只答「措辞定稿」，未涉勾选；② 撤销 `can_commit` 后历史代理提交是否需在 Web 标注「授权已撤销」 —— 09-14 §8.4-2 只答「理由必填」。**此为如实拆分，非 owner 裁定** | 复核 09-14《裁决答复》原文，两问均未涉及 |
| 42 | §15.1 A2 可见性语义 | — | **不受影响**。owner 2026-09-16 另行定稿 roR8Pk 第 1 条为「事项进行中只见本人立场、进入终态后全员可见」（按**事项状态**判定，非按人数）。该条属**读侧过滤**，与本稿 §3 已列的「`visibility` 读侧过滤全部移除（维持 A2）」互不冲突：A2 定的是 `visibility` **字段**语义（留痕属性），09-16 裁定定的是**列表/单读的过滤规则** | owner 2026-09-16 裁定 2 |

---

## 3. 未采纳/故意的行为（便于评审对照）

| 项 | 本稿做法 | 说明 |
|---|---|---|
| `visibility` 读侧过滤 | 全部移除 | 维持 A2；见 9.6.2 |
| `acting_as` 由服务端派生 | **不做** | 两条路径统一「客户端声明 + 服务端闸门校验」（本期 owner 拍板） |
| `authority` 保留自由文本 | **不做** | 收敛为枚举 `propose_only` / `can_commit`（本期 owner 拍板） |
| 历史 `authority` 启发式回填 | **不做（硬红线）** | 授权记录宁可留洞不可猜；猜错等于伪造授权记录（本期 owner 拍板 R6） |
| 把 `approved_at` 为空的记录标注成代理提交 | **不做** | 会把历史本人提交误记成代理承诺（语义污染）；措辞固定为中性（本期 owner 拍板 R8） |
| 每次迁移都往 `audit_events` 塞一条 | **不做** | 主留痕是 `schema_migrations`；只有数据语义变更才写 `SCHEMA_MIGRATED`（本期 owner 拍板 R9） |
| `authority_legacy` 自动过期 | **不做** | 唯一授权约定载体，清掉即永久丢失，成本可忽略；给只读导出与未来下线通道（本期 owner 拍板 R10） |
| 新增错误码 | 不新增 | Q12 明确；`hub/api/errors.py` 的「Do NOT add others」维持 |
| 不可逆的两档以上状态 | 不引入 | Q2 明确「无第三档」；`irreversible` 为布尔 |
| 委托审批人 / 多级授权 | 仍不做 | 与 2.2「明确不做」一致 |

---

## 4. 待办（本次未定 / 需 owner 或后续确认）

### 4.1 已关闭

#### 4.1.1 本轮回合已关闭（owner 2026-09-13 拍板）

| 原待办 | 结论 |
|---|---|
| 原有 11：两条路径留痕不对称 | **已解决**：`stances` 新增 `approved_at`（可空），与 `outputs.approved_at` 语义对齐（见 2.4.6、9.3、15.1） |
| 原有 1：`acting_as` 判定方 | **已定**：两条路径统一为「客户端声明 `acting_as` + 服务端闸门校验三项」，不做派生（见 2.4.3） |
| 原有 2：`authority` 是否收敛为枚举 | **已定**：收敛为 `propose_only` / `can_commit`，与 `agent_authority` 同源（见 2.4.3、15.1） |
| 原有 6：FR-12b / FR-12c 编号 | **已定**：保留后缀编号，FR-27 / FR-28 方案不再采纳（见 1.5） |
| 第二轮 2：路径① 是否需要 `authority` 字段 | **已解决**：`outputs` 新增 `authority` 枚举列（可空），两条路径字段完全对称（R7，见 2.4.3、2.4.6、9.2、15.1） |
| 第二轮 3：`stances.authority` 存量自由文本回填映射 | **已解决**：禁止启发式回填。新枚举列一律 `NULL`（语义=历史值不可识别），原值整体搬到只读列 `authority_legacy`，迁移写审计（R6，见 2.4.6、9.3） |
| 第二轮 10：存量 `approved_at` 为空的标注语义 | **已解决**：固定中性措辞「该记录早于本版本，无确认时间留痕」，且只在内部审计/运维视图；禁止任何暗示代理的措辞（R8，见 9.3） |
| 上一轮 8：`authority_legacy` 的保留期限与导出策略 | **已定**：不设自动过期 + 只读导出 + 标注「只读兼容列，未来大版本可评估下线」（R10，见 2.3、术语表） |
| 上一轮 10：迁移审计的落地方式 | **已定**：三层落地——主留痕为 `schema_migrations`；数据语义变更才进 `audit_events`（新增常量 `SCHEMA_MIGRATED`，不受错误码禁令约束）；写入前探测表存在性（迁移先于 `create_all`），不存在只写 logging（R9，见 9.3） |

#### 4.1.2 2026-09-14 / 09-16 关闭（自 §4.2 移入）

| 原 §4.2 # | 原问题 | 结论 | 关闭日 |
|---|---|---|---|
| 1（**前半**） | 4.3 增列句与开通一次性告知的**确切文案**是否定稿 | **已定**：采用交接稿 §8.2-S4 的现有草案措辞。§1.4 两处【待 owner 定稿】标记已摘除 | 2026-09-14 |
| 4（**前半**） | `irreversible` 变更时理由**是否必填** | **已定：必填** | 2026-09-14 |
| 5 | `agent_authority` 的开通/撤销是否限于事项未进入终态 | **已定：限非终态事项**（`completed` / `cancelled` 后不得开通或撤销） | 2026-09-14 |
| 6 | §1.1 一句话定位「提交经本人确认的纯文本输出」是否改写 | **已定：维持现状** | 2026-09-14 |
| 3 | 悬空引用修复方案选 A 还是 B | **已按 A 落地**：§15.1 已存在并被 §1.12 / 术语表引用，无「双事实源」风险。**选项 B 未被采用**；若日后评审翻案选 B，需回头删除 §15.1 —— 当前无遗留动作 | 2026-09-16 |
| 7 | 仓库内过期注释声称「本仓库没有迁移工具」 | **已收口**（2026-09-16 核）：本项原先引用的 `hub/db/models.py:197` 与 `hub/api/stances.py:154` **两处已被修正**；同一错误主张当时还残留在 `outputs/stance-layer-walkthrough.md:204` 与 `tests/api/test_stances_api.py:611`（本项未提及），**该两处已于 2026-09-16 一并改掉** | 2026-09-16 |

### 4.2 待确认

> 编号沿用原表以便与历史记录对照；原条目**前半已答、后半未答**者，前半关闭后**后半另立号（1b / 4b）留在本表**。

| # | 事项 | 为什么需要确认 |
|---|---|---|
| 1b | 开通 `can_commit` 的一次性告知，是否还需「**我已阅读**」勾选确认？（前半「文案措辞定稿」已于 2026-09-14 关闭，见 §4.1.2） | 09-14 答复 §8.4-1 只定措辞、未涉勾选；属对外承诺的交互确认方式，需 owner 与合规责任方定 |
| 2 | **仓库外「方案」文档的逐字比对**：所有渠道 0 命中，本次**未比对**。若后续拿到该方案（含其「第 2.4 节」），必须补做一次逐字比对并出结论，重点核对 `irreversible` 与 `agent_authority` 的语义边界。 | 见 0.3 未比对声明 |
| 4b | 撤销 `can_commit` 后，**历史代理提交是否需在 Web 上标注「授权已撤销」**？本稿只要求保留 `acting_as` 留痕，未要求撤销标注。（前半「理由必填」已于 2026-09-14 关闭，见 §4.1.2） | 09-14 答复 §8.4-2 只答了理由必填；Q10 / Q7 均未覆盖此问 |
| 8 | 内部审计/运维视图的具体承载页面（`/admin/audit` 还是新增面板），及历史值标注与 `SCHEMA_MIGRATED` 记录的出现位置。 | R8 只定措辞与「只在内部视图」，R9 只定三层留痕，均未定承载页面 |
