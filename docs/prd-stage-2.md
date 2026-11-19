# MTR-P Hub 阶段 2 增量 PRD：只读查询闭环

> 文档类型：简单增量 PRD
> 上游依据：`docs/MTR-P数据中台及中间件开发实施计划.md`（重点第 1、2、5、6 阶段 2、7、8、9、10、11 节）、`docs/prd-stage-0-1.md`、`docs/architecture-stage-0-1.md`、`docs/capability-matrix.md`
> 当前基线：阶段 0+1 的 119 项测试已通过并完成两轮 QA；本地 `main` 基线为 `188d3e2 feat: complete stage 0 and 1 routing baseline`。
> 本轮边界：真实 Obsidian 只读 + Core 生成不可变 ExecutionManifest + WorkBuddy 白名单只读执行乐享/GitHub；不是 Core Native 集成，不包含任何外部写入。

## 1. 项目信息

- Language：简体中文。
- Programming Language：Python 3.11，沿用现有 Pydantic、Typer、SQLite、Pytest 技术栈与指定 Python 3.11.11 虚拟环境。
- Project Name：`mtrp_hub`。
- 原始需求复述：在保持阶段 0+1 确定性路由、访问前拒绝、秘密零日志和 Fake 离线测试兼容的前提下，完成 T/R、M、P 的真实只读查询闭环，统一 Canonical Resource/Evidence、快照、相关度与全链追踪，并以 Fixture 和真实冒烟两类证据分别验收。
- 执行模式定义：
  - `fixture`：进程内 Fake 或临时 Fixture Vault；默认禁网，不读取真实 Vault，不代表真实连接成功。
  - `obsidian-local`：独立 Core 直接读取本地显式配置的 Vault 范围，只读。
  - `workbuddy-manifest`：Core 只生成/校验不可变 ExecutionManifest；已连接的 WorkBuddy 按白名单调用乐享/GitHub，再把受控执行结果交给 Core 规范化。Core 不继承连接器登录态。
  - `native`：阶段 5 才建设，本轮不可声明已接通或已实测。

## 2. 产品目标

1. **打通诚实的只读闭环**：按既有确定性路由获取 T/R/M/P 数据，返回可定位到来源与快照的 `Resource[]` 和 `Evidence`，空库、空仓与故障均不伪造结果。
2. **把越权与篡改挡在访问前**：Vault 仅触达授权范围，WorkBuddy 只能执行 hash 已校验的白名单只读 Manifest，任何库、仓库、分支、工具或参数变化都回到 Router 重建。
3. **形成可追溯、可区分的验收证据**：用 `request_id / route_id / execution_id / trace_id` 串联计划、调用和结果，同时明确真实实测与 Fixture 证据，保守更新能力状态。

## 3. 非目标与硬边界

- 不实现或宣称 Native Lexiang/GitHub Adapter、独立连接器登录态、FastAPI/MCP Hub；这些仍属阶段 5。
- 不创建、编辑或发布 M 条目，不调用乐享写工具，不依赖 Ask AI 或内置知识图谱。
- 不创建 GitHub 分支、提交、文件、PR，不 push/merge/rebase，不同步本地与远程分歧。
- 不读取本地 dirty 内容，不实现 LocalGitAdapter；GitHubAdapter 只能读取远程已提交快照。
- 不修改 Vault 标签、frontmatter、文件名或路径，不移动/删除文件，不自动分类，不触发 G2/G3，不做生命周期、审批、dry-run 标签提案或样本打标。
- 不扫描整个 Vault、个人目录或磁盘，不自动发现 Vault/Workspace/Repository，不接受任意 URL、任意命令或任意绝对路径作为数据源。
- 不实现全局语义重排、Graph/RDF、推理、多租户、Watcher、增量同步或自动重试改路。
- 产品运行时不执行任何 Git 写操作。工程流程仅在本轮验收通过后由主理人按规则做一次本地 Git commit，不自动 push；这不属于 Adapter 能力。

## 4. 用户故事

1. 作为知识库使用者，我希望按 intent 获得带来源、快照和相关度的结果，以便判断答案来自哪个库及哪个版本。
2. 作为 WorkBuddy 调用方，我希望只收到 Core 已生成且不可变的只读 Manifest，以便连接器不能自由改库、改仓库或追加工具。
3. 作为 Vault 所有者，我希望系统只读指定 managed roots 且排除敏感目录，以便个人笔记不会被全盘扫描、误归类或修改。
4. 作为测试人员，我希望分别取得 Fixture 自动化与真实只读冒烟证据，以便空结果、故障和真实连通性不会被相互冒充。
5. 作为审计人员，我希望能按全链 ID 重放每次决策和调用，同时日志不含正文、秘密或原始外部响应。

