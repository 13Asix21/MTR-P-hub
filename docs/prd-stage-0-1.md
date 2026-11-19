# MTR-P Hub 阶段 0 + 阶段 1 增量 PRD

> 文档类型：简单 PRD  
> 实施仓：`13Asix21/MTR-P-hub`  
> 本地工作区：`E:\ASoftwareManage\Agithub\MTR-P-hub`  
> 上游依据：`docs/MTR-P数据中台及中间件开发实施计划.md`  
> 本轮范围：仅从空仓建立安全实施基线、确定性路由与 Fake 验证能力；不进入真实外部系统写入。

## 1. 项目信息

- Language：简体中文。
- Programming Language：目标版本为 Python 3.11；沿用实施计划中的 Pydantic、Typer、Pytest 等选型。
- 实测解释器：系统 PATH 默认 `python` 为 Python 3.13.14，不是本轮目标版本；`D:/DevelopSoftwares/miniconda/python.exe` 实测为 Python 3.11.11，本轮以其作为目标解释器，不要求更换系统默认 Python。
- Project Name：`mtrp_hub`。
- 原始需求复述：按照实施计划开始开发，本轮完成阶段 0 与阶段 1；四个人工确认门未确认前，仅建设 Router、Registry、Fake Adapters 和只读能力，不修改真实 Obsidian Vault，不调用乐享/GitHub 写工具，不自动扫描磁盘，不自动 commit/push。
- 当前前提：仓库尚无提交，除实施计划外基本为空；`origin` 为 `git@github-mtrp:13Asix21/MTR-P-hub.git`，远端 `MTR-P-hub` 仍为空，不能作为已有可读 P 资产。

## 2. 产品目标

1. **建立可复现的安全基线**：从空仓得到可安装、可配置、可测试的 Python 项目，配置与日志规则不泄密，能力状态不夸大。
2. **提供确定且可解释的路由**：通过 Canonical Model、Registry 和版本化 Read Policy，让相同请求生成相同 ReadPlan，并准确定位权威仓库坐标。
3. **用 Fake 验证边界**：不依赖真实外部系统，覆盖 M 空库、P 空仓、同名分支不同 SHA、未登记本地路径等关键场景，全程关闭外部写能力。

## 3. 非目标与阶段边界

### 3.1 本轮明确不做

- 不实现真实 Obsidian、乐享、GitHub、LocalGit Adapter，不执行真实外部查询闭环。
- 不修改、移动、删除、重命名真实 Vault 文件，不批量或自动打标签。
- 不创建乐享条目，不发布 M，不创建 GitHub 分支、提交、推送、PR 或合并。
- 不自动扫描磁盘或 Vault，不自动发现、登记本地 Workspace。
- 不实现生命周期阈值计算、审批执行、真实写入、冲突恢复或增量监听。
- 不实现 FastAPI/MCP Hub、独立 Native 凭据、Graph/RDF、推理、多租户。
- 不自动 commit/push 本仓库；本轮产物存在不代表首个提交已获授权。

### 3.2 本轮模型与后续能力的区别

- 本轮实现 `WriteProposal` 的**数据契约与校验**，不提供生成真实变更、审批或执行入口。
- 本轮 `ReadPlan` 只描述确定性路由及 Fake 结果，不代表已经连接真实 M/P/T/R。
- 阶段 0 只冻结只读冒烟测试**契约**并使用 Fixture 验证；真实 Vault/乐享/GitHub 连通性执行不列为本轮完成条件，默认留到阶段 2 或另行明确授权。
- 阶段 2 才实现真实只读 Adapter 与 WorkBuddyManifestExecutor；阶段 3 才做 T/R dry-run 与受控标签更新；阶段 4 才做受控回写与多分支 P；阶段 5/6 按实施计划后置。

## 4. 用户故事

