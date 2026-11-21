# 本地 L01 独立 QA：运行时与迁移安全

- 验收时间：2026-11-21（UTC+8）。
- 验收人：Edward / QA Engineer。
- 范围：本地启动、独立数据根、安全模板、显式迁移、回环请求防护、静态资源路径和 bind 后浏览器回调。
- 第一轮结论（历史）：**不通过，路由 Engineer**。3 个生产问题分别导致目标数据被覆盖/误删，以及显式迁移在启动后不可用。
- 第二轮最终结论：**本轮已测范围通过，Send To: NoOne**。原 3 个 P1 及扩展审核边界发现的 L01-004 均已通过独立复验；真实 GUI、真实用户档案/Vault、打包、跨平台及掉电恢复仍不属于本结论。
- QA 只维护独立测试、隔离 runner 和本文档，未修改生产代码，未修改 Engineer 的 `tests/test_runtime.py`，未 commit、push 或部署。

## 1. 隔离边界与测试轮次

所有迁移输入、竞争者档案、用户档案、数据根、导出文件和扫描 Vault 副本均位于系统临时目录。旧源文件由 `ProfileStore` 现场合成，包含空 Vault 偏好，不复制真实档案。

既有 `tests/conftest.py` 会读取仓库 `apps/connect-panel/data/users.json` 并在 teardown 写回，故本轮**没有直接运行原始全套面板测试**。新增 `tests/qa_l01_isolated_runner.py` 在导入 conftest 之前将旧 session 服务的档案、配置、审计及导出路径重定向到临时目录；独立 L01 子进程保留各自显式的合成数据根与 legacy。该 runner 不改变生产代码，也不读取或复制真实档案。

测试轮次：

1. Round 1：独立安全/迁移测试发现占位竞态；隔离全套面板测试最初为 9 failed / 99 passed。
2. Round 2：QA 修复自身 runner 的默认路径重定向与 ReadyServer 测试的数据目录隔离，并增加真实 HTTP 迁移/空 Vault 检查。最终仍发现 3 个生产失败；按两轮上限停止，不进入第三轮。

下文的“真实 HTTP”指真实 Uvicorn 子进程、OS 分配回环端口和 HTTP 请求；“确定性时序”指测试在指定系统调用边界注入竞争者/异常，**不是**真实多进程压力测试。

## 2. 实测命令与原始结果

均从仓库根目录执行，解释器为 `.venv/Scripts/python.exe`。

### 2.1 主仓回归

```bash
.venv/Scripts/python.exe -m pytest -q
```

结果：`225 passed in 23.49s`，退出码 `0`。

### 2.2 独立 L01 安全与迁移测试

```bash
.venv/Scripts/python.exe -m pytest -q apps/connect-panel/tests/test_qa_l01_runtime.py
```

结果：`3 failed, 11 passed in 4.91s`，退出码 `1`。共 14 个测试节点，其中 5 个为安全参数化用例。3 个失败全部是保留的生产保护断言，没有为了全绿而删改。

通过项目：

- 已存在的非空目标拒绝迁移，目标与合成旧源原字节保持。
- 注入 `os.replace()` 失败且竞争者已写入非空目标时，目标与旧源保持。
- 显式新数据目录不复制合成旧档案，无 repo 用户配置时选择 `panel/default_config`；源码开发默认仍指向原开发档案位置，不自动复制。
- 伪造 Host、外部/错端口/HTTPS Origin、`Sec-Fetch-Site: cross-site` 均 403；带 body 的非 JSON 写请求为 415。
- 真实 port 0 + 中文数据目录 + 非仓库 cwd：health 200、`local_only=true`；overview 200、空 Vault、`status=pending`；静态首页 200；目标数据目录创建档案，未创建 repo 配置副本。
- 真实 HTTP 下同源且同端口 Origin + `same-origin` 的 GET/JSON POST 200；无 body 的旧 POST 200。
- 未配置 Vault 的显式检查返回 400 `PATH_REQUIRED`，不扫描、不伪造成功。
- 浏览器动作以 mock 替代，但真实 Uvicorn bind 和 health 请求已执行；mock 收到 OS 实际分配端口，且回调后可请求 health。该用例的数据目录已显式隔离。

