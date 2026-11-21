# MTR-P 四库只读连接面板

这是位于 `apps/connect-panel/` 的独立本地面板，用来加载主仓配置，对 M / T / R / P 四库做能力边界明确的连接校验，并提供 Vault 进入 M 库前的人工审核工作台。后端使用 FastAPI，前端是零 CDN、零 npm、零构建步骤的单文件 HTML。审核工作台只产生审核台账与放行清单，不执行发布。

## 双形态交付契约（当前实测）

| 形态 | 当前交付 | 台账与边界 | 已验收证据 |
| --- | --- | --- | --- |
| 本地完整版 | FastAPI 本地面板，及 `packaging/dist-v1/MTRP-ReviewWorkbench/` Windows 便携目录 | 本机回环服务，无登录认证；台账在明确的数据目录。保留 M/T/R/P 面板与审核导出，不改写 Vault | 独立 QA 最新 **64 / 136 / 225 项**通过、NoOne；主线程真实便携 EXE 扫描—审核—导出 smoke 通过 |
| 浏览器静态版 | `online/` 零构建静态审核工作台，可回环启动或另行部署 HTTPS | **本地浏览器档案，不是账号登录；无 Auth、云台账或跨设备同步**；正文不上传，Vault 只读 | 主线程真实 Chrome **11/11 组**完整断言、Node **66 项**、50 篇双引擎对拍差异 **0** |

两者代码、存储和运行入口独立，不自动合并进度。`docs/prd-online-workbench.md` 是原完整在线版目标；其中云账号/云同步、真实 Vault 全量对拍、真实系统目录选择器和公开 HTTPS 安装**尚非本轮已交付事实**，以本节和 `online/README.md` 的实际边界为准。公开部署须另行明确授权，本轮未发布公网链接。便携目录不是安装器，真实升级保留、安装卸载和跨机器仍待验。

## 启动

必须从主仓根目录使用项目已有的 Python 3.11 虚拟环境：

```bash
./.venv/Scripts/python.exe -m pip install -r apps/connect-panel/requirements.txt
./.venv/Scripts/python.exe apps/connect-panel/run_panel.py
```

默认自动打开 `http://127.0.0.1:8765/`，监听地址固定为 `127.0.0.1`，没有 `0.0.0.0` 覆盖项。默认源码开发模式继续读取主仓 `config/` 和面板 `data/users.json`，原档案不会自动搬迁。常用启动参数：

```bash
# 改固定端口，不自动打开浏览器
./.venv/Scripts/python.exe apps/connect-panel/run_panel.py --port 8766 --no-browser

# 使用独立数据目录；0 由操作系统分配空闲端口，控制台输出实际 URL
./.venv/Scripts/python.exe apps/connect-panel/run_panel.py --data-dir "E:/MTRP-local-data" --port 0 --no-browser
```

`--data-dir` 必须是本机绝对路径，台账和导出分别写入该目录的 `users.json` 与 `exports/`。显式新目录优先读取其 `config/`；没有完整配置时使用随面板提供的 `panel/default_config/` 安全模板，不回读主仓的私人路径。无旧档时首次启动建立空档案，页面提示填写并保存 Vault 绝对路径，不自动扫描。

发现旧版 `apps/connect-panel/data/users.json` 时，页面提供“确认复制旧版档案”。只有显式确认后才读取、完整校验并复制到新目录，旧文件保持原字节，已有新档案绝不覆盖；迁移待决时启动、`health/users/overview` 和审核读取不创建目标，`users` 返回空列表，`overview` 只返回内存默认快照。选择“暂不迁移”、开始新增用户/保存偏好/提交审核会明确建立独立新档，此后不可再迁移覆盖。该防护是本机回环安全边界，不是登录认证。