1. 作为 Core 开发者，我希望从空目录获得统一包结构、示例配置和测试入口，以便在无真实凭据时重复搭建与验证。
2. 作为 WorkBuddy 调用方，我希望输入 intent 和项目坐标后得到稳定、带规则版本的 ReadPlan，以便后续执行者不能自由改变查库顺序。
3. 作为项目负责人，我希望每个 Repository 只有明确的权威 Location，并区分远程快照和本地 Workspace，以便不会把同名分支或本地 dirty 内容误认为同一版本。
4. 作为知识库使用者，我希望 M 空库或 P 空仓时得到明确状态并继续后续只读步骤，以便空数据不会被误报为失败或伪造命中。
5. 作为安全负责人，我希望未登记路径、任意数据源 URL 和一切写请求被拒绝，以便系统不会越权访问个人文件或外部系统。
6. 作为测试人员，我希望用固定 Fixture 重放边界场景并查看 Decision Log，以便定位路由选择、空结果与降级原因。

## 5. 需求池

优先级：P0 = 本轮必须完成；P1 = 本轮应完成，不阻塞核心演示时可后续补齐；P2 = 可选增强，不得挤占 P0/P1。

### 5.1 P0：本轮验收门槛

| 编号 | 阶段 | 需求 | 最小完成口径 |
|---|---|---|---|
| P0-01 | 0 | Python 包与测试基线 | Python 3.11 项目可安装，CLI 与 Pytest 有统一入口；不依赖真实凭据即可运行本轮测试。 |
| P0-02 | 0 | 配置示例与本地覆盖 | 提供 `stores.example.yaml`、`projects.example.yaml`、`routing.yaml` 和环境变量模板；示例只用占位符，真实本地配置不进入 Git。 |
| P0-03 | 0 | 日志、密钥与忽略规则 | `.gitignore` 覆盖密钥文件、本地配置、日志、数据库、缓存和临时产物；日志默认脱敏，不记录令牌、Cookie、凭据或笔记正文。 |
| P0-04 | 0 | 能力矩阵 | `docs/capability-matrix.md` 使用 `tested / available / not-tested / disabled` 标注事实与边界，保留 2026-11-18 实测口径，不把 Agent 工具能力写成 Core SDK 能力。 |
| P0-05 | 0 | 只读冒烟测试契约 | 固化 Vault 可解析、乐享空间可解析且空结果成功、GitHub README 可按 SHA 读取三类契约；本轮用 Fixture 验证，不发生真实写入。 |
| P0-06 | 1 | Canonical Model | 实现 Resource、Relation、Evidence、ProjectCoordinate、ReadPlan、WriteProposal 的必填校验与序列化；能表达来源、快照、证据、路由和提案状态，非法/悬空坐标被拒绝。 |
| P0-07 | 1 | Store 与 Project Registry | 实现 StoreRegistry、ProjectRegistry、Repository/Location/Workspace；每仓库恰有一个 `authoritative_location_id`，本地 `workspaces` 默认空，未登记项不可由请求注入。 |
| P0-08 | 1 | 默认坐标解析 | 项目与仓库可解析到权威 Location 和默认分支；快照未知时保持未知，不为 P 空仓伪造 SHA；本地路径仅在明确登记后可作为 Workspace。 |
| P0-09 | 1 | 版本化确定性 Read Policy | 规则含 `rule_version`；支持五类 intent、固定查库顺序、空结果降级与 R 开关；LLM 不参与主路由，同输入同配置输出完全一致。 |
| P0-10 | 1 | Fake Adapters 与指定 Fixture | 覆盖 M 空库、P 空仓、同名分支不同 SHA、未登记本地路径；默认不访问网络、真实 Vault 或未登记磁盘路径。 |
| P0-11 | 1 | Decision Log | 记录请求摘要/哈希、规则版本、解析坐标、计划步骤、Fake 结果、空结果与降级原因、拒绝原因；不得记录秘密或伪造执行成功。 |
| P0-12 | 1 | `mtrp route query` CLI | 支持结构化输入、JSON 输出和清晰错误码；只生成 ReadPlan/展示 Fake 结果，无写命令、无自动外部调用。 |
| P0-13 | 0+1 | 自动化验收 | 单元、契约与安全回归测试全部通过；断开网络且不使用真实 Vault 时仍可完整运行。 |

### 5.2 Canonical Model 最小业务语义