## 5. 需求池

优先级：P0 = 本轮必须完成；P1 = 应完成的可诊断性增强；P2 = 可选，不得挤占 P0/P1。

### 5.1 P0：本轮验收门槛

| 编号 | 需求 | 最小完成口径 |
|---|---|---|
| P0-01 | 基线兼容 | 既有 119 项测试及访问前拒绝、写请求拒绝、禁网 Fake、确定性 golden 全部保持；既有 `route query` v1 接口不因真实执行而被隐式改路或继承连接器状态。新增公共字段采用兼容的新响应 schema/执行命令。 |
| P0-02 | Obsidian 范围与读取 | 仅遍历配置的 `vault_root + managed_roots` 内 Markdown；排除项优先，拒绝路径逃逸和符号链接/重解析点逃逸；读取正文并按原始文件字节计算 SHA-256，绝不写文件。 |
| P0-03 | T/R 标签视图 | T 仅含 `mtr/trial`，R 仅含 `mtr/rarely`；frontmatter `tags` 与 Obsidian 行内标签按 Fixture 契约解析，代码块或普通文本提及不算标签。双标签冲突记 `MTR_TAG_CONFLICT` 并跳过；仅有 `status/stale` 不能归为 R。未带有效 MTR 标签一律 `unmanaged / needs_classification`，不默认 T。 |
| P0-04 | LexiangMAdapter | 通过 `workbuddy-manifest` 支持空间解析、普通搜索、向量搜索、条目内容解析和空结果；条目 ID、版本/内容 hash、score 仅能来自真实响应或明确 Fixture，不得编造。 |
| P0-05 | GitHubPAdapter | 通过 `workbuddy-manifest` 支持仓库、分支、ref→完整 commit SHA、按 SHA 读文件；空仓/未知快照的 commit 为 `null`，同名分支不同 SHA 不合并，本地 dirty 不可见。 |
| P0-06 | 不可变 Manifest | 从已通过 Guard/Registry/ReadPolicy 的计划生成；包含模式、路由/执行身份、只读步骤、固定参数、空结果策略和 `manifest_hash`。只允许声明式的前序步骤输出绑定，禁止执行者自由补参或改参。 |
| P0-07 | WorkBuddy 白名单执行 | 内置 `stage2-readonly-v1` 仅覆盖乐享空间解析/普通搜索/向量搜索/条目解析及 GitHub 仓库/分支/ref 解析/按 SHA 读文件的固定工具与参数 schema；先验 hash 再调用。写工具、任意 URL、任意命令、任意绝对路径、额外参数和未知工具均在首次外部调用前拒绝。 |
| P0-08 | 统一 Canonical 结果 | 聚合 `Resource[] + Evidence[] + StepResult[]`；Resource 携带 `source_ref`、可用时的 `snapshot_ref`、`score`、证据引用和 `trace_id`，Evidence 携带来源/快照/hash/`trace_id`，StepResult 携带执行身份和 `trace_id`；引用完整、排序稳定，空结果不能构造 Resource。 |
| P0-09 | 路由与降级闭环 | 完整支持既有五类 intent；重点通过 `project_context` P→M→T、`general_knowledge` M→T、`cold_detail` R→T 及规定降级，普通查询不隐式加入 R。 |
| P0-10 | 全链追踪与安全日志 | 每次计划、工具调用、步骤状态与最终结果均可由 `request_id / route_id / execution_id` 关联，响应带 `trace_id`；Decision Log 只存白名单 ID、hash、计数、状态、原因码和耗时。 |
| P0-11 | 受控 CLI 交接 | 提供 Fixture 离线执行、Manifest 生成/校验、WorkBuddy 执行结果规范化入口；真实 M/P 工具调用只能发生在 WorkBuddy 侧，CLI 不得以 HTTP/SDK/子进程绕过该边界。 |
| P0-12 | Fixture 自动化 | 覆盖真实响应契约、正文/hash、T/R/unmanaged、M 空库与条目、P 空仓/分支/SHA、错误、降级、Manifest 篡改和全链追踪；默认禁网且不触真实 Vault。 |
| P0-13 | 真实只读冒烟 | 按第 8 节取得真实 Vault、真实乐享和真实 GitHub 证据；真实 M 为空时验证空结果而非造数据。连接器不可用则记 `blocked/not-tested`，不能用 Fixture 替代真实完成证据。 |
| P0-14 | 安全回归与能力矩阵 | 证明外部写为零、Vault 内容前后不变、秘密/正文不落日志；能力矩阵分别更新 `obsidian-local` 和 `workbuddy-manifest`，Native 仍保持未实现/未实测。 |

### 5.2 统一结果与快照语义

