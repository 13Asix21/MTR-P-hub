# MTR-P 审核工作台在线版

## 当前交付边界

本目录是零构建、无运行时 CDN 的静态浏览器审核工作台。用户主动授权一个本地目录后，页面只读扫描 `2-Permanent` 等 managed roots 内的 Markdown，支持筛选、原文详情、通过/拒绝/跳过/撤销、理由、快捷键、单批不超过 500 篇，以及 JSON+CSV 浏览器下载。

**“在线版”指静态页面形态，不代表账号或云同步。** 当前身份只有“本地浏览器档案”；审核记录、目录句柄和导出备份只保存在当前 Origin 的 IndexedDB。没有 Auth、跨设备同步、云台账、云 SDK、服务端扫描或自动发布。审核不会改写 Vault 原文。

## 本地使用

不要双击 `index.html` 以 `file://` 打开；ES modules、Service Worker、持久化授权需要 HTTP(S) Origin。

```bash
# 从仓库根目录启动仅回环的静态服务器（已修正 Windows 下 .mjs 的 MIME）
./.venv/Scripts/python.exe apps/connect-panel/online/serve.py --port 8080
```

不要用普通的 `python -m http.server` 替代：部分 Python/Windows 环境会把 `.mjs` 返回为 `text/plain`，导致浏览器拒绝加载 ES module，页面停在“正在检查浏览器目录读取能力”。`serve.py` 固定监听 `127.0.0.1`，并设置 `.js/.mjs/.webmanifest/.css/.svg/.png` 的正确 Content-Type。

当前实测环境使用本机 Chrome；Edge 等具备 File System Access API 的桌面浏览器尚未单独验收。打开 `http://127.0.0.1:8080/`，点击“选择 Vault 目录”并在 Chrome 授权对话框中选择所需 Vault，授予**只读**访问。选错目录、取消授权或权限失效时不要继续审核，按页面提示重新选择。其他浏览器应显示能力边界，不以模拟句柄冒充可用。键盘焦点不在输入框时，J/K 导航，A/R/S 审核，U 撤销。

- 每次审核和撤销都会现读文件字节并计算 SHA-256；详情之后内容变化会拒绝旧 SHA 操作，已读队列不会被清空。
- `INVALID_YAML` 仍进入分母，允许人工查看只读原文并审核。
- 导出先基于当前状态 dry-run，再由浏览器下载 JSON+CSV；导出文件在浏览器下载目录，不在 Vault 内。
- 刷新后可按页面提示恢复目录授权和当前 IndexedDB 台账。浏览器清理站点数据、隐私模式限制或 IndexedDB 错误可能造成记录不可用；页面必须提示，不能伪报已保存。
- “清除本地台账”有两次明确确认，只删除本应用的 `dir_handle / pending_actions / export_backup`，不删除 Vault 文件、其他 Origin 数据或浏览器下载。

## 隐私与安全

1. 正文只在浏览器内读取和展示，不上传；Service Worker 不缓存 API、Vault、正文、Blob、导出内容或 IndexedDB 台账。
2. PWA 只按当前 scope 的精确相对路径缓存白名单静态 shell/vendor。升级不删除 IndexedDB。
3. 目录句柄只能由用户授权获得，网页不能任意枚举磁盘；测试中的 OPFS `test-vault` 与注入只读句柄不是生产目录选择器。
4. 清除浏览器站点数据会删除本地台账。跨浏览器、跨浏览器档案、跨 Origin、跨设备均不共享记录。
5. 页面不提供任意文件下载接口，也不写本地服务器磁盘。浏览器自身的下载权限、操作系统账户和磁盘安全仍由用户负责。
6. 发布到共享/公共 Origin 前应单独评审同源其他应用：同源脚本可能访问相同 IndexedDB、OPFS 和 Service Worker 存储。

## 静态部署

可部署本目录中的静态文件，不需要 Node/npm 构建或后端。生产环境必须使用 HTTPS，或在开发时使用可信 `127.0.0.1` 回环地址；普通局域网 HTTP 不足以支持所需的浏览器能力。

服务器至少应正确返回：

| 文件类型 | Content-Type |
| --- | --- |
| `.html` | `text/html; charset=utf-8` |
| `.js`、`.mjs` | `text/javascript` 或 `application/javascript` |
| `.webmanifest` | `application/manifest+json` 或合法 JSON 类型 |
| `.css` | `text/css` |
| `.svg` | `image/svg+xml` |
| `.png` | `image/png` |

`sw.js` 是模块 Service Worker，必须允许其静态 import 的 JS 文件以正确 MIME 同源加载。不要配置跨源 CDN、运行时安装脚本或把 API 路由反向代理到本目录。首次加载和 SW 更新需要网络；只有已成功安装并缓存白名单 shell 的同一 Origin 才能离线重新打开。

**本轮没有执行公开部署，也没有公网 HTTPS 安装、系统级目录选择器、真实个人 Vault 或非 Chrome 浏览器验收。** 部署候选不等于公开链接已发布。

## 验证与测试边界

```bash
# 已有 Node 环境：纯引擎、状态机、存储失败和静态边界
node --test apps/connect-panel/online/parity/tests/*.test.mjs

# 在已安装 Playwright Python 包和本机 Chrome 的环境运行；不下载浏览器
./.venv/Scripts/python.exe apps/connect-panel/online/parity/browser_smoke.py --channel chrome --overall-timeout 180 --phase-timeout 30
```

浏览器脚本只向当前测试 Origin 的 OPFS `test-vault` 写两篇合成 Markdown，并注入只读目录句柄，**绝不选择或读取真实个人 Vault**。脚本默认整体超时 180 秒，阶段等待上限 30 秒；普通失败由 `ExitStack` 清理 context/browser/server，整体超时由父 watchdog 尝试终止测试进程树，Windows 无法绑定 Job 时回退终止直接子进程。失败、缺失依赖或超时均非通过。Windows 嵌套 Job、强制回收全部 Chrome 后代及驱动断连尚未实测，不能由正常退出或桩测试外推。

本次 Engineer 使用已有 Playwright 环境复测：真实 Chrome 完整执行并记录 11 组断言，`page_errors=[]`，Node 66 项通过。这些结果不替代独立 QA 对最终交付的复验。主仓当前 `.venv` 未安装 Playwright，不能据此重放 Chrome 结果或安装系统浏览器。

已知测试命令退出可能受宿主 safe-delete 临时目录清理警告影响；应读取 pytest 主退出码，不得关闭安全保护或把警告改写为业务断言失败。