迁移临时文件与目标位于同一文件系统，使用 `os.link(temp, target)` 原子发布；目标已存在返回 409，硬链接不支持或权限失败返回写入失败，均不覆盖或按大小删除目标，也不伪报迁移成功。清理只针对自有临时路径。当前测试覆盖确定性竞争和多线程一方成功一方 409，不宣称已覆盖多进程压力、目录 fsync、掉电恢复或所有目标平台。

也可用环境变量 `MTRP_PANEL_DATA_DIR` 指定数据根，`MTRP_PANEL_CONFIG_DIR` 显式指定另一份配置副本，`MTRP_PANEL_LEGACY_PROFILE` 为安装器/测试指定绝对旧档路径（默认仍为原开发档案位置）；优先级为启动参数/环境数据根 > 数据根 `config/` > 默认源码开发主仓配置（仅未显式隔离时）> 安全模板。面板依赖仅写在本目录 `requirements.txt`，不进入主仓 `pyproject.toml`。

`GET /api/health` 可检查 `ready/local_only` 与迁移状态，不返回本机数据绝对路径；`POST /api/runtime/migrate` 用 `{"confirm":true}` 显式复制旧档，用 `{"use_independent":true}` 明确建立独立新档，两者都不会删除旧档。

## Windows 便携目录交付

`packaging/dist-v1/MTRP-ReviewWorkbench/` 是包含 EXE、`_internal/` 运行时与静态资源的 **Windows x64 便携目录**，不是单个可独立运行的 EXE，也不是安装器。交付时保留整个 `MTRP-ReviewWorkbench/` 目录及内部相对结构，然后运行其中的 `MTRP-ReviewWorkbench.exe`。不要只复制 EXE 或移动 `_internal/`。当前产物未签名，没有安装向导、卸载程序、自动更新或跨机器兼容性验收。

- 直接启动默认监听 `127.0.0.1:8765` 并打开本机浏览器；也支持前述 `--port`、`--no-browser` 参数。例如：

  ```powershell
  .\MTRP-ReviewWorkbench.exe --port 8766 --no-browser
  ```

