import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { walkCollect } from '../../js/scanner.js';
import { createBrowserAdapter } from '../../js/browserScan.js';
import { scanVaultWithWorkers, workerConcurrency } from '../../js/scanWorker.js';

function memoryDirectory(files, name = '2-Permanent') {
  return {
    kind: 'directory', name,
    async getDirectoryHandle(part) {
      if (part === '2-Permanent') return this;
      if (part === 'nested') return memoryDirectory({ 'b.md': '#mtr/rarely\n[[a]]' }, 'nested');
      throw new DOMException('缺失', 'NotFoundError');
    },
    async getFileHandle(part) {
      const bytes = files[part];
      if (!(bytes instanceof Uint8Array)) throw new DOMException('缺失', 'NotFoundError');
      return { kind: 'file', getFile: async () => ({ size: bytes.length, lastModified: 0, arrayBuffer: async () => Uint8Array.from(bytes).buffer }) };
    },
    async *entries() {
      for (const [entryName, bytes] of Object.entries(files)) {
        const handle = entryName === 'nested' ? memoryDirectory({ 'b.md': '#mtr/rarely\n[[a]]' }, 'nested') : {
          kind: 'file',
          async getFile() { return { size: bytes.length, lastModified: 0, arrayBuffer: async () => Uint8Array.from(bytes).buffer }; },
          async arrayBuffer() { return Uint8Array.from(bytes).buffer; },
        };
        yield [entryName, handle];
      }
    },
  };
}

const workerFactory = () => ({
  postMessage(message) {
    queueMicrotask(async () => {
      try {
        const { scanFileBytes, compactScannedFact } = await import('../../js/scanner.js');
        const bytes = new Uint8Array(await (await message.fileHandle.getFile()).arrayBuffer());
        const fact = await scanFileBytes(message.relative, bytes, { size_bytes: message.size, mtime: message.mtime });
        this.onmessage({ data: { id: message.id, fact } });
      } catch {
        this.onmessage({ data: { id: message.id, fact: { relative_posix: message.relative, size_bytes: message.size, mtime: message.mtime, content_sha256: '', tags: [], classification: 'unknown', parse_status: 'FILE_READ_FAILED', error_code: 'FILE_READ_FAILED', error_message: '文件读取失败。', raw_bytes: [] } } });
      }
    });
  },
  terminate() {},
});

test('Worker 管线、真实浏览器适配器共用收集器并处理 NFC 路径', async () => {
  const root = memoryDirectory({ 'a.md': new TextEncoder().encode('---\ntags: [mtr/trial]\n---\n[[nested/b]]'), 'nested': [], 'skip.txt': new TextEncoder().encode('x') });
  const adapter = createBrowserAdapter(root);
  assert.equal((await adapter.statDirectory(['2-Permanent'])).kind, 'directory');
  const listing = await scanVaultWithWorkers(root, { workerFactory, concurrency: 2, scannedAt: '2026-01-01T00:00:00.000Z' });
  assert.equal(listing.total, 2);
  assert.equal(listing.path, '2-Permanent');
  assert.equal(listing.items.find((item) => item.vault_relative_path.endsWith('a.md')).links_out, 1);
  assert.equal(listing.items.find((item) => item.vault_relative_path.endsWith('b.md')).links_in, 1);
  const detail = await adapter.readBytes(['2-Permanent', 'a.md']);
  assert.equal(new TextDecoder().decode(detail), '---\ntags: [mtr/trial]\n---\n[[nested/b]]');
});

test('Worker 并发上限 8、单篇读取失败不终止、排除目录计数正确', async () => {
  assert.equal(workerConcurrency({ navigator: { hardwareConcurrency: 32 } }), 8);
  assert.equal(workerConcurrency({ navigator: { hardwareConcurrency: 1 } }), 2);
  const root = memoryDirectory({ 'a.md': new TextEncoder().encode('a'), 'bad.md': new TextEncoder().encode('b'), 'Resources': [] });
  const originalEntries = root.entries;
  const originalGetDirectory = root.getDirectoryHandle;
  root.getDirectoryHandle = async (part) => part === 'Resources'
    ? { kind: 'directory', async *entries() { yield ['x.md', { kind: 'file' }]; } }
    : originalGetDirectory.call(root, part);
  root.entries = async function* () {
    yield ['Resources', { kind: 'directory', async *entries() { yield ['x.md', { kind: 'file' }]; } }];
    yield ['a.md', { kind: 'file', getFile: async () => { throw new DOMException('失效', 'NotAllowedError'); } }];
    yield ['bad.md', { kind: 'file', async arrayBuffer() { throw new Error('read'); }, async getFile() { return { size: 1, lastModified: 0 }; } }];
  };
  const listing = await scanVaultWithWorkers(root, { workerFactory, concurrency: 2, excludedRoots: ['2-Permanent/Resources'] });
  assert.equal(listing.total, 2);
  assert.equal(listing.statistics.excluded_directories, 1);
  assert.ok(listing.items.some((item) => item.parse_status === 'FILE_READ_FAILED'));
  assert.equal(listing.items.every((item) => item.sha256.length === 0 || item.sha256.length === 64), true);
  root.entries = originalEntries;
});

