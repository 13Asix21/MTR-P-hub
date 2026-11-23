# MTR-P 乐享代理

把审核通过的文件写入乐享知识库的独立 HTTP 服务。**它必须部署为独立应用**，因为浏览器无法直连乐享 MCP（CORS 被拦），而 HTTPS 页面也不能调用本机 127.0.0.1 端口。

- 独立应用：`https://mtrp-lexiang-proxy.app.workbuddy.host/?sharecode=QOPTB45RQL`
- 健康检查：`GET /mtrp/health`
- 传送：`POST /mtrp/transfer`，`Authorization` 通过服务端持有，前端不接触

## 配置

在**本目录**放 `.env`（已被 `.gitignore` 排除，不会入库）：

```ini
MTRP_LEXIANG_TOKEN=<乐享 MCP token>
MTRP_LEXIANG_SPACE_ID=<目标知识库空间 ID>
```

`MTRP_ALLOWED_ORIGINS` 可覆盖允许的调用来源，默认包含审核工作台域名。

## 前端怎么填

在审核工作台的「在线设置 → M 库」填三项：

| 字段 | 值 |
| --- | --- |
| 空间 ID | 目标乐享知识库空间 ID |
| 目标路径 | 知识库内的相对路径，逐级自动建文件夹 |
| 代理地址 | `https://mtrp-lexiang-proxy.app.workbuddy.host` |
| 代理 sharecode | `QOPTB45RQL`（代理分享链接里的那串） |

**sharecode 必须用代理自己的**，不能用审核页地址栏里的那个——跨应用不通用，用错会返回 403。

## 行为

1. 按目标路径逐级查找/创建文件夹；
2. `entry_create_entry` 建 page 条目；
3. `block_update_page` 写入正文（已剥离 frontmatter）。

任何一步失败都返回 `{"ok":false,...}` 与对应 HTTP 状态，不做静默降级。

## 乐享写入的容量与结构边界（实测）

乐享的 `block_update_page` 对正文有硬限制，实测结论：

| 情况 | 结果 |
| --- | --- |
| 单段/小块 Markdown | 通过 |
| 含水平分隔线 `---` `***` `___` `----` | **拒绝**（`structurally unstable after round trip`） |
| 整篇 16KB 复杂文档（表格+代码块+分隔线） | **拒绝**，逐段累加也只能接受 24/119 段 |
| 约 900 字符的普通笔记（9 段） | **全部接受**，回读内容一致 |
| `append_content` 命令 | 该命令不存在 |
| `update_content` | 服务端规范化后 `old_str` 无法匹配，不可靠 |

代理的处理：

1. 写入前把水平分隔线替换为 `———`（中文破折号，乐享接受且语义等价）；
2. 按空行切段，逐段累加并每步整篇提交，直到全部写完；
3. 返回 `segments` / `accepted` / `chars`，前端在 `accepted < segments` 时明确提示“仅部分段落被接受，建议拆分”。

**实践建议**：单篇控制在约 1000～2000 字符内最稳。长文档先拆分，或只在 M 库存摘要 + 原文留在 Obsidian。