- 未传 `--data-dir` 或 `MTRP_PANEL_DATA_DIR` 时，便携版台账和导出位于 Windows 每用户目录 `%LOCALAPPDATA%\MTRP-ReviewWorkbench\`，不是开发仓 `data/`。可把已有 `config/` 配置副本放在该数据根；未提供时使用随包安全模板。
- 建议显式指定一个可写的绝对数据目录，便于备份和明确升级边界：

  ```powershell
  .\MTRP-ReviewWorkbench.exe --data-dir "E:\MTRP-local-data" --port 8766 --no-browser
  ```

  其 `users.json` 保存用户与审核台账，`exports/` 保存正式 JSON/CSV 导出；仍需在页面填写并保存待扫描的 Vault 绝对路径，再显式点击扫描。应备份整个数据目录，审核不会改写 Vault 原文。
- 便携版启动器将旧版档案候选设为程序目录下的 `data/users.json`。检测到有效旧档时，只有页面明确“确认复制旧版档案”才复制到当前数据目录；不会覆盖已有目标或删除源文件。“暂不迁移”或开始新的本地写入会建立独立档案。程序目录、数据目录和 Vault 是三套不同位置，不应混用。
- 升级时退出旧程序，另建目录完整解压新版，再使用同一个数据目录启动；不要用旧版覆盖正在运行的目录。**目前只通过独立数据根、合成 Vault、中文/空格路径的 EXE 扫描—审核—导出 smoke；从旧便携目录升级后保留真实台账与配置尚未实测。** 升级前务必备份数据目录，实际交付候选仍需验收升级保留、安装/卸载边界和目标机器环境。

### 从源码构建与验收

构建使用 `packaging/` 下已有的独立 Python 3.11 环境，不污染主仓 `.venv`；首次准备该环境时需安装 `requirements.txt`、PyInstaller 以及可导入的主仓 `src/` 包。构建前先完成本文件所列面板和主仓回归，再从仓库根执行：

```bash
apps/connect-panel/packaging/.venv/Scripts/python.exe -m PyInstaller apps/connect-panel/packaging/mtrp_review_workbench.spec --distpath apps/connect-panel/packaging/dist-v1 --workpath apps/connect-panel/packaging/build-v1 --clean
```

对已有输出目录不要通过绕过宿主保护的方式批量删除；应使用新的构建目录，确认候选通过后再保留旧目录并替换交付副本。`Build complete!` 只表示打包完成，不是 EXE 验收。必须对最终目录中的真实二进制运行：

```bash
./.venv/Scripts/python.exe apps/connect-panel/packaging/smoke_test.py apps/connect-panel/packaging/dist-v1/MTRP-ReviewWorkbench/MTRP-ReviewWorkbench.exe
```

该 smoke 只创建临时合成 Vault 和独立数据根，验证回环 health、扫描、当前 SHA 审核、导出文件及 Vault SHA 不变，最后关闭 EXE。它不读取真实个人 Vault，也不替代上述升级保留、签名、跨机器或安装器验收。

## 多用户档案层预留

无迁移待决的首次启动或档案缺少 default 时会自动初始化/补建当前数据目录的 `users.json`（默认源码模式为 `apps/connect-panel/data/users.json`）；本目录 `.gitignore` 排除整个 `data/`，不会修改仓库根 `.gitignore`。`users.json` 只有注入的 `ProfileStore` 单例持锁写入，采用 UTF-8、临时文件、`fsync` 和原子替换；另一处允许写入的位置是同一数据根下的 `exports/`。安全模板首次创建的 Vault 偏好为空字符串，表示待设置；保存偏好或执行扫描仍要求本机绝对路径。default 用户初始内容示例（实际路径会经 `pathlib` 归一化）：

```json
{
  "schema_version": "1",
  "users": [
    {
      "user_id": "default",
      "display_name": "本机默认用户",
      "role": "owner",
      "preferences": {
        "vault_root": "C:/Users/example/Obsidian Vault",
        "git_local_path": "C:/Users/example/MTR-P-hub",
        "github_repos": [{"owner": "13Asix21", "repo": "MTR-P"}],
        "auto_refresh_minutes": 0
      },
      "review": {
        "schema_version": "1",
        "items": {},
        "history": [],
        "last_position": null
      }
    }
  ]
}
```

新用户 ID 由服务端生成，格式限 `[a-z0-9-]{1,32}`；新用户偏好复制 default 的**当前值**。个人 Vault 路径、本地 Git 路径、关注仓库与刷新开关按用户档案隔离。审计库 `decision-log.sqlite3` 和 `config/` 配置是机器级共享事实，所有用户读取同一份，不做按用户过滤。

本期不做登录、密码、token、会话或多租户资源隔离。`owner / editor / viewer` 仅存储和展示，不强制执行权限；数据模型和 API 仅预留未来接入 WorkBuddy cloud-service 用户体系的位置，接入认证后才启用角色权限。`auto_refresh_minutes` 可空；0 表示关闭，开启时允许 5–60 分钟，避免高频请求烧尽 GitHub 未认证额度。

## 四库功能口径

| 库 | 校验方式 | 状态解释 |
| --- | --- | --- |
| M | 校验 `stage2.yaml` 的 `workbuddy-manifest`、三个 32 位十六进制 ID 与 `read_only=true`；用 SQLite `mode=ro` 读取 store=`M` 最近事件及最近 run | 配置有效也固定为 `limited`：独立后端不能实时调用 WorkBuddy 会话级 MCP，只提供配置和历史执行证据 |
| T | 校验本地绝对目录、`.obsidian/` 和 managed roots；只读扫描 `.md`，统计 `mtr/trial`、无 MTR 标签、双标签冲突等 | 实文件只读扫描；达到 5000 文件上限或存在局部读取错误时为 `limited` |
| R | 与 T 共用同一次扫描，独立展示 `mtr/rarely` 数量 | 卡片带“预留”；T↔R 下沉/复活提案属于阶段 3，面板不执行生命周期操作 |
| P 在线 | 无凭据直连 `GET /repos/{owner}/{repo}` 和 `GET /repos/{owner}/{repo}/commits/{default_branch}` | 显示真实默认分支、commit、推送时间和毫秒延迟；404、限流、超时、网络错误均如实返回 `error` |
| P 本地 | 只读执行 `status --porcelain=v2 --branch`、`remote get-url origin` | 显示 `.git`、分支、HEAD、dirty 文件数和脱敏 origin URL；设置 `GIT_OPTIONAL_LOCKS=0`，绝不 pull / push / checkout |

T/R 分类互斥：同时具有 trial 和 rarely 的笔记只进入“双标签冲突”，不重复进入 T 或 R 数量。Markdown 解析复用 editable 安装的主仓 `mtrp_hub.adapters.obsidian_markdown.ObsidianMarkdownParser`，包括 frontmatter、行内标签、代码排除、UTF-8 BOM 和 CRLF 语义；解析器不可导入时返回明确的 `PARSER_UNAVAILABLE`，不会崩溃或改用静默的伪造结果。

## 审核工作台（review_workbench）

顶部“审核工作台”tab 不会在切换时自动扫描；必须显式点击“扫描并载入待审清单”。扫描采用 `vault_check._scan_vault_once` 单遍引擎：同一批 `on_file` 回调同时产生 v1 同键 `statistics` 与逐篇 `items[]`，不是先聚合再扫第二遍。wiki 反链只在遍历后基于内存索引计算；详情、写动作和导出允许单文件现算，不构成全树重扫。

- 队列 = managed roots 内实际扫描到的全部 `.md`，含 `INVALID_YAML` 等异常篇；排除目录不入队。当前真实 Vault 的 2026-11-19 实测为 1832 = 1820 可解析 + 12 异常，该数字不是硬编码，Vault 变化后以新扫描为准。
- 每条清单含 NFC POSIX 相对路径、原始字节 SHA-256、字节数、mtime、原始标签、MTR 状态、解析状态与出/反链。解析失败篇 `mtr_state="unknown"`；`INVALID_YAML` 可按原文预览，且通过/拒绝/跳过均不受限。
- 详情现算 SHA，返回 frontmatter 原文和有序键值、正文、文件元信息与用户结论。正文只在本次响应出现，不写 users.json、不写日志；前端用白名单 DOM 渲染受限 Markdown，原文始终可切换。
- 单篇动作必须携带客户端所见 SHA。服务端重新读取原始字节，不一致即 `CONTENT_CHANGED(409)`，不落记录、不追加历史。审核记录只保存动作、可选理由、内容 SHA 与 UTC 时间，不保存正文。

### 进度、状态与多用户

每用户 `review` 独立保存 `schema_version / items / history / last_position`；未审就是 items 中无键。历史包含 `DECIDE / UNDO / STALE_RESET`，保留最近 200 条；理由最多 500 字。档案读取上限为 10MB，损坏的 review 返回 `REVIEW_RECORD_INVALID`，不静默重建。

进度唯一口径：`total = 当前清单篇数`；`reviewed = approved + rejected + skipped`；`pending = total - reviewed`；通过率 = approved ÷ reviewed（reviewed=0 时为 null）。STALE 是“记录 SHA 与当前扫描快照 SHA 不一致”的派生态，不落盘、不计入已审，也不减少分母；详情和导出会进一步单文件现算复核。脱队历史记录不掺入当前进度。

角色仍不用于登录鉴权，但 owner 可查看只读全局汇总、按用户分解和分歧清单；非 owner 的 `global` 为 null。同一篇多人结论不同只标记分歧，不自动合并。`last_position` 支持续审，重扫后失效时前端回退首篇。

### 批量与目录覆盖率

批量请求由前端按当前筛选或目录显式组装，每篇带所见 SHA；超过 500 时前端分批，服务端若单批收到 501 篇以上则整批返回 `REVIEW_BATCH_LIMIT(413)`，不隐式截断。批内缺文件、越出清单、超限或 SHA 变化逐篇报错，正常篇在同一锁内逐篇落记录并只做一次档案原子替换；摘要单列 `INVALID_YAML` 成功/失败数。`progress?view=directory` 按文件直接父目录给出互不重叠的覆盖率，目录 total 之和等于清单 total。

### 导出格式

默认 `include=approved` 只导出动作仍为 approved 且导出时现算 SHA 与记录一致的篇目；stale 被剔除并计入 `excluded_stale`。`include=all` 导出全部已决篇并给每项增加 `stale: true|false`，不剔除。`dry_run=true` 只预览，不创建文件。

正式导出在当前数据目录的 `exports/`（默认源码模式为 `apps/connect-panel/data/exports/`）生成同 stem 的 JSON 与 CSV：`review-<user_id>-<YYYYMMDD-HHMMSS>(-n?).json|csv`，同秒重名自动追加序号。JSON 顶层含 `schema_version / exported_at / user_id / vault_id / scope / counts / items`；`vault_id` 是 resolved Vault 绝对路径 SHA-256 的前 16 位。CSV 使用 UTF-8 BOM、CRLF 和 RFC4180 引号规则，tags 以分号连接，数据行数与 JSON items 严格一致（另有表头）。写盘使用双临时文件、`fsync` 和替换，失败清理本次半套产物。**不会自动删除历史导出**；需要控制份数时由人工清理，整个 `data/` 均不入库。

## API

所有响应统一包裹为：

```json
{"status": "ok", "data": {}}
```

或：

```json
{"status": "error", "error": {"code": "封闭错误码", "message": "安全中文说明"}}
```

`data.status` 是卡片检测状态，固定使用 `ok / limited / error / unconfigured / pending`；它与外层请求是否成功是两个维度。

- `GET /`：返回 `static/index.html`。
- `GET /api/users`：`data` 直接返回用户数组，每项包含 user_id、display_name、role 和完整 preferences；不包含任何认证信息。
- `POST /api/users`：请求体 `{"display_name":"本机观察者","role":"viewer"}`，role 缺省 viewer；服务端生成 ID 并复制 default 当前偏好。
- `PATCH /api/users/{user_id}/preferences`：局部更新 `vault_root / git_local_path / github_repos / auto_refresh_minutes`，未知字段、非法 ID、未知用户均返回封闭错误。
- `GET /api/overview?user_id=default`：返回该用户的四库预填路径、仓库坐标、刷新偏好及 pending 摘要；不扫描、不联网。
- `POST /api/check/vault`：可传 `user_id` 和可选 `path`，缺省取该用户偏好；显式 path 优先。`managed_roots / excluded_roots / trial_tag / rarely_tag` 仍可覆盖，缺省采用 `stage2.yaml`。仅接受本地绝对路径，拒绝 URL、UNC 与相对路径。
- `POST /api/check/github`：可传 `user_id / owner / repo`，缺省仓库取该用户首项 GitHub 偏好，显式 owner/repo 优先。
- `POST /api/check/git-local`：可传 `user_id / path`，显式 path 优先，缺省取该用户偏好。
- `POST /api/check/lexiang`：body 可传 `{"user_id":"default"}`（也兼容同名 query），校验用户是否存在；M 配置与审计证据仍为机器级共享事实。
- `GET /api/audit/recent?limit=10`：返回最近事件白名单字段，`limit` 范围 1–100；不按用户过滤。

原检查端点的 `user_id` 均缺省 `default`。“校验此路径”只发起临时检查，只有前端“保存为该用户偏好”按钮或显式 PATCH 才写入 users.json。

审核端点如下，均走同一包裹、严格请求模型与封闭错误边界；写动作是面板自管档案动作，不是 Vault 写入：

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/review/scan` | 单遍全量重扫并替换进程内单槽缓存，同时返回 statistics、counts 与 items |
| `GET /api/review/listing` | 读取当前扫描快照；空缓存返回 `REVIEW_SCAN_REQUIRED` |
| `GET /api/review/item?user_id&path` | 单篇现算详情、frontmatter、正文、链接、用户结论与精确 stale |
| `POST /api/review/decide` | 通过/拒绝/跳过；请求 SHA 与服务端现算不一致则拒绝写入 |
| `POST /api/review/reset` | `undo` 或 `stale_reset`，删除当前键、保留前值历史并回到未审 |
| `GET /api/review/records?user_id&history_limit` | 当前用户完整 items、逆序历史与 last_position，不需要扫描缓存 |
| `POST /api/review/position` | 设置或清除当前用户续审位置 |
| `GET /api/review/progress?user_id&view` | `summary` 用户/owner 全局汇总；`directory` 另含目录覆盖率 |
| `POST /api/review/batch` | 单批最多 500 篇，逐篇预检、一次锁内落账、逐篇结果与异常摘要 |
| `POST /api/review/export` | dry-run 预览或正式生成 JSON+CSV；`include=approved|all` |