| 字段/模型 | 本轮契约 |
|---|---|
| `Resource` | 至少含稳定 ID、类型、标题或安全显示名、`content_ref`、`source_ref`、`snapshot_ref`、`score`、`evidence_ids`、`trace_id`。正文若用于本次回答，只存在本次受控响应，不进入 Decision Log。 |
| `source_ref` | P 定位 owner/repo 与逻辑路径；M 定位 team/space/entry；T/R 定位 vault 与 Vault 相对路径。不得用任意 URL 代替逻辑坐标。 |
| `snapshot_ref` | P 必须为已解析的完整 commit SHA；T/R 为文件原始字节的 `content_sha256`；M 仅在确有 entry version、revision 或内容 hash 时填写，否则为 `null`。 |
| `score` | 统一为 `0..1` 数字或 `null`，并记录来源类型（普通搜索/向量搜索/未提供）。连接器未给分、精确按路径读取或无法比较时为 `null`，不得伪造 0/1；本轮不跨 M/P/T/R 做分数融合。 |
| `Evidence` | 至少含来源、可用快照、P 的 repo/ref/commit/path 或 M 的 entry 定位或 T/R 的相对路径、内容 hash、step/route/trace 关联。缺少真实快照时保持未知。 |
| `StepResult` | 区分 `OK / EMPTY / DEGRADED / REJECTED`，含资源计数、原因码和下一步；`EMPTY` 不等于错误，`DEGRADED` 不等于真实连接成功。 |
| `trace_id` | 由请求、路由和执行身份确定性派生；同一执行内 Resource、Evidence、StepResult 一致。ID 不使用随机数或当前时间，避免破坏计划确定性。 |

ID 默认由完整 hash 派生：`request_id ← request_hash`、`route_id ← plan_hash`、`execution_id ← manifest_hash`（Fixture 执行由对应执行内容 hash 派生）、`trace_id ← 三者组合 hash`。日志可用自增序号区分同一 Manifest 的多次执行，但序号和时间不得进入确定性 Plan/Manifest。

### 5.3 路由验收口径

- `project_context`：P 权威默认坐标 → M → T；P 空/不可用继续 M，**M 空库是成功空步骤并继续 T**，不得报错或伪造命中。
- `general_knowledge`：M → T；不得默认枚举 P 的仓库或全部分支。
- `cold_detail`：保持 v1 的 R→T 计划，首个 R 步骤只查 `mtr/rarely`，R 空或按策略不可用时回 T；不得查 P/M，不得把 unmanaged 当 R/T。若未来要求“R 命中即停止”，必须升级路由规则版本，不在本轮暗改 v1。
- P 文件结果必须同时定位 `repo + ref + commit + path`，不能只返回 GitHub URL；`repo+ref` 不是快照。
- 相同 Fixture、配置和规则仍须三次字节确定；真实远端数据可变化，因此真实验收比较相同快照下的规范化结果与引用完整性，不承诺远端永远不变。

### 5.4 P1：体验与诊断

| 编号 | 需求 | 完成口径 |
|---|---|---|
| P1-01 | 追踪查询 | 决策日志只读入口支持按 request/route/execution/trace ID 查询，不返回正文或原始响应。 |
| P1-02 | 人类可读摘要 | 展示实际执行顺序、空结果、降级、命中计数和模式标识；明确 `fixture` 或 `workbuddy-manifest`。 |
| P1-03 | M 搜索诊断 | 分别记录普通/向量搜索的计数、耗时、score 可得性和空结果，不做跨分数量纲比较。 |
| P1-04 | Vault 扫描摘要 | 仅输出各 managed root 的扫描/命中/跳过/错误计数与聚合 hash；真实相对路径仅在明确授权的冒烟证据中出现。 |
| P1-05 | Manifest 差异检查 | 只比较两个 Fixture Manifest 的规范化字段和 hash，不执行其中工具。 |

### 5.5 P2：可选增强

- CLI shell 补全。
- Fixture Resource/Evidence 的静态文本图。
- 对已脱敏 Fixture 的搜索结果对比报告。

## 6. CLI 与 ExecutionManifest 契约

### 6.1 通用约束

- 原 `mtrp route query` 继续承担确定性 Fake 查询，默认不访问真实 Vault/网络；既有参数、0/1/2 退出码和 v1 golden 保持兼容。
- 新增阶段 2 命令至少覆盖：`manifest create`（Core 生成）、`manifest verify`（只校验，不执行）、`query finalize`（校验 WorkBuddy 执行信封并规范化为 Canonical Result）。最终命名可保持现有 CLI 风格，但不得出现 Native 直连或写命令。
- Manifest/执行信封优先经 stdout/stdin 在受控进程间传递。Fixture 文件仅允许既有 cwd 相对规则；真实原始响应不得写入 Git、Decision Log 或默认持久化文件。
- `manifest verify` 成功只表示结构/hash/权限有效，不表示外部连接成功；只有真实回执与规范化结果才能记为真实执行证据。
- 退出码沿用：`0` = 查询完成（包括空结果和计划内降级）；`2` = 输入、配置、Manifest 或访问边界拒绝；`1` = 未预期内部错误或审计写入失败。真实冒烟即使查询按策略降级退出 0，也不能据此判该真实连接项验收通过。

