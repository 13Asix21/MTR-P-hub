# MTR-P 四库只读连接面板

这是位于 `apps/connect-panel/` 的独立本地面板，用来加载主仓配置，并对 M / T / R / P 四库做能力边界明确的连接校验。后端使用 FastAPI，前端是零 CDN、零 npm、零构建步骤的单文件 HTML。

## 启动

必须从主仓根目录使用项目已有的 Python 3.11 虚拟环境：

```bash
./.venv/Scripts/python.exe -m pip install -r apps/connect-panel/requirements.txt
./.venv/Scripts/python.exe apps/connect-panel/run_panel.py
```

浏览器打开 `http://127.0.0.1:8765/`。可用 `--port` 改端口，例如：

```bash
./.venv/Scripts/python.exe apps/connect-panel/run_panel.py --port 8766
```

监听地址固定为 `127.0.0.1`，没有 `0.0.0.0` 覆盖项。默认配置目录为主仓 `config/`；仅为独立调试可用环境变量 `MTRP_PANEL_CONFIG_DIR` 指向另一份配置副本。面板依赖仅写在本目录 `requirements.txt`，不进入主仓 `pyproject.toml`。

## 多用户档案层预留

首次启动或档案缺少 default 时会自动初始化/补建 `apps/connect-panel/data/users.json`；本目录 `.gitignore` 排除整个 `data/`，不会修改仓库根 `.gitignore`。这是面板唯一允许写入的数据位置，采用进程内锁、UTF-8 和临时文件原子替换。default 用户初始内容示例（实际路径会经 `pathlib` 归一化）：

```json
{
  "schema_version": "1",
  "users": [
    {
      "user_id": "default",
      "display_name": "本机默认用户",
      "role": "owner",
      "preferences": {
        "vault_root": "C:/Users/Administrator/Nutstore/1/我的坚果云/Obsidian Vault",
        "git_local_path": "E:/ASoftwareManage/Agithub/MTR-P-hub",
        "github_repos": [{"owner": "13Asix21", "repo": "MTR-P"}],
        "auto_refresh_minutes": 0
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

- `run_panel.py`：固定回环地址的 Uvicorn 启动入口。
- `panel/settings.py`：只读 YAML 配置、共享数据类与本地路径校验。
- `panel/vault_check.py`：Obsidian 识别、链接安全扫描与 T/R 统计。
- `panel/github_check.py`：GitHub 在线无凭据实测。
- `panel/git_local_check.py`：本地 Git 最小只读命令。
- `panel/lexiang_check.py`：M 配置和证据边界。
- `panel/audit_reader.py`：Decision Log 统一只读查询。
- `panel/user_profiles.py`：本地用户档案校验、default 初始化和原子偏好更新。
- `data/users.json`：运行时自管用户档案，由本目录 `.gitignore` 排除。
- `panel/main.py`：FastAPI 路由、统一包裹和异常兜底。
- `static/index.html`：全中文、浅色、原生 JavaScript 单文件前端。
