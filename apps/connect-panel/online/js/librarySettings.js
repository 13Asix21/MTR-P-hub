/**
 * 四库连接的读取与保存。
 * 安全边界：云端只存「连接地址与标识」，绝不存 token / AppSecret；
 * 需要凭据的读取（乐享 M、私有 GitHub）必须走各自的服务端代理，不能把密钥放进浏览器。
 */
import { LIBRARIES } from './cloudClient.js?v=20261123k';

export const LIBRARY_IDS = LIBRARIES.map((item) => item.id);
export const TABLE = 'library_connections';

/**
 * token 类字段只允许留在本机，用于检测误填，不参与任何远端写入。
 * 用正则覆盖 access_token / github_pat / client_secret 这类变体，避免逐个枚举漏网。
 */
const SECRET_KEY_PATTERN = /(token|secret|passw|api[_-]?key|apikey|authorization|credential|(^|[_-])pat($|[_-]))/iu;

export class LibraryConfigError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'LibraryConfigError';
    this.code = code;
  }
}

function str(value) {
  return typeof value === 'string' ? value.trim() : '';
}

export function emptyConfig(library) {
  if (library === 'm') return { space_id: '', proxy_url: '', path: '', sharecode: '' };
  if (library === 't' || library === 'r') return { mode: 'local', vault_root: '', mcp_endpoint: '', managed_roots: [], tags: [] };
  if (library === 'p') return { owner: '', repo: '', branch: 'main', proxy_url: '' };
  throw new LibraryConfigError('LIBRARY_UNKNOWN', `未知库：${library}`);
}

function listOf(value, max) {
  if (!Array.isArray(value)) return [];
  return value.map(str).filter(Boolean).slice(0, max);
}