新增专用错误码共 10 个：

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `REVIEW_SCAN_REQUIRED` | 409 | 尚未扫描，缓存为空 |
| `REVIEW_PATH_INVALID` | 400 | 路径含逃逸、绝对路径、URL、UNC、盘符、ADS 或非法段 |
| `REVIEW_PATH_NOT_SCANNED` | 404 | NFC 归一后仍不在当前清单 |
| `REVIEW_FILE_MISSING` | 404 | 清单成员已从磁盘消失或不可读 |
| `REVIEW_ITEM_NOT_DECIDED` | 404 | 撤销/重置目标没有在档结论 |
| `CONTENT_CHANGED` | 409 | 所见 SHA 与服务端现算不同，拒绝审核写入 |
| `FILE_TOO_LARGE` | 413 | 超过 max_file_bytes，拒绝读取完整正文 |
| `REVIEW_BATCH_LIMIT` | 413 | 单批超过 500，整体拒收 |
| `REVIEW_RECORD_INVALID` | 503 | 审核档案结构损坏，拒绝操作 |
| `EXPORT_WRITE_FAILED` | 500 | 导出写盘失败，本次临时/半套文件已清理 |

## 只读与安全边界

- 不修改或删除 `src/mtrp_hub/`、`config/`、`pyproject.toml`，不把它们作为面板输出目录。
- 不调用 MCP，不读取或导出 WorkBuddy 凭据，不发送带 token 的 GitHub 请求；GitHub 请求显式带 User-Agent，单次超时 10 秒。
- 对 Vault 只执行目录枚举和小文件二进制读取；symlink / junction / Windows reparse point 一律跳过，不跟随目标。
- 排除规则按完整路径段前缀匹配：排除 `A/B` 不会误排 `A/B2`。
- 只读 UTF-8 / UTF-8 BOM 文件，支持中文路径；大于 `max_file_bytes` 的文件跳过，解析错误仅返回相对路径与封闭错误码。
- SQLite 采用 URI `file:...?mode=ro&immutable=1`（`uri=True`）和 `PRAGMA query_only=ON`，不建表、不更新、不切换 journal 模式，也不写 WAL 共享内存。若存在非空活动 WAL，面板返回 `AUDIT_WAL_ACTIVE` 并拒绝读取，而不是写 SHM 或展示可能陈旧的证据。
- Git 使用参数数组、`shell=False`、禁用终端提示和可选锁；origin URL 去除用户信息、查询和 fragment，避免意外展示凭据。
- 前端所有动态值使用 `textContent` 渲染，不把路径、远端或审计内容注入 HTML；API 不向客户端返回 traceback 或底层异常全文。
- 默认没有自动轮询。P 在线的“自动刷新（5 分钟）”由用户显式开启，页面隐藏时暂停，避免消耗未认证每小时限流。