### 6.2 Fixture 查询保持离线

```bash
mtrp route query \
  --config-dir config \
  --query "MTR-P 当前如何实现路由？" \
  --intent project_context \
  --project mtrp \
  --repo mtrp-principles \
  --fixture tests/fixtures/project-context.json \
  --format json
```

此命令仍只执行 Fake；新增阶段 2 Fixture 端到端入口可输出 v2 执行信封，但必须显式标识 `data_mode=fixture`，不得把 Fixture 的 `execution_id` 当成 WorkBuddy 调用证据。

### 6.3 Core 生成 Manifest

```bash
mtrp manifest create \
  --config-dir config \
  --query "MTR-P 当前如何实现路由？" \
  --intent project_context \
  --project mtrp \
  --repo mtrp-principles \
  --format json
```

示例仅表达业务契约，字段可扩展但安全语义不得减弱；示例 ID/hash 均为占位符，不是真实执行证据：

```json
{
  "schema_version": "1",
  "mode": "workbuddy-manifest",
  "read_only": true,
  "request_id": "req_<request_hash>",
  "route_id": "route_<plan_hash>",
  "execution_id": "exe_<manifest_hash>",
  "plan_hash": "<sha256>",
  "manifest_hash": "<sha256>",
  "tool_profile": "stage2-readonly-v1",
  "steps": [
    {
      "step_id": "p-resolve-ref",
      "route_step_id": "s01-p",
      "adapter": "github-p",
      "operation": "resolve_ref",
      "arguments": {"owner": "13Asix21", "repo": "MTR-P", "ref": "main"},
      "on_empty": "continue"
    },
    {
      "step_id": "p-read-file",
      "route_step_id": "s01-p",
      "adapter": "github-p",
      "operation": "get_file_contents",
      "arguments": {
        "owner": "13Asix21",
        "repo": "MTR-P",
        "path": "README.md",
        "sha": {"from_step": "p-resolve-ref", "field": "commit"}
      }
    },
    {
      "step_id": "m-search",
      "route_step_id": "s02-m",
      "adapter": "lexiang-m",
      "operation": "search_kb_embedding_search",
      "arguments": {"space_id": "<configured-space-id>", "keyword": "MTR-P 当前如何实现路由？", "limit": 5},
      "on_empty": "continue"
    }
  ]
}
```

约束：

1. Manifest 的逻辑坐标只能来自 Registry；owner/repo/space 等实际执行参数由 Core 从受信配置物化，请求不能注入。示例中的 owner/repo/space_id 均为配置解析结果，不是执行者现场选择。
2. `manifest_hash = SHA-256(canonical_manifest)`，其中 canonical_manifest 仅排除两个派生身份字段 `execution_id` 与 `manifest_hash`，其余字段（包括 `request_id / route_id`、逻辑坐标、步骤、工具操作和参数）全部参与；随后固定 `execution_id = "exe_" + manifest_hash`。执行前必须重算并同时等值校验收到的 `manifest_hash` 与派生的 `execution_id`，再逐字段校验其余内容；篡改任一字段均须失败，不能只信任附带的 ID 或 hash 字符串。
3. `sha` 等动态值只能来自 Manifest 预先声明的前序只读步骤字段；解析失败、类型不符或得到空值即失败，不能改为读取分支最新文件。M 条目解析同理，若使用前序 entry ID，必须有固定绑定和数量上限。
4. 执行者不得更改库、仓库、分支、ref、路径、工具、操作、limit、threshold 或任何其他参数；需要变化时废弃当前 Manifest，回 Router 生成新的 route/execution/hash。
5. T/R 由 Core 的 ObsidianAdapter 在授权范围内执行，并与相同 request/route/execution 身份关联；WorkBuddy 不接收 Vault 绝对路径，不代替 Core 决定 T/R 范围。

### 6.4 WorkBuddy 执行与 Core 规范化输出

WorkBuddy 只执行上述白名单步骤并返回含原 Manifest hash、逐步骤回执和安全引用的执行信封。Core 的 `query finalize` 重新验证身份、hash、步骤完整性和响应契约后，输出如下逻辑结构：

