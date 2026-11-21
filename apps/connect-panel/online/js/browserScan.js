import { loadFileDetail } from './scanner.js';
import { decodeUtf8Sig } from './parser.js';

/** 浏览器适配器只返回 File 的原始字节，不接触写句柄。 */
export function createBrowserAdapter(root) {
  async function directoryAt(parts) {
    let directory = root;
    for (const part of parts) directory = await directory.getDirectoryHandle(part, { create: false });
    return directory;
  }
  return {
    async statDirectory(parts) {
      try { await directoryAt(parts); return { kind: 'directory' }; } catch { return { kind: 'missing' }; }
    },
    async listDirectory(parts) {
      const directory = await directoryAt(parts);
      const entries = [];
      for await (const [name, handle] of directory.entries()) {
        if (handle.kind === 'directory') entries.push({ name, kind: 'directory' });
        else {
          const file = await handle.getFile();
          entries.push({ name, kind: 'file', size: file.size, mtime: `${new Date(file.lastModified).toISOString().slice(0, -1)}+00:00` });
        }
      }
      return entries;
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
