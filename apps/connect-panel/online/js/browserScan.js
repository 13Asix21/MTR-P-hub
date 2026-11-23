import { DEFAULT_EXCLUDED_ROOTS, isExcluded, loadFileDetail } from './scanner.js?v=20261123f';
import { decodeUtf8Sig } from './parser.js?v=20261123f';

/** 浏览器适配器只返回 File 的原始字节，不接触写句柄。 */
export function createBrowserAdapter(root) {
  const excluded = DEFAULT_EXCLUDED_ROOTS.map((path) => path.split('/'));
  // 尾部的 '.' 是「库根」标记，不是真实目录名，必须跳过，否则会被当成库根下的子目录去查找。
  async function directoryAt(parts) {
    let directory = root;
    for (const part of parts) {
      if (part === '.' || part === '') continue;
      directory = await directory.getDirectoryHandle(part, { create: false });
    }
    return directory;
  }
  // 排除规则与纯引擎一致：库根下的 .obsidian/.trash/.git 等不进清单，避免全库扫描被隐藏目录拖慢。
  async function collect(directory, prefix) {
    const entries = [];
    for await (const [name, handle] of directory.entries()) {
      const parts = [...prefix, name];
      if (isExcluded(parts, excluded)) continue;
      if (handle.kind === 'directory') entries.push({ name, kind: 'directory' });
      else {
        const file = await handle.getFile();
        entries.push({ name, kind: 'file', size: file.size, mtime: `${new Date(file.lastModified).toISOString().slice(0, -1)}+00:00` });
      }
    }
    return entries;
  }
  return {
    async statDirectory(parts) {
      try { await directoryAt(parts); return { kind: 'directory' }; } catch { return { kind: 'missing' }; }
    },
    async listDirectory(parts) {
      return collect(await directoryAt(parts), parts);
    },
    async readBytes(parts) {
      const fileHandle = await (await directoryAt(parts.slice(0, -1))).getFileHandle(parts.at(-1), { create: false });
      return new Uint8Array(await (await fileHandle.getFile()).arrayBuffer());
    },
    async readFileHandle(relative) {
      const parts = relative.split('/');
      return (await directoryAt(parts.slice(0, -1))).getFileHandle(parts.at(-1), { create: false });
    },
  };
}

export async function readCurrentSha(adapter, relative, sha256Bytes) {
  const handle = await adapter.readFileHandle(relative);
  const bytes = new Uint8Array(await (await handle.getFile()).arrayBuffer());
  return { sha256: await sha256Bytes(bytes), bytes };
}

export async function readBrowserDetail(adapter, item, relative = item.vault_relative_path) {
  const bytes = await adapter.readBytes(relative.split('/'));
  const detail = await loadFileDetail(item, bytes);
  // UI 专用原文视图；不改变供 Python 对拍的 body/frontmatter 契约。
  try { return { ...detail, raw_text: decodeUtf8Sig(bytes) }; }
  catch { return { ...detail, raw_text: '' }; }
}