```json
{
  "schema_version": "2",
  "data_mode": "workbuddy-manifest",
  "request_id": "req_<request_hash>",
  "route_id": "route_<plan_hash>",
  "execution_id": "exe_<manifest_hash>",
  "trace_id": "trace_<combined_hash>",
  "manifest_hash": "<sha256>",
  "status": "OK",
  "step_results": [
    {"step_id": "s01-p", "status": "OK", "resource_count": 1, "trace_id": "trace_<combined_hash>"},
    {"step_id": "s02-m", "status": "EMPTY", "reason_code": "M_EMPTY", "next_step_id": "s03-t", "trace_id": "trace_<combined_hash>"},
    {"step_id": "s03-t", "status": "EMPTY", "reason_code": "T_EMPTY", "trace_id": "trace_<combined_hash>"}
  ],
  "resources": [
    {
      "resource_id": "res_<content_derived>",
      "resource_type": "project_file",
      "title": "README.md",
      "content_ref": "ref_<opaque>",
      "source_ref": {"store": "P", "store_id": "p", "logical_key": "mtrp-principles:README.md"},
      "snapshot_ref": {"kind": "git_commit", "value": "c3a6c7b83ced99a01b2b51039e87a40ee29df6a5"},
      "score": null,
      "evidence_ids": ["ev_<content_derived>"],
      "trace_id": "trace_<combined_hash>"
    }
  ],
  "evidence": [
    {
      "evidence_id": "ev_<content_derived>",
      "source_ref": {"store": "P", "store_id": "p", "logical_key": "mtrp-principles:README.md"},
      "snapshot_ref": {"kind": "git_commit", "value": "c3a6c7b83ced99a01b2b51039e87a40ee29df6a5"},
      "project_coordinate": {
        "project_id": "mtrp",
        "repo_id": "mtrp-principles",
        "location_id": "github-main",
        "ref": "main",
        "commit": "c3a6c7b83ced99a01b2b51039e87a40ee29df6a5",
        "path": "README.md"
      },
      "content_sha256": "<sha256>",
      "trace_id": "trace_<combined_hash>"
    }
  ]
}
```

真实响应中的正文、token、Cookie、认证头及未脱敏错误不得因序列化整个响应而进入 Decision Log。默认 CLI 摘要只展示白名单字段；如后续增加正文输出，必须显式选择且仍禁止落日志，不属于本轮 P0。

## 7. 配置新增项

真实值只进 gitignored 本地配置；示例配置只用占位符，不保存连接器凭据。逻辑结构如下：

```yaml
obsidian:
  vault_id: default
  vault_root: <local-vault-root>
  managed_roots:
    - {root_id: permanent, path: 2-Permanent}
  excluded_roots:
    - 2-Permanent/Resources/MTR-P-System
    - 2-Permanent/Areas/AI-Assets/_templates
  trial_tag: mtr/trial
  rarely_tag: mtr/rarely
  read_only: true
  follow_symlinks: false
  max_file_bytes: <positive-safe-limit>

lexiang_m:
  mode: workbuddy-manifest
  team_id: <configured-team-id>
  space_id: <configured-space-id>
  root_entry_id: <configured-root-entry-id>
  read_only: true
  allow_empty: true
  search_modes: [text, vector]
  max_results: 5
  timeout_seconds: <bounded-value>

github_p:
  mode: workbuddy-manifest
  read_only: true
  timeout_seconds: <bounded-value>
  max_file_bytes: <positive-safe-limit>

workbuddy_manifest:
  schema_version: "1"
  tool_profile: stage2-readonly-v1
  max_steps: <bounded-value>
  max_entry_reads: <bounded-value>
  persist_raw_responses: false
```

配置规则：

- G1 已于 2026-11-19 明确确认：阶段 2 可在真实 Vault 的 `2-Permanent` 范围内进行只读扫描，排除 `2-Permanent/Resources/MTR-P-System` 与 `2-Permanent/Areas/AI-Assets/_templates`；允许只读正文并计算 hash，不修改文件、不补 MTR 标签。排除项始终优先于包含项，本确认不授权扩大到其他目录。
- T/R 共用同一 Vault，但逻辑标签不同；配置不能把 unmanaged 设为 T，也不能把 `status/stale` 映射为 R。
- `stage2-readonly-v1` 是代码内置封闭白名单，本地配置不能扩张工具、写权限、URL、命令或路径类型。
- 乐享/GitHub 认证留在 WorkBuddy 连接器中；本轮 Core 配置不新增 token，也不尝试导出登录态。
- P 的仓库、权威 Location、默认 branch、允许的 path 仍由 `projects.yaml` 登记。`188d3e2` 只是本地基线，不证明远端 `MTR-P-hub` 已有该快照；真实 P 冒烟应选已登记且实际非空的远程仓库。
- 既有 `routing.yaml` v1 语义不变；仅执行方式变化不得暗中改变查库顺序或 R 规则。