失败项目：L01-001、L01-002、L01-003，详见第 4 节。

### 2.3 既有面板测试的隔离复跑

```bash
.venv/Scripts/python.exe apps/connect-panel/tests/qa_l01_isolated_runner.py \
  --test-dir apps/connect-panel/tests \
  --ignore=apps/connect-panel/tests/test_qa_l01_runtime.py --tb=short
```

结果：`8 failed, 100 passed, 1 warning in 14.41s`，退出码 `1`。该命令覆盖 108 个既有测试节点，不包含独立 QA 文件；不是工程师报告的原环境 106 节点，也不应表述为“面板 106 全过”。

| 失败节点 | 分类与依据 |
|---|---|
| `test_panel_api.py::TestUserProfileLayer::test_list_users_baseline_and_default_snapshot` | 旧测试在 `test_panel_api.py:217` 硬编码真实 Vault 路径含 `Obsidian Vault`；独立安全模板正确为空。测试环境假设，不是新数据根生产缺陷。 |
| `test_panel_api.py::TestAuditStore::test_recent_limit_5_whitelist_columns` | 期望 5 条真实审计记录；安全模板无审计库，生产返回 503 `AUDIT_NOT_FOUND`。测试环境假设。 |
| `test_panel_api.py::TestLexiang::test_lexiang_config_valid_but_limited` | 期望真实 Lexiang 配置为 `limited`；安全模板未配置，实际 `unconfigured`。测试环境假设。 |
| `test_panel_api.py::TestLexiang::test_lexiang_query_form_still_accepted` | 同上；端点本身 200，失败只在 `limited` 状态假设。 |
| `test_review_workbench.py::test_07_export_approved_all_and_csv` | `test_review_workbench.py:324-326` 把相对导出响应固定拼到仓库 `PANEL_DIR/data/exports`，同时又与临时 `EXPORTS_DIR` 比较，两个前提互相矛盾。旧测试路径适配问题。 |
| `test_review_workbench.py::test_09_nfc_request_and_record_keys` | `test_review_workbench.py:446` 同样从仓库旧导出路径读取，实际文件在临时数据根，产生 FileNotFoundError。旧测试路径适配问题。 |
| `test_review_workbench_qa.py::test_qa_12_old_endpoints_regression_smoke` | 审计请求期望 200；安全模板没有真实审计库，实际 `AUDIT_NOT_FOUND`。测试环境假设。 |
| `test_runtime.py::test_real_http_migration_confirmation_and_source_preservation` | 启动后目标已经存在，与显式迁移预期冲突；独立 L01-003 的真实 HTTP 探针进一步确认生产问题，不能仅归为夹具问题。 |

另有真实 `audit`/`Lexiang` 依赖未在本轮伪造“通过”。QA runner 仍有改进空间：可将旧测试的相对导出路径读取适配到临时根，并为需要审计/配置契约的用例建立合成 fixture；本轮不以进一步改测试来掩盖生产问题。

## 3. 静态审查与已测边界

| 能力 | 证据 | 结论 |
|---|---|---|
| 数据目录优先级 | `panel/runtime.py:57-95`：显式参数 > 环境变量 > 开发默认/每用户目录 | 静态符合；显式新根已由合成旧源实测不自动复制。 |
| 安全模板 | `panel/runtime.py:175-192`、`panel/default_config/stage2.yaml:1-25` | 无用户/开发配置时回退安全模板；空 Vault、未配置 Lexiang 均诚实表达。 |
| 源码默认兼容 | `panel/runtime.py:75-77,101-112` | 开发默认使用旧位置且不要求迁移；未发现自动复制路径。 |
| 启动与静态路径 | `run_panel.py:56-67`、`panel/main.py:43,211-217` | 先 `startup()` 成功再取 socket 端口并打开浏览器；静态文件基于 `__file__`，不依赖 cwd。真实 bind + mock 浏览器动作已测，GUI 未测。 |
| 回环 Web 防护 | `panel/runtime.py:195-246`、`panel/main.py:154-174` | Host、Origin 端口/协议、cross-site、带 body JSON Content-Type 均经独立实测；这是本机请求防护，不是身份认证。 |
| 审计只读 | `panel/audit_reader.py:31-58` | `mode=ro&immutable=1` + `PRAGMA query_only=ON`，活动 WAL 和缺库诚实失败；真实数据库内容未访问。 |
| 导出隔离 | `panel/review_api.py:608-615` | 正式导出跟随运行时数据根；旧测试仍按仓库固定路径读取，需 QA 适配。 |

