/**
 * 通过审核后的传送：把笔记正文送到 M 库（乐享）指定空间与路径。
 *
 * 浏览器无法直连乐享 MCP（CORS 被拦），所以必须经用户配置的服务端代理。
 * 这里只发「标题 + 正文 + 目标」的最小载荷；不发 token，代理自己持有凭据。
 */
import { LibraryConfigError } from './librarySettings.js?v=20261123g';

export const TRANSFER_PATH = '/mtrp/transfer';

function unwrapText(detail) {
  const text = detail?.raw_text ?? detail?.body?.text ?? '';
  return String(text).replace(/^\uFEFF/u, '');
}

/** 去掉 frontmatter，只把正文送到 M 库；frontmatter 作为元数据单独附带。 */
export function splitForTransfer(rawText) {
  const normalized = String(rawText ?? '').replace(/^\uFEFF/u, '');
  const match = normalized.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/u);
  if (!match) return { frontmatter: '', body: normalized };
  return { frontmatter: match[1], body: normalized.slice(match[0].length) };
}

export function noteTitle(path) {
  const base = String(path ?? '').split('/').at(-1) ?? '';
  return base.replace(/\.md$/iu, '');
}

/**
 * 代理自身的 sharecode：网关用它控制对该应用链接的访问。
 * 注意不能复用审核页地址里的 sharecode —— 那是主应用的凭证，跨应用不通用。
 * 这个值会出现在前端源码里，属于“链接可见即授权”的边界，与代理自己的分享链接同级。
 */
export function proxySharecode(library) {
  const value = library?.sharecode;
  return typeof value === 'string' ? value.trim() : '';
}

/**
 * @param {object} input
 * @param {string} input.path Vault 内相对路径
 * @param {object} input.detail 详情（含 raw_text / item.sha256）
 * @param {object} input.library 已保存的 M 库配置
 */
export async function transferApprovedNote({ path, detail, library, session }, fetcher = globalThis.fetch) {
  if (!library?.space_id) throw new LibraryConfigError('M_NOT_CONFIGURED', '未配置乐享空间 ID，无法传送。');
  if (!library?.proxy_url) throw new LibraryConfigError('M_PROXY_REQUIRED', '乐享需要服务端代理，请先在在线设置里填写代理地址。');
  if (!session) throw new LibraryConfigError('LOGIN_REQUIRED', '请先登录再传送，避免传送到错误账号。');
  const rawText = unwrapText(detail);
  if (!rawText) throw new LibraryConfigError('EMPTY_CONTENT', '这篇没有可传送的正文。');
  const sha = detail?.item?.sha256 ?? '';
  if (!sha) throw new LibraryConfigError('SHA_MISSING', '缺少当前内容 SHA，请重新打开详情后再传送。');
  const { frontmatter, body } = splitForTransfer(rawText);
  const sharecode = proxySharecode(library);
  if (!sharecode) throw new LibraryConfigError('SHARECODE_REQUIRED', '请在在线设置里填写乐享代理的 sharecode（在代理分享链接里）。');
  // 已经是规范的代理路径就不再重复拼接，避免出现 /mtrp/transfer/mtrp/transfer。
  const base = library.proxy_url.replace(/\/+$/u, '');
  const pathPart = base.endsWith(TRANSFER_PATH) ? base : base + TRANSFER_PATH;
  const endpoint = `${pathPart}${pathPart.includes('?') ? '&' : '?'}sharecode=${encodeURIComponent(sharecode)}`;
  let response;
  try {
    response = await fetcher(endpoint, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-mtrp-sharecode': sharecode },
      body: JSON.stringify({
        space_id: library.space_id,
        path: library.path ?? '',
        title: noteTitle(path),
        source_path: path,
        content_sha256: sha,
        frontmatter,
        markdown: body,
      }),
    });
  } catch (error) {
    throw new LibraryConfigError('PROXY_UNREACHABLE', `无法连接乐享代理：${String(error?.message ?? error).slice(0, 80)}`);
  }
  if (response.status === 403) {
    throw new LibraryConfigError('SHARECODE_REJECTED', '代理拒绝了访问：sharecode 不正确或已失效，请到在线设置更新。');
  }
  if (!response.ok) {
    throw new LibraryConfigError('TRANSFER_FAILED', `传送失败（HTTP ${response.status}），请检查代理日志。`);
  }
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (payload && payload.ok === false) {
    throw new LibraryConfigError('TRANSFER_REJECTED', payload.message ?? '代理拒绝了这次传送。');
  }
  const target = library.path ? `/${String(library.path).replace(/^\/+/u, '')}` : '知识库根目录';
  return {
    ok: true,
    entryId: payload?.entry_id ?? null,
    message: payload?.entry_id
      ? `已传送到 M 库：空间 ${library.space_id} · ${target}（条目 ${payload.entry_id}）。`
      : `已传送到 M 库：空间 ${library.space_id} · ${target}。`,
  };
}