## 8. Fixture / 真实验收矩阵

### 8.1 证据分级

- **Fixture 自动化证据**：临时目录、固定 JSON/Markdown、工具替身和 I/O spy；适合覆盖攻击、故障、非空 M 条目和版本组合。不得写成“真实连接器已验证”。
- **真实只读冒烟证据**：实际 Vault/WorkBuddy 连接器在明确授权范围执行；报告只保留脱敏命令模板、模式、ID/hash、状态、计数、耗时和必要 P 坐标，不保留原始 query、正文或原始响应。
- **真实为空也是有效证据**：M 空库、R/T 无命中可验收“连接 + 权限 + 空结果 + 降级”；但不能验收“非空条目解析/正文命中”，后者只能标为 Fixture 已验证或真实未测。

### 8.2 必测矩阵

| 能力/用例 | Fixture 自动化 | 真实只读冒烟 | 通过证据与限制 |
|---|---|---|---|
| 阶段 0+1 回归 | **必须**：119 项原测试及新增回归全过 | 不需要 | 禁网、无真实 Vault/凭据可运行。 |
| Vault managed roots、排除、路径逃逸 | **必须**：临时 Fixture Vault 覆盖包含/排除、中文路径、无 frontmatter、异常 YAML、多/行内标签、只读文件、链接逃逸 | **必须**：按 2026-11-19 已确认的 G1 范围做只读冒烟 | Fixture 证明精确过滤和 I/O spy 未触禁区；真实报告给出扫描/命中/跳过计数、内容聚合 hash 前后不变。不得为测试创建标签或样本。 |
| T/R 正文读取与内容 hash | **必须**：T/R/unmanaged/双标签、正文与原始字节 hash golden | **必须验证真实范围可查询**；只有真实已存在合规标签笔记时才验正文命中 | 真实无 T/R 标签时 `EMPTY` 合法；不能拿 Fixture 正文宣称真实命中。 |
| M 空间解析、普通/向量搜索、空库 | **必须**：正常、空、异常响应、权限/超时 | **必须**：WorkBuddy 对配置空间分别执行普通和向量只读搜索 | 真实空结果均为成功；记录空间逻辑 ID、步骤状态和计数，不记录原始内容。 |
| M 非空条目解析、版本/snapshot/score | **必须**：完整、缺字段、无 score、无版本、畸形条目 | **条件实测**：仅当授权空间已有可读条目；不得创建数据 | 当前空库时只允许以 Fixture 验收非空解析，能力矩阵注明真实非空未测。 |
| P 仓库、分支、ref→SHA、按 SHA 读文件 | **必须**：正常、空仓、分支不存在、无效 SHA、读取失败 | **必须**：选已登记非空远程仓库完成一次完整链路 | 真实证据含 repo/ref/commit/path；不得以本地 `188d3e2` 冒充远端存在。 |
| P 同名分支不同 SHA、本地 dirty | **必须**：A/B SHA 不合并；本地 dirty 与远程快照隔离 | 不构造或读取 dirty 作为验收手段 | 空仓 commit=null；GitHubAdapter 无本地文件/Git 调用入口。 |
| Manifest hash、白名单和参数绑定 | **必须**：改库/仓库/ref/工具/参数/hash/execution_id、写工具、URL/命令/绝对路径均零调用拒绝 | **必须**：真实执行前由 WorkBuddy 校验同一 Manifest；攻击用例只在 Fixture | 真实成功回执绑定原 hash；不允许执行后补写或替换 Manifest。 |
| `project_context` P→M→T，M 空继续 T | **必须**：覆盖 P/M/T 各自空与不可用 | **必须**：在已配置环境完成一次真实只读闭环 | 真实 M 空时步骤顺序和 T 继续清晰可见，Resource 不伪造。 |
| `general_knowledge` M→T | **必须**：M 空/命中、T 回退、无 P 调用 | **必须**：真实 M 搜索 + 真实 T 查询 | spy/步骤日志证明没有 P 全分支枚举。 |
| `cold_detail` R→T | **必须**：R 命中、R 空回 T、R 故障降级、无 P/M/unmanaged | **必须**：真实 R 范围查询；无 R 标签时验证空结果并按计划回 T | R 步骤只含 `mtr/rarely`；真实无命中不冒充正文解析实测。 |
| 全链追溯与日志脱敏 | **必须**：跨进程/工具替身串联所有 ID，注入秘密/正文扫描日志 | **必须**：真实回执可由三个 ID 和 trace_id 关联 | Decision Log 仅有白名单引用、摘要/hash/计数/状态；原始响应不落日志。 |