## 4. 生产问题与精确复现

### L01-001 — P1：占位 `stat` 与 `os.replace` 之间存在 TOCTOU，可覆盖竞争者的非空目标

- 位置：`apps/connect-panel/panel/runtime.py:146-152`。
- 根因：`open("xb")` 只保证创建占位时排他；后续 `stat().st_size == 0` 与 `os.replace()` 不是一个原子发布操作，也不证明替换时目标仍属于当前迁移。
- 复现测试：`tests/test_qa_l01_runtime.py:40-68`，`test_qa_migration_nonempty_replacement_is_not_deleted_or_overwritten`。
- 确定性时序：占位创建后，包装 `Path.stat()`；原 stat 返回零字节后，竞争者把同一路径替换为 `b'{"schema_version":"1","users":[]}\n'`；生产继续执行 `os.replace()`。
- 预期：抛 409 `MIGRATION_NOT_AVAILABLE`，竞争者原字节保持，旧源原字节保持。
- 实测：没有抛异常，测试为 `DID NOT RAISE`；函数继续按迁移成功路径执行，竞争者目标被迁移临时文件替换。
- 影响：另一进程/实例刚创建或写入的档案可能被静默覆盖。
- 修复方向：由 Engineer 设计真正不覆盖已有目标的发布协议；不要只再增加一次 stat。可考虑同文件系统上的原子“仅当不存在”发布/硬链接 + 独占清理，或受验证的跨进程锁与占位所有权方案，并保留此确定性回归测试。

### L01-002 — P1：失败清理只按“当前零字节”删除路径，可误删竞争者接管的新文件

- 位置：`apps/connect-panel/panel/runtime.py:159-167`。
- 根因：异常时 `finally` 再次 stat 目标路径，只要大小为 0 就 unlink；路径可能已经不是本迁移创建的占位。
- 复现测试：`tests/test_qa_l01_runtime.py:71-92`，`test_qa_migration_zero_byte_replacement_survives_failure_cleanup`。
- 确定性时序：注入 `os.replace()`；在抛 `OSError("QA injected replace failure")` 前，竞争者把目标路径替换为新的零字节文件。
- 预期：返回 `MIGRATION_WRITE_FAILED`，旧源不变，竞争者文件仍存在且大小为 0。
- 实测：错误码正确、旧源不变，但 `paths.profile_path.exists()` 为 `False`；finally 删除了竞争者文件。
- 影响：失败路径同样不具备数据保护保证，不能仅凭“空文件可能不重要”忽略其新所有权与后续写入风险。
- 修复方向：清理必须证明仍是本次创建的占位；绝不按路径当前大小推定所有权。与 L01-001 一起重构发布/回滚协议，并覆盖非空、零字节和 replace 失败时序。

### L01-003 — P1：lifespan 先建默认目标，使显式迁移提示与迁移端点在启动后不可用

