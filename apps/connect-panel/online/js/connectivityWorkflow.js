/**
 * 内部联通工作流：按固定步骤检查 M / T / R / P 的真实可达性。
 *
 * 设计原则：每一步要么拿到真实回执，要么明确失败原因，不做“看起来通过”的假成功。
 * - M 库：乐享，经代理自检（需要服务端持有 token）
 * - T/R 库：Obsidian，本地目录授权 或 MCP 端点，二选一
 * - P 库：GitHub 公开 API 直连
 * - Agent：MatrixOne Agent 网关（可选，用于后续智能归类）
 */

export const STEP_STATE = Object.freeze({
  pending: 'pending',
  running: 'running',
  ok: 'ok',
  failed: 'failed',
  skipped: 'skipped',
});

/** 固定步骤顺序，界面上按这个顺序展示。 */
export const WORKFLOW_STEPS = Object.freeze([
  { id: 'm', label: 'M 库 · 乐享知识库' },
  { id: 't', label: 'T 库 · Obsidian 试运行区' },
  { id: 'r', label: 'R 库 · Obsidian 冷区' },
  { id: 'p', label: 'P 库 · GitHub 项目资产' },
  { id: 'agent', label: 'MatrixOne Agent（可选）' },
]);

function trim(value) {
  return typeof value === 'string' ? value.trim() : '';
}

/** 校验 M 库配置是否具备自检条件。 */
export function checkM(config) {
  if (!config) return { state: STEP_STATE.failed, message: '未配置 M 库。' };
  if (!trim(config.space_id)) return { state: STEP_STATE.failed, message: '缺少乐享知识库 ID。' };
  if (!trim(config.proxy_url)) return { state: STEP_STATE.failed, message: '缺少代理地址。' };
  if (!trim(config.sharecode)) return { state: STEP_STATE.failed, message: '缺少代理访问码。' };
  return { state: STEP_STATE.ok, message: '配置完整，待自检。' };
}

/**
 * T/R 双模式：`local` 本地文件夹（浏览器目录授权）或 `mcp` 远端 MCP 端点。
 * 两种模式都不允许写回，只是读取来源不同。
 */
export function checkVault(config) {
  if (!config) return { state: STEP_STATE.skipped, message: '未配置，跳过。' };
  const mode = trim(config.mode) || 'local';
  if (mode === 'local') {
    return { state: STEP_STATE.ok, message: `本地模式：扫描范围 ${trim(config.vault_root) || '整个库'}（需在本机授权文件夹）。` };
  }
  if (mode === 'mcp') {
    if (!trim(config.mcp_endpoint)) return { state: STEP_STATE.failed, message: 'MCP 模式缺少端点地址。' };
    if (!/^https?:\/\//u.test(trim(config.mcp_endpoint))) return { state: STEP_STATE.failed, message: 'MCP 端点必须以 http:// 或 https:// 开头。' };
    return { state: STEP_STATE.ok, message: `MCP 模式：${trim(config.mcp_endpoint)}，待连通测试。` };
  }
  return { state: STEP_STATE.failed, message: `未知模式：${mode}` };
}

export function checkGithub(config) {
  if (!config) return { state: STEP_STATE.skipped, message: '未配置，跳过。' };
  if (!trim(config.owner) || !trim(config.repo)) return { state: STEP_STATE.failed, message: '缺少仓库信息。' };
  return { state: STEP_STATE.ok, message: `待测：${trim(config.owner)}/${trim(config.repo)}` };
}

/** 把检查结果汇总成可展示的列表。 */
export function summarize(results) {
  const total = results.length;
  const ok = results.filter((item) => item.state === STEP_STATE.ok).length;
  const failed = results.filter((item) => item.state === STEP_STATE.failed).length;
  const skipped = results.filter((item) => item.state === STEP_STATE.skipped).length;
  return { total, ok, failed, skipped, allPassed: failed === 0 && ok > 0 };
}