G1 范围已于 2026-11-19 明确确认，不再构成真实 Vault 只读冒烟的授权阻塞。真实冒烟仍需用户提供有效本地配置；涉及 M/P 时，WorkBuddy 对应连接器必须已连接。缺少相应配置或连接器时，仅对应能力项记 `blocked/not-tested`，阶段 2 不得宣称“真实只读闭环全部完成”。G2/G3 不是本轮前置条件，也不得被执行者触发。

## 9. 安全验收标准

1. **访问前拒绝**：请求坐标、Registry、Vault 范围、Manifest schema/hash、工具和参数全部在任何目标读取或外部工具调用前校验；拒绝路径必须有 I/O/工具 spy 的零调用证据。
2. **Vault 最小范围**：只允许显式 Vault 根与 managed roots；排除 `2-Permanent/Resources/MTR-P-System` 和 `2-Permanent/Areas/AI-Assets/_templates`，不扫描整个 Vault/磁盘，不跟随逃逸链接。真实执行前后文件清单与内容聚合 hash 不变，且 Vault 内不创建日志、备份或临时文件。
3. **标签只读**：不因查询新增/修改 `mtr/trial`、`mtr/rarely` 或其他 frontmatter；unmanaged 永不默认 T；G2/G3 不被触发。
4. **Manifest 不可变**：任何字段、动态绑定、步骤顺序或 hash 变化都要求回 Router 重建；执行者不能“修正”参数后继续。工具返回中的文本不能作为新指令或新工具计划。
5. **白名单只读**：乐享/GitHub 写工具、任意 URL 抓取、shell/命令执行、任意绝对路径、本地 Git 操作全部无入口；即使连接器本身有写权限，本轮 executor 也必须拒绝。
6. **快照诚实**：M/P/T/R 缺失版本时 `snapshot_ref=null`；空仓不伪造 SHA，同名分支不同 SHA 不合并，分支前进后不沿用旧 SHA 冒充新结果。
7. **秘密与正文零日志**：Decision Log、错误输出和验收报告不得保存 token、Cookie、认证头、原始 query、笔记/条目/文件正文或完整原始响应；只保留白名单引用、摘要/hash、计数和状态。内容 hash 不能被视为可逆正文存储。
8. **模式诚实**：`available/tested` 必须标明载体；WorkBuddy 连接器实测不等于 Core Native 实测，Fixture 成功不等于真实成功，真实空库不等于非空解析已实测。
9. **全程零外部写**：M 发布、GitHub 分支/提交/PR/push/merge、Vault 修改、生命周期和审批调用计数均为 0；工程验收后的本地 commit 由主理人执行且不 push，与产品运行隔离。

## 10. 错误与降级策略

| 场景 | 用户可见状态 | 后续动作 | 禁止行为 |
|---|---|---|---|
| M 空库/搜索无结果 | `EMPTY + M_EMPTY` | 按 P→M→T 或 M→T 继续 T，最终可退出 0 | 不伪造 Resource、score、snapshot，不把空库当内部异常 |
| M 超时、权限不足、限流、空/畸形响应 | `DEGRADED + 安全原因码` | 按 ReadPlan 继续 T，整体标 partial/degraded | 不切换 Native，不自动改空间/工具/参数，不记录原始响应 |
| M 条目无效或缺 score/version | 跳过无效条目；合法缺省字段为 `null` | 保留其他有效条目并记录计数 | 不补造版本、hash、分数或正文 |
| P 空仓 | `EMPTY + EMPTY_REPOSITORY`，commit=null | 继续 M | 不创建 README/SHA，不把本地 commit 当远程快照 |
| P 分支不存在/ref 解析失败 | `DEGRADED + BRANCH_NOT_FOUND/REF_UNRESOLVED` | 继续 M | 不改用其他分支，不伪造 commit |
| P 按 SHA 读取失败 | `DEGRADED + FILE_READ_FAILED` | 继续 M，保留已解析坐标但不生成文件 Resource | 不回退读取“最新分支文件”冒充同一步骤 |
| Vault 无有效 MTR 标签（含无 frontmatter 且无有效行内标签） | `unmanaged`，计入跳过数 | 继续扫描授权范围 | 不默认 T/R，不写标签 |
| Vault 同时含 T/R 标签 | `MTR_TAG_CONFLICT`，计入跳过数 | 不进入任一视图，继续其他文件 | 不自行选择 T 或 R，不改标签 |
| Vault 单文件 YAML 异常或不可读 | 文件级 `DEGRADED`，安全计数 | 跳过该文件并保留其他合法结果 | 不落原文/异常全文，不扩大扫描范围 |
| Vault 路径/链接逃逸 | `REJECTED + PATH_ESCAPE` | 目标读取前阻止；请求坐标非法时退出 2 | 不 stat/open 逃逸目标，不转为 unmanaged 后读取 |
| R 空或按策略不可用 | `R_EMPTY` 或安全降级原因 | 按 cold_detail 计划回 T | 不查 P/M，不读 unmanaged |
| Manifest hash/工具/参数被改 | `REJECTED + MANIFEST_INTEGRITY_FAILED/TOOL_FORBIDDEN` | 首次外部调用前退出 2；需变更则重新路由 | 不自动修补、重签或继续执行 |
| WorkBuddy 连接器不可用 | M/P 对应步骤 `DEGRADED`，真实冒烟项 `blocked/not-tested` | 可按计划走 Core T/R 降级；保留安全诊断 | 不声明真实 M/P 成功，不尝试导出登录态 |
| Decision Log 写入失败 | `DECISION_LOG_WRITE_FAILED` | 退出 1，不宣告完整成功 | 不把无审计执行写成可重放成功 |