/** 校验并归一化；不合格直接抛错，不静默丢弃用户输入。 */
export function normalizeConfig(library, raw = {}) {
  const base = emptyConfig(library);
  const rejected = Object.keys(raw).filter((key) => SECRET_KEY_PATTERN.test(key) && str(raw[key]));
  if (rejected.length > 0) {
    throw new LibraryConfigError(
      'SECRET_NOT_ALLOWED',
      `连接配置不能包含凭据字段（${rejected.join(', ')}）。token 只应放在服务端代理或本机环境变量里。`,
    );
  }
  if (library === 't' || library === 'r') {
    const vaultRoot = str(raw.vault_root).replaceAll('\\', '/').replace(/\/+$/u, '');
    if (vaultRoot && (vaultRoot.includes(':') || /^[A-Za-z][A-Za-z0-9+.-]*:\/\//u.test(vaultRoot) || vaultRoot.startsWith('/') || vaultRoot.split('/').some((part) => part === '..'))) {
      throw new LibraryConfigError('VAULT_ROOT_INVALID', '库内目录只能填相对路径，例如 2-Permanent 或留空表示整库。');
    }
    const mode = str(raw.mode) || 'local';
    if (!['local', 'mcp'].includes(mode)) {
      throw new LibraryConfigError('MODE_INVALID', '接入方式只能是 local 或 mcp。');
    }
    const mcpEndpoint = str(raw.mcp_endpoint);
    if (mode === 'mcp') {
      if (!mcpEndpoint) throw new LibraryConfigError('MCP_ENDPOINT_REQUIRED', 'MCP 模式必须填写端点地址。');
      if (!/^https?:\/\//u.test(mcpEndpoint)) throw new LibraryConfigError('MCP_ENDPOINT_INVALID', 'MCP 端点必须以 http:// 或 https:// 开头。');
    }
    return {
      ...base,
      mode,
      vault_root: vaultRoot,
      mcp_endpoint: mcpEndpoint,
      managed_roots: listOf(raw.managed_roots, 8),
      tags: listOf(raw.tags, 8),
    };
  }
  if (library === 'm') {
    const spaceId = str(raw.space_id);
    const proxyUrl = str(raw.proxy_url);
    const path = str(raw.path).replaceAll('\\', '/').replace(/^\/+/u, '');
    if (proxyUrl && !/^https?:\/\//u.test(proxyUrl)) {
      throw new LibraryConfigError('PROXY_URL_INVALID', '代理地址必须以 http:// 或 https:// 开头。');
    }
    if (path.split('/').some((part) => part === '..')) {
      throw new LibraryConfigError('PATH_INVALID', '目标路径不能包含 .. 回到上级目录。');
    }
    const sharecode = str(raw.sharecode);
    if (sharecode && !/^[A-Za-z0-9_-]{4,64}$/u.test(sharecode)) {
      throw new LibraryConfigError('SHARECODE_INVALID', 'sharecode 格式不正确（应为代理分享链接里的那串字符）。');
    }
    return { ...base, space_id: spaceId, proxy_url: proxyUrl, path, sharecode };
  }
  const owner = str(raw.owner);
  const repo = str(raw.repo);
  // github_token 属于本机凭据，绝不进云端配置。
  if (owner && !/^[A-Za-z0-9_.-]{1,39}$/u.test(owner)) {
    throw new LibraryConfigError('GITHUB_OWNER_INVALID', 'GitHub 所有者名格式不正确。');
  }
  if (repo && !/^[A-Za-z0-9_.-]{1,100}$/u.test(repo)) {
    throw new LibraryConfigError('GITHUB_REPO_INVALID', 'GitHub 仓库名格式不正确。');
  }
  const branch = str(raw.branch) || 'main';
  if (!/^[A-Za-z0-9._\/-]{1,100}$/u.test(branch)) {
    throw new LibraryConfigError('GITHUB_BRANCH_INVALID', '分支名格式不正确。');
  }
  const proxyUrl = str(raw.proxy_url);
  if (proxyUrl && !/^https?:\/\//u.test(proxyUrl)) {
    throw new LibraryConfigError('PROXY_URL_INVALID', '代理地址必须以 http:// 或 https:// 开头。');
  }
  return { ...base, owner, repo, branch, proxy_url: proxyUrl };
}

/** GitHub token 单独存本机，不进云端。 */
export function rememberGithubToken(token) {
  try {
    if (token) globalThis.localStorage?.setItem('mtrp.githubToken', token);
    else globalThis.localStorage?.removeItem('mtrp.githubToken');
  } catch { /* 隐私模式下列表不可用，忽略 */ }
}

export function readGithubToken() {
  try {
    return globalThis.localStorage?.getItem('mtrp.githubToken') ?? '';
  } catch {
    return '';
  }
}

/** 经代理探查该仓库是否可写（token 只发往代理，不发往第三方）。 */
export async function checkGithubAccess(config, fetcher = globalThis.fetch) {
  const proxy = (config?.proxy_url ?? '').replace(/\/+$/u, '');
  const sharecode = config?.sharecode ?? '';
  const token = readGithubToken();
  if (proxy && sharecode) {
    const url = `${proxy}/mtrp/github-check?sharecode=${encodeURIComponent(sharecode)}&owner=${encodeURIComponent(config.owner ?? '')}&repo=${encodeURIComponent(config.repo ?? '')}`;
    const response = await fetcher(url, { headers: { accept: 'application/json', ...(token ? { 'x-mtrp-github-token': token } : {}) } });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload?.ok) throw new LibraryConfigError('GITHUB_CHECK_FAILED', payload?.message ?? `检查失败（HTTP ${response.status}）。`);
    return payload;
  }
  // 没有代理时只能做匿名读取
  const info = await probeGithub(config, fetcher);
  return {
    ok: true,
    writable: false,
    can_push: false,
    message: `只读：${info.full_name}（未配置代理，无法验证写入权限）`,
  };
}

/** 数据库行 → 面板可用对象。 */
export function rowToEntry(row) {
  return {
    library: row.library,
    label: row.label ?? '',
    enabled: row.enabled !== false,
    config: row.config && typeof row.config === 'object' ? row.config : {},
    updatedAt: row.updated_at ?? null,
  };
}

export async function loadConnections(cloud) {
  const { data, error } = await cloud.database
    .from(TABLE)
    .select('library, label, config, enabled, updated_at')
    .order('library', { ascending: true });
  if (error) {
    throw new LibraryConfigError('LOAD_FAILED', `读取云端连接配置失败：${error.message}`);
  }
  const map = {};
  for (const id of LIBRARY_IDS) map[id] = { library: id, label: '', enabled: false, config: emptyConfig(id), updatedAt: null, saved: false };
  for (const row of data ?? []) {
    if (!LIBRARY_IDS.includes(row.library)) continue;
    map[row.library] = { ...rowToEntry(row), saved: true };
  }
  return map;
}

/** 保存单个库；owner_id 由数据库 DEFAULT auth.uid() 填充，这里绝不发送。 */
export async function saveConnection(cloud, library, { config, label = '', enabled = true, ownerName = null }) {
  const normalized = normalizeConfig(library, config);
  const payload = {
    library,
    label: str(label),
    config: normalized,
    enabled: Boolean(enabled),
    updated_at: new Date().toISOString(),
  };
  const { data, error } = await cloud.database
    .from(TABLE)
    .upsert({ ...payload, owner_name: ownerName ?? null }, { onConflict: 'owner_id,library' })
    .select();
  if (error) {
    if (error.code === '42501') throw new LibraryConfigError('NOT_ALLOWED', '没有权限写入该配置，请确认已登录。');
    if (error.code === '23505') throw new LibraryConfigError('DUPLICATE', '同一库已有配置，请刷新后重试。');
    throw new LibraryConfigError('SAVE_FAILED', `保存失败：${error.message}`);
  }
  const affected = Array.isArray(data) ? data : [];
  if (affected.length === 0) {
    throw new LibraryConfigError('NOT_ALLOWED', '保存未生效：配置可能不属于当前账号，请重新登录。');
  }
  return rowToEntry(affected[0]);
}

export async function deleteConnection(cloud, library) {
  const { data, error } = await cloud.database.from(TABLE).delete().eq('library', library).select();
  if (error) throw new LibraryConfigError('DELETE_FAILED', `删除失败：${error.message}`);
  return Array.isArray(data) && data.length > 0;
}

/** GitHub 公开只读探活；浏览器可直连，私有仓库需要代理。 */
export async function probeGithub(config, fetcher = globalThis.fetch) {
  if (!config.owner || !config.repo) throw new LibraryConfigError('GITHUB_INCOMPLETE', '请先填写 owner 与 repo。');
  const base = config.proxy_url
    ? `${config.proxy_url.replace(/\/+$/u, '')}/repos/${config.owner}/${config.repo}`
    : `https://api.github.com/repos/${config.owner}/${config.repo}`;
  const response = await fetcher(base, { headers: { accept: 'application/vnd.github+json' } });
  if (response.status === 404) throw new LibraryConfigError('GITHUB_NOT_FOUND', '仓库不存在，或为私有仓库需要代理。');
  if (!response.ok) throw new LibraryConfigError('GITHUB_FAILED', `GitHub 返回 ${response.status}。`);
  const body = await response.json();
  return {
    full_name: body.full_name ?? `${config.owner}/${config.repo}`,
    default_branch: body.default_branch ?? '',
    private: Boolean(body.private),
    pushed_at: body.pushed_at ?? null,
  };
}

export function describeLibrary(library) {
  return LIBRARIES.find((item) => item.id === library) ?? { id: library, name: library, hint: '' };
}

/** 测连接：乐享走代理自检接口，GitHub 直接查仓库元数据。 */
export async function testConnection(library, config, fetcher = globalThis.fetch) {
  if (library === 'm') {
    if (!config.proxy_url) throw new LibraryConfigError('M_PROXY_REQUIRED', '请先填写代理地址。');
    if (!config.sharecode) throw new LibraryConfigError('SHARECODE_REQUIRED', '请先填写代理访问码。');
    if (!config.space_id) throw new LibraryConfigError('SPACE_REQUIRED', '请先选择乐享知识库。');
    const base = config.proxy_url.replace(/\/+$/u, '');
    const url = `${base}/mtrp/verify?sharecode=${encodeURIComponent(config.sharecode)}&space_id=${encodeURIComponent(config.space_id)}&path=${encodeURIComponent(config.path ?? '')}`;
    let response;
    try {
      response = await fetcher(url, { headers: { accept: 'application/json' } });
    } catch (error) {
      throw new LibraryConfigError('PROXY_UNREACHABLE', `连不上代理：${String(error?.message ?? error).slice(0, 60)}`);
    }
    if (response.status === 403) throw new LibraryConfigError('SHARECODE_REJECTED', '代理拒绝了访问：访问码不正确。');
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload?.ok) {
      throw new LibraryConfigError('M_TEST_FAILED', payload?.message ?? `自检失败（HTTP ${response.status}）。`);
    }
    return { ok: true, message: payload.message, detail: payload };
  }
  if (library === 'p') {
    // 有代理时能判断能否写入；没有代理只能匿名读，且易被限流。
    const access = await checkGithubAccess(config, fetcher);
    return { ok: true, message: access.message, detail: access };
  }
  // T/R 无远端服务可测，只能校验配置本身。
  return { ok: true, message: '目录设置已保存；文件读取需在本机授权 Obsidian 文件夹后生效。', detail: null };
}

/** Agent 网关是否可用：经代理实测一次，避免把 key 暴露到前端。 */
export async function testAgent(config, fetcher = globalThis.fetch) {
  const proxy = (config?.proxy_url ?? '').replace(/\/+$/u, '');
  const sharecode = config?.sharecode ?? '';
  if (!proxy || !sharecode) throw new LibraryConfigError('AGENT_PROXY_MISSING', '请在 M 库高级设置里填好代理地址与访问码。');
  const url = `${proxy}/mtrp/agent-check?sharecode=${encodeURIComponent(sharecode)}`;
  const response = await fetcher(url, { headers: { accept: 'application/json' } });
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload?.ok) {
    throw new LibraryConfigError('AGENT_FAILED', payload?.message ?? `Agent 检查失败（HTTP ${response.status}）。`);
  }
  return { ok: true, message: payload.message, detail: payload };
}
