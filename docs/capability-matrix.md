# MTR-P Hub 能力矩阵

> 事实口径保留自 2026-11-18 实施调查；阶段 0+1 代码只提供确定性路由、Registry、Fixture 与 Fake 只读闭环。

| 能力 | 状态 | 事实与边界 |
|---|---|---|
| 本地 Python 包、CLI、Canonical Model、Registry | tested | 阶段 0+1 由 Python 3.11.11 虚拟环境和自动化测试验收。 |
| 五类 intent 的确定性 ReadPlan | tested | 仅版本化 v1 规则；不包含真实工具名、网络参数或写操作。 |
| Fixture / Fake P、M、T、R 只读闭环 | tested | 空库、空仓和故障均诚实降级；Fake 成功不代表真实连接成功。 |
| Obsidian 本地 Markdown 读取与 frontmatter 解析 | tested | 2026-11-18 已实测 WorkBuddy 本地文件能力；本轮仅解析给定 Fixture 文本，不扫描或修改真实 Vault。 |
| 乐享 MCP 空间、目录与搜索 | tested | 2026-11-18 在 WorkBuddy 连接器中实测；M 当前空结果属于成功，不等于独立 Core SDK 已接入。 |
| GitHub MCP 仓库、分支、按 SHA 读取 | tested | 2026-11-18 在 WorkBuddy 连接器中实测；不等于独立 Core 已继承凭据。 |
| WorkBuddy 乐享/GitHub 连接器后续只读执行 | available | 依赖当前聊天连接器状态；本轮不实现 ExecutionManifest 或执行器。 |
| Core 独立乐享/GitHub Native Adapter | not-tested | 需要后续独立凭据、权限、超时和密钥管理，本轮未实现。 |
| `MTR-P-hub` 作为可读 P 资产 | not-tested | origin 为 `git@github.com:13Asix21/MTR-P-hub.git`；截至实测远端为空，无 README 或快照可声明。 |
| FastAPI / MCP Hub | disabled | 属于后续独立服务阶段，本轮无服务端实现。 |
| M 创建、编辑、发布 | disabled | 本轮无入口、无实现。 |
| GitHub 分支、提交、推送、PR、合并 | disabled | 本轮无入口、无实现，不自动执行 Git 写操作。 |
| Vault 标签修改、移动、批量分类 | disabled | G1 仅确认授权范围内只读；G2/G3 未确认，本轮无生命周期、补标签或真实写入。 |
| Workspace 查询 | disabled | Registry 可表达显式登记项；CLI 即使遇到已登记 Workspace 也拒绝本地路由。 |
| 自动磁盘发现、真实 URL 抓取 | disabled | 请求和 Fixture 不能注入路径、URL、Adapter、网络或工具参数。 |

## 阶段 2 增量状态

| 能力 | 状态 | 事实与边界 |
|---|---|---|
| Obsidian Adapter — `obsidian-local / Fixture-tested` | fixture-tested | 临时 Vault 已覆盖 managed roots、排除优先、中文/NFC、reparse 拒绝、双读原始字节 hash、T/R/unmanaged/conflict、代码排除及单文件故障隔离；不是实际 Vault 冒烟。 |
| Obsidian Adapter — `obsidian-local / real-smoke` | tested; real-degraded | G1 授权范围只读执行前后内容聚合 hash 一致；scanned=1830、unmanaged=1818、T/R matched=0、禁区 I/O=0、Vault write=0。12 个真实 INVALID_YAML 导致 `VAULT_PARTIAL_FAILURE`，不写成 EMPTY/OK，不自动修笔记。 |
| ExecutionManifest、expected recompile、Envelope、Executor | tested | Fixture 已验证篡改拒绝、Core barrier、continuation 与确定性；run3 真实全链另完成 expected-recompile、7/7 调用参数、9/9 receipt、projection、raw response hash 与 DB 对照。 |
| 乐享空间解析 — `workbuddy-manifest` | tested | run3 真实空间解析 OK，只映射 `data.space` 的三个逻辑 ID；不代表 Core Native 已接入。 |
| 乐享普通/向量空搜索 — `workbuddy-manifest` | tested; real-empty | run3 普通/向量搜索均为真实 EMPTY，条目读取调用=0，M 空后继续 T；空搜索不证明非空条目能力。 |
| 乐享非空条目 describe / AI parse / score / version | Fixture-tested; real-not-tested | 非空字段映射只用合成 Fixture 验证；真实遇未知非空形态以 `M_NONEMPTY_CONTRACT_UNCONFIRMED` fail-closed，不创建测试条目。 |
| GitHub branches / ref→完整 SHA / root@SHA | tested | run3 对 `13Asix21/MTR-P` 完成 branches→main/ref→`c3a6c7b83ced99a01b2b51039e87a40ee29df6a5`→root@commit，全部 OK。 |
| GitHub file@SHA 受控文本与合成 base64 | tested + Fixture-tested | run3 读取 `.gitignore`@同一 commit 成功；正文 SHA-256=`44bd525761aaaf8ca4cf44c3291323e7618350ce827cc5957b22c8428ea167d3`，hash_scope=`response_utf8`。任意位置重复合法下载头均拒绝；base64 仅合成 Fixture。 |
| Core Native 乐享/GitHub/Obsidian SDK | not-tested/disabled | WorkBuddy 契约或后续实测均不提升 Native 状态；仓内无 HTTP、SDK、subprocess 或 LocalGit transport。 |
| 外部写、生命周期、G2/G3、LocalGit/dirty query、FastAPI/MCP Hub | disabled | 无写入口，不读取本地 dirty 内容，不提前实现后续阶段能力。 |

### 阶段 2 最终验收证据（2026-11-19）

- 自动化：225 passed、0 failed、0 skipped；构成 118 条旧测试原样 + 1 条授权安全迁移 + 106 条阶段 2 新增测试。
- 唯一真实全链为 `run_seq=3`：Manifest `1c5593fe326250c2074b640ea680d2ff57c3b68fbd6511ce042aa1babd117a93`，trace `trace_2a60663b83b5a895531c803d20f7051a9b27d7d293a56ed498fdc9b617a9645e`。
- run3 规范化 stdout：7255 bytes，SHA-256=`36bd4676e4b5b489b6ff1ad39c6ec849492fbfed2213d33f25dd5ac1d99b3d37`；file raw response SHA-256=`4364569773289de90a4a7e76886d131eca8bacbc6d5af80b8eba1e106efa60bc`。
- run4 及后续重复 finalize 仅为诊断，未混入 run3 的计数、hash 或能力状态。

`tested` 仅表示表格所述载体和范围已经验证；`available` 不表示 Core 自动继承；`fixture-tested` 不表示真实连接成功；`contract-probed` 只表示响应形状已探测；`not-tested` 不表示已可用；`disabled` 表示本轮明确关闭。