计划内空结果/降级可使查询退出 0，但整体状态必须如实标为 `EMPTY` 或 `DEGRADED`；真实能力验收另按第 8 节判定，不能仅看进程退出码。

## 11. 待确认问题与非阻塞默认

**已确认、不再待确认：**G1 已于 2026-11-19 明确授权阶段 2 在真实 Vault 的 `2-Permanent` 范围内只读扫描，排除 `2-Permanent/Resources/MTR-P-System` 与 `2-Permanent/Areas/AI-Assets/_templates`；可只读正文并计算 hash，不修改文件、不补 MTR 标签。该项不再是阻塞，也不扩大其他授权范围。

| 待确认项 | 本轮安全默认 |
|---|---|
| 真实 P 冒烟使用哪个非空仓库和文件 | 默认从已登记的 `13Asix21/MTR-P` 中选择配置明确的 `main + README.md`，执行时以真实 registry/连接器结果为准；不得假定远端 `MTR-P-hub` 已有本地基线。 |
| M 当前是否已有可供真实条目解析的数据 | 以真实搜索为准；为空则非空条目解析仅 Fixture 验收，不为测试发布 M。 |
| 乐享普通/向量 score 与 entry version 的实际字段和量纲 | 先做只读契约探测；有合法值才映射，否则 `score/snapshot_ref=null`，不自行归一化或编造。 |
| WorkBuddy 各逻辑 operation 对应的具体工具 ID 与响应版本 | 由架构阶段固定进封闭白名单和契约 Fixture；未固定的工具不执行，不由 Agent 现场选择替代品。 |
| 真实执行结果是否需要正文预览 | P0 默认只返回引用/hash/计数用于验收；正文使用限本次受控响应，不做默认持久化或日志展示。 |

## 12. 阶段 2 完成定义

只有同时满足以下条件，阶段 2 才算完成：

1. P0 需求全部实现，既有 119 项测试与新增单元、契约、集成、安全测试通过，独立 QA 验收通过；禁网 Fixture 全套不依赖真实 Vault、连接器或凭据。
2. 临时 Fixture Vault 证明 managed roots、排除、T/R/unmanaged、正文读取与 hash 正确；真实 Vault 冒烟证明授权范围只读且内容未变化。真实为空可证明空结果，不冒充非空命中。
3. WorkBuddy 真实乐享空间解析、普通/向量搜索和空结果通过；真实 GitHub 非空仓库完成仓库/分支/ref→SHA/按 SHA 读文件并返回 repo/ref/commit/path。M 非空解析若仍无真实数据，明确保留为 Fixture-tested、real-not-tested。
4. 三类关键路由验收完成：`project_context` P→M→T 且 M 空继续 T、`general_knowledge` M→T 且无 P 枚举、`cold_detail` R 只读 `mtr/rarely` 并可回 T；全链 ID 与 trace_id 可重放解释。
5. Manifest 篡改、写工具、URL/命令/绝对路径、路径逃逸均在访问前零调用拒绝；M 空库不造命中，P 空仓不造 SHA，同名分支不同 SHA 不合并，本地 dirty 不被 GitHubAdapter 读取。
6. Decision Log、错误与验收证据不含秘密、正文或原始外部响应；外部写计数为零，G2/G3 未触发。
7. 能力矩阵和真实/Fixture 证据明确区分 `obsidian-local`、`workbuddy-manifest` 与 `native`；Native、FastAPI/MCP、生命周期和写能力仍保持后置/禁用，不夸大完成状态。
8. 上述验收通过后，由主理人按工程规则创建阶段 2 本地 Git commit 并记录基线；不自动 push，也不以该 commit 反推远程 P 资产已可读。