| 模型 | 必须表达的内容 |
|---|---|
| Resource | 稳定标识、类型、标题/内容引用、来源库、`source_ref`、可用时的 `snapshot_ref`；空结果不能构造伪 Resource。 |
| Relation | 关系类型、两端 Resource 标识及证据引用；不接受不存在的必要端点。 |
| Evidence | 来源定位、快照/版本、路径或条目定位、内容哈希/摘要及决策追踪信息。 |
| ProjectCoordinate | project、repository、location、默认或指定 ref、可用时的 commit、path、可选 Workspace；本地与远程身份不可混用。 |
| ReadPlan | 规范化 intent、规则版本、有序只读步骤、坐标、R 开关、空结果/降级策略；不含随机值和当前时间。 |
| WriteProposal | 目标坐标、建议操作、理由、基准快照与提案状态；本轮恒为不可执行，不提供真实写入入口。 |

### 5.3 首版路由契约

| intent | 必须生成的顺序 | 空结果与降级 | R 规则 |
|---|---|---|---|
| `project_context` | P 权威默认分支 → M → T | P 空/不可达则继续 M；M 空则继续 T | 仅显式启用 |
| `general_knowledge` | M → T | M 空则继续 T | 仅显式启用 |
| `method_skill` | M → T | M 空则继续 T | 仅显式启用 |
| `decision_history` | P(ADR) → T → M | ADR 缺失只记录，继续 T/M | 仅显式启用 |
| `cold_detail` | R → T | R 空可回查 T | 默认仅 `mtr/rarely` |

补充约束：T 只使用 `mtr/trial` 视图；未带 MTR 标签的内容为 `unmanaged / needs_classification`，不得默认视为 T。普通查询不得隐式加入 R。

### 5.4 P1：体验与可诊断性

| 编号 | 需求 | 完成口径 |
|---|---|---|
| P1-01 | 人类可读输出 | 在 JSON 外提供简洁 text 输出，展示路由顺序、规则版本、降级与拒绝原因。 |
| P1-02 | 配置错误诊断 | 对重复 ID、多个权威 Location、未知引用、非法规则版本给出具体字段级错误，不泄漏秘密。 |
| P1-03 | 补充 Fake 故障场景 | 增加分支不存在、无 frontmatter、异常 YAML、空响应和权限不足等只读 Fixture。 |
| P1-04 | 决策日志检查入口 | 能按请求哈希/计划哈希查看本地 Decision Log，便于重放与排查。 |

### 5.5 P2：可选增强

- 为 CLI 提供 shell 补全。
- 提供两个 Fixture 的 ReadPlan 差异对比。
- 提供只读路由的文本图或静态可视化，不引入图数据库或服务端。

## 6. CLI 交互契约

### 6.1 通用约束

- 命令：`mtrp route query`。
- 必要输入：`--query`、`--intent`；涉及 P 时必须提供可解析的 `--project` 与 `--repo`。
- 配置输入：通过 `--config-dir` 指定包含 `stores.yaml`、`projects.yaml`、`routing.yaml` 的本地配置目录；示例配置可复制后使用，不需要真实凭据。
- Fake 输入：`--fixture` 可选；缺省使用全空 Fake 数据。无论是否提供 Fixture，本轮都不得切换为真实 Adapter。
- R 输入：普通 intent 只有显式 `--include-r` 才加入 R；`cold_detail` 默认包含 R。
- 坐标输入只接受已登记 ID。作为数据源/坐标的 URL、绝对路径、路径逃逸和未登记 Workspace 必须拒绝；普通问题文本中出现 URL 不等同于授权访问该 URL。
- 输出：成功时 stdout 为稳定 JSON；诊断写 stderr 或本地日志。ReadPlan 不含时间戳、随机 ID 等不确定字段。
- 退出码：`0` 表示计划成功（包括空结果和降级）；`2` 表示输入/配置/权限边界校验失败；`1` 表示未预期内部错误。空库、空仓不是错误。

以下示例假设本地配置已由示例复制生成，且所有数据访问均由 Fake 完成。

### 6.2 项目上下文：默认不含 R

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

示例输出（字段可扩展，但语义不得改变）：

```json
{
  "intent": "project_context",
  "rule_version": "v1",
  "include_r": false,
  "read_only": true,
  "project_coordinate": {
    "project_id": "mtrp",
    "repo_id": "mtrp-principles",
    "location_id": "github-main",
    "ref": "main"
  },
  "steps": [
    {"store": "P", "on_empty": "continue"},
    {"store": "M", "on_empty": "continue"},
    {"store": "T", "tag": "mtr/trial", "on_empty": "return_empty"}
  ],
  "write_proposal": null
}
```