test('Worker 与 Node 适配器对同一 44 篇 fixture 逐字段全等', async () => {
  const vault = fileURLToPath(new URL('../fixtures/vault/', import.meta.url));
  const fixedOptions = { scannedAt: '2026-01-01T00:00:00.000Z' };
  async function fixtureDirectory(parts = []) {
    const children = await readdir(path.join(vault, ...parts), { withFileTypes: true });
    const entries = [];
    for (const child of children) {
      const filePath = path.join(vault, ...parts, child.name);
      const metadata = await stat(filePath);
      const handle = child.isDirectory()
        ? await fixtureDirectory([...parts, child.name])
        : {
            kind: 'file',
            name: child.name,
            async getFile() {
              const bytes = new Uint8Array(await readFile(filePath));
              return { size: bytes.length, lastModified: metadata.mtimeMs, arrayBuffer: async () => Uint8Array.from(bytes).buffer };
            },
          };
      entries.push([child.name, handle]);
    }
    return {
      kind: 'directory',
      name: parts.length ? parts.at(-1) : 'fixture-vault',
      async *entries() { yield* entries; },
      async getDirectoryHandle(part) {
        const entry = entries.find(([name]) => name === part);
        if (!entry || entry[1].kind !== 'directory') throw new DOMException('缺失', 'NotFoundError');
        return entry[1];
      },
      async getFileHandle(part) {
        const entry = entries.find(([name]) => name === part);
        if (!entry || entry[1].kind !== 'file') throw new DOMException('缺失', 'NotFoundError');
        return entry[1];
      },
    };
  }
  const root = await fixtureDirectory();
  const nodeAdapter = {
    async statDirectory(parts) {
      try {
        let directory = root;
        for (const part of parts) directory = await directory.getDirectoryHandle(part);
        return { kind: 'directory' };
      } catch { return { kind: 'missing' }; }
    },
    async listDirectory(parts) {
      let directory = root;
      for (const part of parts) directory = await directory.getDirectoryHandle(part);
      const entries = [];
      for await (const [name, handle] of directory.entries()) {
        if (handle.kind === 'directory') entries.push({ name, kind: 'directory' });
        else {
            const file = await handle.getFile();
          const mtime = `${new Date(file.lastModified).toISOString().slice(0, -1)}+00:00`;
          entries.push({ name, kind: 'file', size: file.size, mtime });
        }
      }
      return entries;
    },
    async readBytes(parts) {
      let directory = root;
      for (const part of parts.slice(0, -1)) directory = await directory.getDirectoryHandle(part);
      const fileHandle = await directory.getFileHandle(parts.at(-1));
      const file = await fileHandle.getFile();
      return new Uint8Array(await file.arrayBuffer());
    },
  };
  const legacyDirectory = { kind: 'directory', name: 'encoding' };
  await assert.rejects(
    async () => legacyDirectory.getFileHandle('08-bom-crlf.md'),
    { name: 'TypeError', message: 'legacyDirectory.getFileHandle is not a function' },
  );
  const nestedDirectory = await (await root.getDirectoryHandle('2-Permanent')).getDirectoryHandle('encoding');
  assert.deepEqual(
    { kind: nestedDirectory.kind, name: nestedDirectory.name, hasGetFileHandle: typeof nestedDirectory.getFileHandle === 'function' },
    { kind: 'directory', name: 'encoding', hasGetFileHandle: true },
  );
  const singleBytes = await nodeAdapter.readBytes(['2-Permanent', 'encoding', '08-bom-crlf.md']);
  assert.equal(singleBytes.length, 66);
  const options = { ...fixedOptions, concurrency: 4, workerFactory };
  const browser = await scanVaultWithWorkers(root, { ...options, path: 'fixture-vault' });
  const node = await walkCollect(nodeAdapter, { ...fixedOptions, path: 'fixture-vault' });
  assert.equal(node.total, 44);
  assert.deepEqual(browser, node);
  assert.equal(browser.total, 44);
  assert.equal(node.items.filter((item) => item.parse_status === 'FILE_READ_FAILED').length, 0);
  assert.equal(browser.items.filter((item) => item.parse_status === 'FILE_READ_FAILED').length, 0);
  const first = node.items.find((item) => item.vault_relative_path === '2-Permanent/encoding/08-bom-crlf.md');
  assert.equal(first.parse_status, 'ok');
  assert.equal(first.sha256, '73256f542c8d0553afd88e8d5f0940c883d3e6b92262c0efef525a7662845a34');
});