- 位置：`apps/connect-panel/panel/main.py:46-51,121-127,236-242`；`apps/connect-panel/panel/runtime.py:98-119`。
- 根因：lifespan 调用 `ensure_default()`，在全新显式数据根创建 `users.json`；`migration_status()` 又要求目标不存在，迁移函数开头同样检查该状态。前端初始化还先请求 `/api/users` 和 `/api/overview`，均可能初始化档案。
- 复现测试：`tests/test_qa_l01_runtime.py:243-268`，`test_qa_explicit_migration_remains_available_after_startup`。仅将 legacy 路径指向临时合成有效档案，启动真实 `run_panel.py --port 0 --data-dir <临时目录> --no-browser`。
- 预期：启动后仍可检测并显式确认迁移；成功后目标等于旧源，旧源不变。
- 实测元组：`(health.migration_required, POST /api/runtime/migrate status, error.code) == (False, 409, "MIGRATION_NOT_AVAILABLE")`；目标已经是初始化生成的档案，合成旧源保持不变。
- 影响：首次安装用户在启动后看不到可迁移状态，手动确认端点也不能完成迁移；“显式迁移”主流程不可用。
- 修复方向：明确“默认初始化”与“未提交的新目录”的状态机，在显示健康状态/用户列表前保留迁移决策，或持久化独立初始化标记并让迁移只替换本进程明确拥有且未使用的默认档案；不得把 L01-003 的修复变成绕过 L01-001/002 数据保护。修复后恢复并修正 `test_runtime.py:357-379` 的真实 HTTP 迁移验收。

## 5. 未测项与不得外推的结论

- **未执行真实多进程竞争压力测试**。L01-001/002 是确定性竞争者与系统调用时序注入，足以证明生产算法缺少所有权/原子性保证，但不是 OS 级压力覆盖率证明。
- **未打开真实 GUI 浏览器**。只 mock 浏览器动作并验证真实 bind/health；不声称浏览器手势或页面交互通过。
- **未读取、写入或迁移真实 `apps/connect-panel/data/users.json`**，因此本轮不提供其 SHA 不变的实测证明；这是有意的禁止访问边界，不是失败。
- **未访问真实个人 Vault**；扫描相关既有测试使用临时 fixture 副本。未对真实 Vault 做扫描、链接或写保护验收。
- **未验证打包后的 Windows 可执行程序、安装器、跨平台启动和崩溃后目录 fsync/掉电恢复**。
- 既有面板隔离套件中的 100 项通过不能替代原环境的 106 全绿，也不能替代上述真实环境未测项。

## 6. 路由与准入

- **Send To: Engineer**：L01-001、L01-002、L01-003。生产数据保护与迁移主流程未达准入要求；QA 不自行修改生产代码。
- **Send To: QA（后续维护）**：隔离 runner 需要为旧测试的相对导出路径建立一致适配；若要重跑真实配置/审计契约，应使用显式、合成、可审计的 fixture，而非访问个人档案。
- **准入结论**：安全模板、空 Vault 诚实提示、回环请求防护、port 0/cwd/静态资源和 bind 后浏览器回调这些已测能力可保留；**L01 整体暂不通过，尤其不得发布“并发迁移不丢目标”或“启动后可显式迁移”的通过结论**。

---

# 第二轮独立验收（修复后）

## 7. 执行边界、预检与独立实测

第二轮只使用系统临时目录内的合成 legacy、目标档案、审核 Markdown、导出目录和审计数据库。`qa_l01_isolated_runner.py` 不导入原有 `conftest.py` 的真实档案备份路径；旧 session 服务使用合成临时 `users.json`，独立 L01 子进程保留各自显式的 `--data-dir` 和 `MTRP_PANEL_LEGACY_PROFILE`，不会继承真实开发旧档。

主理人曾报告专项 `54 passed in 9.35s`，Engineer 报告聚焦 `56 passed in 8.71s`，两者均只作为上游预检，不代替以下 QA 独立执行。

| 测试范围 | 命令（均从仓库根目录） | QA 实测结果 |
|---|---|---|
| 原聚焦回归 | `.venv/Scripts/python.exe -m pytest -q apps/connect-panel/tests/test_runtime.py apps/connect-panel/tests/test_qa_l01_runtime.py` | `56 passed in 11.51s`，pytest 退出码 `0` |
| 隔离面板全套 | `.venv/Scripts/python.exe apps/connect-panel/tests/qa_l01_isolated_runner.py --test-dir apps/connect-panel/tests --tb=short` | `128 passed, 1 warning in 28.79s`，退出码 `0` |
| 主仓 | `.venv/Scripts/python.exe -m pytest -q` | `225 passed in 21.74s`，退出码 `0` |
| 扩展审核/迁移探针后的聚焦回归 | 同第一行命令 | `1 failed, 62 passed in 19.25s`，退出码 `1`；唯一失败为 L01-004 |