### 6.3 冷门查询与空结果降级

```bash
mtrp route query \
  --config-dir config \
  --query "查找旧版实现细节" \
  --intent cold_detail \
  --fixture tests/fixtures/r-empty.json \
  --format text
```

```text
规则版本: v1
只读计划: R[mtr/rarely] -> T[mtr/trial]
空结果策略: R 为空则继续 T
外部写入: disabled
```

使用 `project_context` 和 `tests/fixtures/m-empty-p-empty.json` 时，必须显示 `P_EMPTY`、`M_EMPTY` 及继续后续步骤的决策，不返回伪造 Resource，退出码仍为 `0`。

### 6.4 未登记路径：拒绝且不访问磁盘

```bash
mtrp route query \
  --config-dir config \
  --query "读取这个工作区" \
  --intent project_context \
  --project mtrp \
  --repo mtrp-hub \
  --workspace E:/not-registered/repo
```

```text
退出码: 2
错误码: UNREGISTERED_WORKSPACE
结果: 不生成 ReadPlan，不读取该路径，不调用任何 Adapter，Decision Log 仅记录脱敏后的拒绝原因。
```

## 7. 功能验收标准

### 7.1 阶段 0

1. 在无真实乐享/GitHub 凭据、无真实 Vault 的环境下，使用 `D:/DevelopSoftwares/miniconda/python.exe` 可完成安装、CLI 帮助调用和本轮 Pytest；验收命令或报告须证明实际运行版本为 Python 3.11（当前实测为 3.11.11）。PATH 默认 Python 3.13.14 不作为 3.11 验收证据，也不要求修改系统默认 Python。
2. 示例配置、环境变量模板、日志目录规则与 `.gitignore` 完整；版本控制状态中不出现真实秘密、本地运行日志或数据库文件。
3. 能力矩阵逐项区分已实测、可用、未实测和禁用，尤其明确：聊天连接器能力不等于独立 Core 能力，远端空仓不等于可读 P 资产。
4. 三类只读冒烟契约均有 Fixture 测试：Vault 解析不修改文件；M 空结果成功；GitHub 读取契约定位到 repo + SHA + path。
5. 本轮测试不需要也不触发真实外部写入、真实磁盘扫描、commit 或 push。

### 7.2 阶段 1

1. 六类 Canonical Model 的正常、缺必填字段、非法引用与序列化测试通过。
2. Store/Project Registry 能解析权威默认坐标；重复权威 Location、未知 repo/location/workspace 均被拒绝。
3. 同一请求、同一配置、同一规则连续运行三次，stdout 的规范化 JSON 逐字节一致，Decision Log 能解释相同决策。
4. 五类 intent 的步骤顺序与降级符合 5.3；默认路由不含 R，只有 `cold_detail` 或显式 `--include-r` 才包含 `mtr/rarely`。
5. M 空库与 P 空仓返回明确状态并继续适用步骤，不报错、不停止其他库计划、不伪造命中。
6. 同名分支不同 SHA 的 Fixture 生成不同快照坐标/证据引用，不得仅按 repo + branch 合并；分支未知或空仓不得伪造 commit。
7. 未登记本地路径、任意坐标 URL、任意绝对路径、路径逃逸均被拒绝，且拒绝前不读取目标。
8. `MTR-P-hub` 空仓 Fixture 只显示 `EMPTY_REPOSITORY`/等价状态；不得列为已有可读 README 或已有快照的 P 资产。
9. `WriteProposal` 只能作为数据模型存在；CLI 无 approve/apply/write/publish/commit/push/merge 等执行能力。

## 8. 安全验收标准

