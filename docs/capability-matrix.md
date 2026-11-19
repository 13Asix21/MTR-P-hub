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
| `MTR-P-hub` 作为可读 P 资产 | not-tested | origin 为 `git@github-mtrp:13Asix21/MTR-P-hub.git`；截至实测远端为空，无 README 或快照可声明。 |
| FastAPI / MCP Hub | disabled | 属于后续独立服务阶段，本轮无服务端实现。 |
| M 创建、编辑、发布 | disabled | 本轮无入口、无实现。 |
| GitHub 分支、提交、推送、PR、合并 | disabled | 本轮无入口、无实现，不自动执行 Git 写操作。 |
| Vault 标签修改、移动、批量分类 | disabled | G1/G2/G3 未确认；本轮无生命周期或真实写入。 |
| Workspace 查询 | disabled | Registry 可表达显式登记项；CLI 即使遇到已登记 Workspace 也拒绝本地路由。 |
| 自动磁盘发现、真实 URL 抓取 | disabled | 请求和 Fixture 不能注入路径、URL、Adapter、网络或工具参数。 |

`tested` 仅表示表格所述载体和范围已经验证；`available` 不表示 Core 自动继承；`not-tested` 不表示已可用；`disabled` 表示本轮明确关闭。