前三项绿结果发生在扩展 QA 探针之前，不能覆盖或抵消之后复现的 L01-004。当时以保留生产失败断言的扩展回归路由 Engineer；第 13 节记录修复后的最终复跑，不改写上述历史数字。

补充探针的设计/调试中出现过重复 pytest 参数化、对未决记录错误期待 reset 成功，以及把 `HistoryEvent` 当字典访问等 QA 问题，分别修正为唯一参数列表、404 零副作用/成功后 undo 两个用例和 `.op` 属性访问。另一次同进程直接调用 `migrate_legacy_profile()` 绕过 HTTP 提交锁的实验不作为生产失败证据；已改成真实 HTTP 的 migrate/skip 竞争。这些均没有通过放宽生产预期来消除失败。

### 沙箱清理警告

多次聚焦、隔离面板及主仓运行的 pytest 主退出码均为 `0`，但 Windows 沙箱在 pytest `atexit` 清理大量历史临时文件时输出 `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 和 `SystemExit: 1`。该输出属于宿主环境的临时目录清理限制，不是测试断言失败，也不是生产异常；未通过手工删除真实档案或关闭保护来规避。另有一次聚焦红运行和一次 3 项补充探针绿运行没有此清理输出。

## 8. 原三项 P1 的第二轮独立判定

| 编号 | 第二轮证据 | 判定 |
|---|---|---|
| L01-001 覆盖竞争者目标 | `runtime.py:141-166` 在与目标相同数据目录创建临时文件，`os.link(temp, target)` 仅当目标不存在时原子发布；`FileExistsError` 转 409。独立 link 边界竞争者注入、线程竞争及 Engineer 的 spawn 双真实子进程用例均执行；后者实测一方 success、一方 `MIGRATION_NOT_AVAILABLE`，目标与 legacy 均等于原合成 source，临时文件清空。 | 本轮已修复 |
| L01-002 清理误删接管文件 | 迁移 `finally` 仅 unlink `NamedTemporaryFile` 返回的自有临时路径。零字节及非空竞争者在 link 失败注入后均原字节保持，legacy 不变；探针断言实际到达 link 边界。 | 本轮已修复 |
| L01-003 启动抢先建默认档 | `main.py` 待决读取走内存默认视图；health/users/overview 重复 GET 不创建目标，显式迁移成功且字节等于 legacy；skip 初始化独立新档；无 legacy 普通新根仍自动初始化，源码开发 data 路径兼容断言通过。 | 本轮已修复 |

`ProfileStore._write_new_unlocked()` 也使用 `os.link` 创建首档；若竞争者已发布则读取实际赢家，而不是把临时 default 当作已保存结果。独立真实 HTTP migrate/skip 并发测试得到一方 200、一方 409 `MIGRATION_NOT_AVAILABLE`，赢家文件不被另一方覆盖，legacy 原字节不变。

这些是 Windows 本机合成 fixture 上的线程、两个 spawn 子进程和确定性故障证据，不是长期压力、跨平台文件系统、断电或目录 fsync 证明。

## 9. 七项旧 fixture 假设的处理结果

以下七项指主理人预检的 `119 passed / 7 failed` 中旧真实环境假设，不把 L01-003 的独立生产失败混入该分类。QA runner 已把它们全部替换为显式、可审计的合成 fixture，最新隔离全套因此为 `128 passed / 0 failed`。

| 原假设 | 第二轮处理 | 状态 |
|---|---|---|
| 默认用户 Vault 路径必须包含真实 `Obsidian Vault` | runner 创建临时空目录，仅目录名满足旧断言；合成默认用户偏好指向它，不读取个人 Vault。 | 已解决 |
| 审计 recent 至少 5 条真实记录 | 创建合成 SQLite `stage2_execution_event` 及 7 条 T 库事件，另建零字节 WAL/SHM fixture。 | 已解决 |
| Lexiang 配置有效但 limited | 写入合成的 32 位十六进制 team/space/root entry 标识；不访问云。 | 已解决 |
| Lexiang query 形式也读取同一真实配置 | 与上一项共用合成配置；完整 run 表字段已补齐，避免 `audit.available=false` 的假失败。 | 已解决 |
| `test_07` 从仓库固定 `data/exports` 读取 | 仅对旧 `test_panel_api`/`test_review_workbench` 调用模块把仓库 data 前缀映射至临时数据根；不扩大下载 API。 | 已解决 |
| `test_09` NFC 导出也从固定仓库路径读取 | 同一路径适配；实际导出仍在临时 runtime data/exports。 | 已解决 |
| 旧端点审计 smoke 期待真实审计库 | 使用上述合成事件库；主仓真实配置/审计内容未读取。 | 已解决 |

路径包装初版曾递归、污染独立 L01 子进程和开发默认路径；已通过预计算原始 `panel_data`、限制调用模块及仅为旧 session 服务注入环境修复。`stage2_execution_run` 初版只有 `run_seq`，与 `AuditReader._RUN_COLUMNS` 不匹配；runner 已补齐 16 个读取字段和一条合成 run。全部属于 QA 自身 fixture 修正，不算生产 Bug。

## 10. 审核待决状态与导出独立覆盖

真实 Uvicorn 子进程 + OS 分配 port 0，使用两篇合成 Markdown 和显式合成 legacy：

- `scan`、`listing`、`records`、`progress`（summary/directory）、使用清单键的 `item` 以及 dry-run export 重复请求均返回 200，目标 `users.json` 持续不存在；dry-run 不创建 exports。
- 待决实际导出返回 409 `MIGRATION_DECISION_REQUIRED`，目标档案和 exports 均不创建。
- 成功 `decide`、`position`、`batch` 明确创建独立新档并保存相应审核结果，不复制 legacy。成功 `decide → reset(undo)` 清除当前结论且保留 UNDO 历史。
- 内容 SHA 不匹配的 decide 返回 409，空批次（全部内容冲突）返回 200 但 succeeded=0，均不建档。空 reset 的 404 失败见 L01-004。
- 实际导出在决定提交后返回旧 `files` 字段及 `files_absolute`；两个绝对路径均位于临时 runtime `data/exports`，文件实际存在，旧字段文件名一致，`export_location` 为“当前数据目录/exports”。真实 GUI 对路径的显示未由 QA 打开浏览器验证。

## 11. L01-004 — P1：待决空 reset 返回 404，却在校验前提交默认档案

- 位置：`apps/connect-panel/panel/review_api.py:387-406`，当前第 391 行 `store = writable_factory()`。
- 根因：writable factory 在待决状态会调用 `_profile_for_commit()`；reset 在确认记录存在及 `reset()` 能成功之前就取得 writable store，从而创建 `users.json` 并关闭迁移。
- 复现：有效合成 legacy + 目标不存在，真实 HTTP 启动；扫描合成 Vault；对扫描中尚未决定的路径 POST `/api/review/reset`，`mode="undo"`。
- 实测：响应为 404 `REVIEW_ITEM_NOT_DECIDED`，但目标文件已经存在。同一用例先前 decide 内容冲突 409 与全失败 batch 后目标均不存在，说明是 reset 特有前置提交。
- 保留的红断言：`test_qa_pending_failed_review_write_does_not_commit` 在 404 后要求 `not target.exists()` 且 legacy 原字节不变。扩展聚焦结果为 `1 failed, 62 passed in 19.25s`。
- 影响：失败/无权限变更语义的请求仍改变迁移状态机，用户失去显式复制 legacy 的机会；404 响应掩盖了持久化副作用。
- 修复方向：待决 reset 先在内存 `ReviewState` 上完成路径与待撤销记录校验，仅变换能成功时才取得 writable factory；正常已有档案及成功 undo 行为保持兼容。
- 路由：**Send To: Engineer**。QA 未修改生产代码或放宽断言。

## 12. 第二轮未测边界与准入

- 未读取、写入、备份或迁移真实 `apps/connect-panel/data/users.json`；未访问真实个人 Vault 或云资源。合成审计/Lexiang fixture 只验证面板消费契约，不证明真实云/真实审计环境可用。
- QA 本轮未执行真实 GUI 浏览器交互。真实 bind、HTTP 与 mock 浏览器回调通过，不等于 GUI、前端 skip 操作或导出路径显示通过。
- 未验证 Windows 可执行分发包、安装器、非 Windows 文件系统、跨设备/不支持硬链接的 FS、长期多进程压力、崩溃及掉电恢复。文件 `fsync` 的存在不等于目录项持久化已验收。
- 两个真实 spawn 子进程竞争和故障注入已测；不外推为所有操作系统或所有故障时序的全证明。
- **当前 Send To: Engineer（L01-004）**。原 L01-001/002/003 和七项 fixture 问题可关闭；只有 L01-004 修复并复跑保留红测试后，才可把 L01 本轮路由改为 NoOne，且仍须保留上述真实环境未测限定。

## 13. L01-004 修复后最终复验

Engineer 已将 `reset_item` 调整为先用只读 factory/`review_state_for_read()` 取得现有审核状态；不存在待撤销记录时直接返回 404 `REVIEW_ITEM_NOT_DECIDED`，只有记录存在才调用 writable factory。QA 保留原红测试及成功 undo 的持久化断言，没有修改生产实现或放宽测试。

以下结果均为 QA 在修复后的独立实测，不采用 Engineer 报告的 63/135/225 数字替代：

| 范围 | QA 最终实测 |
|---|---|
| `test_runtime.py + test_qa_l01_runtime.py` | `63 passed in 21.20s`，pytest 退出码 `0` |
| 隔离面板全套（同一 QA runner） | `135 passed, 1 warning in 32.18s`，退出码 `0` |
| 主仓 `pytest -q` | `225 passed in 27.52s`，退出码 `0` |

原失败用例现确认：内容冲突 decide、全失败 batch 及未决 reset 均不创建目标，reset 仍为 404，legacy 原字节保持。成功 `decide → reset(undo)` 仍保存 UNDO 历史并清除当前结论。上述三次运行均出现相同的宿主 safe-delete `atexit` 清理警告，但 pytest 主退出码均为 0；没有断言失败。

- L01-001/002/003/004：**全部关闭**。
- 七项旧 fixture 假设：**全部由 runner/显式合成 fixture 解决**。
- 本轮生产遗留：**无已复现的未解决失败**。
- 最终路由：**Send To: NoOne**。

该结论仅覆盖本报告实测的合成环境、HTTP、线程及两个 spawn 子进程场景。Engineer 报告的真实 EXE smoke 与主理人报告的真实 Chrome smoke 均不是 QA 本轮实测，不据此扩大 GUI、打包或真实个人数据环境的通过范围。

### 最新导出路径增量回归

Engineer 在上述 63/135/225 复验后又补充了一项真实 HTTP 导出路径测试及默认开发模式导出断言。QA 仅复跑该增量所在聚焦套件和必要回归，未扩大生产验收范围：

| 范围 | QA 最新实测 |
|---|---|
| `test_runtime.py + test_qa_l01_runtime.py` | `64 passed in 21.10s`，pytest 退出码 `0` |
| 隔离面板全套 | `136 passed, 1 warning in 29.88s`，退出码 `0` |
| 主仓 `pytest -q` | `225 passed in 25.38s`，退出码 `0` |

新增覆盖确认：显式独立 data-dir 下 JSON/CSV 均真实存在，`files_absolute` 仅声明这两个实际 runtime exports 文件，旧 `files` 相对字段保持兼容，JSON 内容、CSV BOM 和路径白名单符合契约；构造任意文件导出子路径、带 path 的 GET 及 users.json 路径均不能读取文件。默认开发模式的实际路径断言也在隔离合成数据根中通过。

失败 reset 仍不建档，成功 undo 的 history 持久化保持通过。最新三组也均出现宿主 safe-delete `atexit` 清理警告，但 pytest 主退出码均为 0，无测试失败。最终路由保持 **Send To: NoOne**，真实环境未测限定不变。
