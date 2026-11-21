# 在线版 vendored 基线与云能力冒烟记录

## 固定 YAML 基线（T01）

- `js-yaml`：4.1.0，官方 `dist/js-yaml.min.js` UMD 文件，同源 vendored。
- 大小：39430 字节。
- SHA-256：`45dc3dd03dc07a06705a2c2989b8c7f709013f04bd5386e3279d4e447f07ebd7`。
- 来源与 MIT LICENSE 来源完整记录于 `vendor/BASELINE.json`，运行时不请求 CDN。
- 对拍参照：`ruamel.yaml 0.19.1 / YAML(typ='safe') / allow_duplicate_keys=False`。

## cloud-service 能力冒烟（T03，未完成）

执行顺序：

1. 对 `apps/connect-panel/online` 执行 cloud-service `inspect`：本会话应用数 0。
2. 按续工授权执行 `activate`，目标「MTR审核工作台」，用途「审核记录保存和账号登录」。
3. 受信任的 WorkBuddy 确认界面被取消；实际返回：`activated:false`、`cancelled:true`、`useLocalImplementation:true`。

因此没有获得 `publicConfig`，没有真实云环境，未执行建表 SQL，未创建任何云资源。**CHECK、RLS、A/B 账号隔离均未实测，不得视为通过。** 目前尚无可用于真实联调的云端表。

此外，当前官方 cloud-service 文档描述的是 managed PostgreSQL + PostgREST，推荐用户行使用 `TEXT DEFAULT auth.uid()` 和 PostgreSQL RLS；与架构 §4 的「云 SQLite」描述不一致。本文不擅自修改架构 DDL；已向 team-lead 报告并请求架构裁定。真实环境可用后，应先以 `review_ledger_item` 与 `review_history` 两表探测 CHECK 和 RLS，再执行 A/B 账号探针；CHECK 不支持时，仅按架构既定方案将约束校验收敛至 `reviewDomain.validateState`，不得放宽 RLS。

## 浏览器 SDK 边界

纯静态应用仍须使用 `@tencent-ai/workbuddy-cloud-sdk`。当前官方文档对无构建站点提供 CDN 形式，与本项目 B3「零运行时 CDN」冲突；实施时将固定版本同源 vendored，记录版本、LICENSE、hash，且不引入构建链。不得手写 `/.cloud/**` 网络请求或另换 Supabase/CloudBase 客户端。

## 第一关证据

- 扩展受管 fixture：44 篇；stage2 只读受管 fixture：6 篇；联合 50 篇。
- 两套各自差异数均为 0；NIST 双端每套 8 个结果均通过；`size_bytes` 始终严格比较。
- 联合报告：`reports/parity_report.json`；独立报告前缀为 `extended_` 和 `stage2_`。
- 本阶段未扫描真实 Vault，未执行浏览器真实授权对拍。

## T04 自动核验（不含真实浏览器授权验收）

- Node 全套测试：56 项通过；其中 Worker 与纯扫描核心对同一扩展 fixture 的 44 篇输出逐字段深度全等。
- 生产 Worker 使用 `FileSystemFileHandle.getFile().arrayBuffer()` 读取；普通对象不可假定拥有 Blob 方法。
- 正文提取链接后在主线程释放，仅保留扫描事实；详情仍按原始相对路径单篇重读。
- 比较器反证：Python unittest 5 项通过。
- 双回归：主仓 225 项、本地版 72 项通过。
- 联合 fixture gate：50 篇、差异 0、NIST 16 项通过，报告为 `reports/parity_report.json`。
- **未完成**：真实 Chromium 用户手势、目录取消/选错/失效的真实交互，真实 Vault 扫描与前后全树 hash 快照。上述仍属 T04/T10 待验，不能由 Node 内存句柄测试替代。
- T03 云台账、离线缓冲仍阻断；未执行 T09 静态审计 gate，未进入后续功能任务。

### 30 篇读取失败的最小复现与根因

- 首个失败路径：`2-Permanent/encoding/08-bom-crlf.md`。
- 旧测试夹具调用链：`root.getDirectoryHandle('2-Permanent') → getDirectoryHandle('encoding') → getFileHandle('08-bom-crlf.md')`。
- 失败时父句柄为 `{ kind:'directory', name:'encoding' }`，但旧递归子目录缺少 `getFileHandle`，抛出 `TypeError: directory.getFileHandle is not a function`；在 `getFile()` 之前失败。
- 修复后的同一目录句柄为 `{ kind:'directory', name:'encoding', hasGetFileHandle:true }`；单文件读取为 66 字节，扫描 `parse_status:'ok'`，SHA-256 为 `73256f542c8d0553afd88e8d5f0940c883d3e6b92262c0efef525a7662845a34`。
- 根因是 Node 测试模拟适配器未完整实现 FileSystemDirectoryHandle 契约；生产路径不需要因此改动。测试夹具已统一递归构造完整目录/文件句柄，不再删除或豁免失败项。
- 曾临时推测「30 篇全是嵌套文章」，与保留失败清单不完全一致，已撤回；最终证据使用全量 44 篇双端零 `FILE_READ_FAILED` 与逐字段深度全等，不依赖该分类假设。
- 最终命令结果：Node 56/56 通过；44 篇 Worker↔walkCollect 深度全等且双端 `FILE_READ_FAILED=0`；联合 50 篇双引擎差异 0、NIST 16/16。真实 Vault 与云资源均未触碰。