1. **真实 Vault 零修改**：本轮自动化只使用临时 Fixture；不读取或改写计划中所列真实 Vault，不生成标签变更，不创建备份到真实 Vault。
2. **外部写全关闭**：M 创建/编辑/发布、GitHub 分支/文件/PR/提交/推送/合并均无调用入口；任何写请求返回非零退出码并记录拒绝原因。
3. **Git 操作受控**：不自动 commit、push、pull、rebase、merge，不修改 HEAD、分支、索引或工作区；本轮不依赖首个远端提交。
4. **磁盘零发现**：不遍历个人目录、不自动搜索仓库或 Vault；仅读取显式配置、测试 Fixture 和已登记且必要的元数据，路径逃逸测试必须证明目标未被访问。
5. **网络零依赖**：Fake 测试在禁网环境通过；不得因 Fixture 中的 owner、space_id、URL 或路径而发起真实请求。
6. **秘密零入库/零日志**：`.env`、真实凭据、本地运行配置和日志被忽略；示例无有效秘密；Decision Log、错误输出、Resource/Evidence 不含令牌、Cookie 或认证头。
7. **计划只读且不可被请求改写**：调用方不能通过 query、URL、路径或 Fixture 指定写操作、改变固定路由、替换权威 Location 或提升权限。
8. **诚实降级**：空库、空仓、缺失快照和权限拒绝均保留真实状态；不得把“未执行真实连接”标记为“真实连接成功”。

## 9. 四个人工确认门

四个门不是本轮代码自动通过的检查项，必须由用户/主理人明确确认。未确认部分不得被实现者自行推断为授权。

| 门 | 待确认内容 | 当前默认/状态 | 未确认前的本轮边界 |
|---|---|---|---|
| G0：实施仓 | `MTR-P-hub` 为正式 Core 仓、本地路径，以及首个 main commit/push 的单独授权 | 仓址与本地克隆按计划确认；首个 commit/push 仍未授权 | 可本地开发测试；不得把空仓当 P 资产，不自动提交或推送 |
| G1：T/R 范围 | managed roots 与 excluded roots | 建议 `2-Permanent` 子集；排除 `2-Permanent/Resources/MTR-P-System` 和 `2-Permanent/Areas/AI-Assets/_templates`，待确认 | 仅用 Fixture 表达范围；不扫描或读取真实 Vault，不扩大管理范围 |
| G2：标签与阈值 | `mtr/trial`、`mtr/rarely`、180 天下沉、30 天至少 2 次复活 | 标签用于本轮 Fake 路由；生命周期阈值仅记录为建议，待真实分布后确认 | 不计算或执行生命周期，不修改标签；`status/stale` 不得当作 R |
| G3：首批样本 | 用于标签提案测试的 5–10 篇笔记及逐篇授权 | 未选定 | 不自动挑选、读取或列出真实样本，不生成真实 dry-run 清单 |

## 10. 待确认问题与非阻塞默认

以下问题不阻塞本轮 P0 开发；未答复时采用“默认处理”，不得借此扩大权限。

| 问题 | 默认处理 |
|---|---|
| 本轮是否额外执行真实 Vault/乐享/GitHub 只读冒烟？ | 默认不执行，只交付契约与 Fixture；如需执行须另行明确范围，且仍禁止写入。 |
| G0 的首个 commit/push 何时执行？ | 默认留待用户单独确认，不属于本轮验收。 |
| 初始规则版本名是否采用 `v1`？ | 默认采用 `v1`；任何路由语义变化必须升级版本。 |
| M/P 真实只读集成何时开始？ | 默认阶段 2；本轮 Fake 成功不得写成真实 Adapter 已完成。 |
| G1/G2/G3 何时确认？ | 默认保持待确认；不阻塞 Router、Registry、Fake Adapters 与只读契约开发。 |
| 空仓 `MTR-P-hub` 的权威坐标如何展示？ | 保留登记的逻辑坐标与 `empty` 状态，commit 为空；首次可检索快照待后续真实提交推送后登记。 |

## 11. 本轮完成定义（Demo）

本轮完成时，必须能在禁网、无真实 Vault、无真实凭据的环境中演示：

1. 从示例配置启动 CLI，为一个项目问题生成稳定且可解释的 P → M → T 只读计划。
2. 连续三次相同请求输出完全一致；普通请求不含 R，冷门请求正确包含 R。
3. 用 Fixture 展示 M 空库、P 空仓正常降级，以及同名分支不同 SHA 不混淆。
4. 未登记路径和写请求被明确拒绝，且没有磁盘目标访问或外部写调用。
5. 能力矩阵、Decision Log 和自动化测试共同证明：本轮交付的是安全路由基线，而不是真实外部集成或写入能力。