## 文件职责

- `run_panel.py`：固定回环地址、独立数据目录、动态端口与就绪后浏览器启动入口。
- `panel/runtime.py`：数据目录与配置优先级、显式旧档案迁移、Host/Origin/写请求安全校验。
- `panel/default_config/`：不含本机私人路径的四份安全 YAML 模板。
- `panel/settings.py`：只读 YAML 配置、共享数据类与本地路径校验。
- `panel/vault_check.py`：Obsidian 识别、链接安全扫描与单遍 `_scan_vault_once` 引擎。
- `panel/review_scan.py`：同遍 statistics+items 双口径收集、wiki 反链、单篇安全读取与现算详情。
- `panel/review_store.py`：零 IO 审核状态、历史、进度、全局分歧、目录覆盖率和导出项纯函数。
- `panel/review_api.py`：注入单例的 10 个审核端点、单槽扫描缓存与成对原子导出。
- `panel/api_common.py`：严格请求模型、用户 ID 模式和统一响应包裹。
- `panel/github_check.py`：GitHub 在线无凭据实测。
- `panel/git_local_check.py`：本地 Git 最小只读命令。
- `panel/lexiang_check.py`：M 配置和证据边界。
- `panel/audit_reader.py`：Decision Log 统一只读查询。
- `panel/user_profiles.py`：本地用户档案校验、review 深度校验入口、default 初始化和唯一锁内原子写者。
- `data/users.json`：运行时自管用户档案与审核台账，由本目录 `.gitignore` 排除。
- `data/exports/`：运行时审核 JSON/CSV 导出，仅面板自管，永不入库；历史文件不自动删除。
- `panel/main.py`：FastAPI 路由、统一包裹和异常兜底。
- `static/index.html`：全中文、浅色、原生 JavaScript 单文件前端，含四库卡片与审核工作台两个 tab。

## PRD 验收对照与测试

1. **清单与分母自洽**：`tests/test_review_workbench.py::test_01...` 同时断言 review/scan 单响应恒等式及 check/vault 与 review/scan 的 statistics 逐键相等；fixture 固定为 6/5/1、MTR 1/1/2/1。真实 Vault 分母以实际扫描为准。
2. **全流程 Vault 零写**：全流程用例前后执行全树 SHA-256 比对；所有审核写入只落 users.json，导出只落 data/exports。
3. **多用户隔离与 owner 汇总**：双用户对同篇分别通过/拒绝，验证 records 互不影响、global.conflicts 含双方结论、非 owner global=null。
4. **STALE 收敛**：篡改临时 Vault 副本后详情精确标 stale，decide 拒绝旧 SHA，默认导出剔除；`stale_reset` 保留历史并回到 pending。
5. **JSON/CSV 可独立消费**：校验 schema_version、scope、vault_id、items 字段、默认 approved、include=all 的 stale 标注、CSV BOM/CRLF 与数据行一致。
6. **异常篇全程可审**：INVALID_YAML 在队列、可筛、可按原文预览，单篇与批量动作不受限；同时覆盖路径逃逸、原子写失败不损档、批量部分失败和 NFC 记录键。

回归命令：

```bash
./.venv/Scripts/python.exe -m pytest apps/connect-panel/tests -q
./.venv/Scripts/python.exe -m pytest tests/ -q
```

当前独立 QA 实测为专项 **64 passed**、隔离面板 **136 passed**、主仓 **225 passed**，详见 `docs/qa-local-runtime.md`；在线版为 Node **66 passed**、对拍比较 **5 passed**、50 篇双引擎差异 **0**。各轮历史数字保留在 QA 报告中，不以早期全绿覆盖后续新增用例。
