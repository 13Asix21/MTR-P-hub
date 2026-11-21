import test from 'node:test';
import assert from 'node:assert/strict';
import { createBrowserAdapter } from '../../js/browserScan.js';
import { buildExport, downloadExportPair, exportCsv, exportJson, onlineVaultId, previewExport } from '../../js/export.js';
import { OfflineBuffer } from '../../js/offlineBuffer.js';
import { ReviewActionService } from '../../js/reviewActions.js';
import { emptyState } from '../../js/reviewDomain.js';
import { applyBatch } from '../../js/reviewWorkflow.js';
import { MemoryIndexedDb } from '../helpers/memoryIndexedDb.mjs';

const encoder = new TextEncoder();
const first = '---\ntags: [mtr/trial]\n---\r\n# 标题\r\n正文';
const invalid = '---\ntags: [\n---\n坏格式原文';
function directory() {
  const files = new Map([
    ['2-Permanent', new Map([['a.md', encoder.encode(first)], ['invalid.md', encoder.encode(invalid)]])],
  ]);
  const make = (parts) => ({
    kind: 'directory', name: parts.at(-1) ?? 'fixture',
    async getDirectoryHandle(part) {
      const next = [...parts, part];
      if (!files.has(next.join('/'))) throw new DOMException('不存在', 'NotFoundError');
      return make(next);
    },
    async getFileHandle(part) {
      const key = [...parts.slice(1), part].join('/');
      return { kind: 'file', getFile: async () => {
        const bytes = files.get('2-Permanent').get(part);
        if (!bytes || key !== part) throw new DOMException('不存在', 'NotFoundError');
        return { arrayBuffer: async () => Uint8Array.from(bytes).buffer };
      } };
    },
  });
  const root = make([]);
  root.getFileHandle = async (part) => ({
    kind: 'file',
    getFile: async () => {
      const bytes = files.get('2-Permanent').get(part);
      if (!bytes) throw new DOMException('不存在', 'NotFoundError');
      return { arrayBuffer: async () => Uint8Array.from(bytes).buffer };
    },
  });
  return root;
}

function serviceFor(buffer, now = () => new Date('2026-01-01T00:00:00Z')) {
  const adapter = createBrowserAdapter(directory());
  return new ReviewActionService({ adapter, offlineBuffer: buffer, vaultId: 'fixture-vault', now });
}

const listing = {
  path: 'fixture', managed_roots: ['2-Permanent'], excluded_roots: [], scanned_at: '2026-01-01T00:00:00Z', total: 2,
  items: [
    { vault_relative_path: '2-Permanent/a.md', sha256: '', mtr_state: 'trial', tags: ['mtr/trial'], parse_status: 'ok', size_bytes: encoder.encode(first).length, mtime: '', links_in: 0, links_out: 0 },
    { vault_relative_path: '2-Permanent/invalid.md', sha256: '', mtr_state: 'unknown', tags: [], parse_status: 'INVALID_YAML', size_bytes: encoder.encode(invalid).length, mtime: '', links_in: 0, links_out: 0 },
  ],
};

test('真实 UI 服务接口：详情、三动作、撤销、刷新恢复和导出形成闭环', async () => {
  const indexedDB = new MemoryIndexedDb();
  const buffer = new OfflineBuffer({ indexedDB });
  const service = serviceFor(buffer);
  const detail = await service.detail(listing.items[0], emptyState());
  assert.equal(detail.body.text.includes('正文'), true);
  listing.items[0].sha256 = detail.item.sha256;
  const invalidDetail = await service.detail(listing.items[1], emptyState());
  assert.equal(invalidDetail.item.parse_status, 'INVALID_YAML');
  assert.equal(invalidDetail.body.raw_only, true);
  assert.equal(invalidDetail.body.text, '坏格式原文');
  assert.equal(invalidDetail.raw_text, invalid);
  assert.equal(invalidDetail.frontmatter.raw, 'tags: [\n');
  listing.items[1].sha256 = invalidDetail.item.sha256;
  let state = emptyState();
  for (const [index, action] of ['approved', 'rejected', 'skipped'].entries()) {
    const item = listing.items[index === 0 ? 0 : 1];
    state = (await service.decide(state, item, item.sha256, action, '含逗号,换行\n理由')).state;
  }
  state = (await service.reset(state, listing.items[1].vault_relative_path, 'undo', listing.items[1].sha256)).state;
  assert.equal(state.items[listing.items[0].vault_relative_path].action, 'approved');
  assert.equal(state.items[listing.items[1].vault_relative_path], undefined);
  const reopened = new OfflineBuffer({ indexedDB });
  state = await reopened.replayPending({ userId: 'offline-local', vaultId: 'fixture-vault' });
  assert.equal(state.items[listing.items[0].vault_relative_path].reason, '含逗号,换行\n理由');
  const vaultId = await onlineVaultId('fixture', listing);
  const result = await buildExport({ listing, state, userId: 'local-browser', vaultRoot: 'fixture', vaultId, freshSha: (path) => service.freshSha(path), exportedAt: '2026-01-01T00:00:00.000Z' });
  assert.equal(result.document.items.length, 1);
  assert.equal(result.document.items[0].parse_status, 'ok');
  const preview = await previewExport({ listing, state, userId: 'local-browser', vaultRoot: 'fixture', freshSha: (path) => service.freshSha(path) });
  assert.equal(preview.preview.items_count, 1);
  assert.equal(preview.document.items.length, 1);
  assert.match(exportJson(result.document), /"runtime": "online"/u);
  assert.match(exportCsv(result.document.items), /^\uFEFFvault_relative_path,action,reason/u);
  assert.match(exportCsv(result.document.items), /"含逗号,换行\r?\n理由"/u);
  assert.equal(exportCsv(result.document.items).endsWith('\r\n'), true);
  await buffer.close(); await reopened.close();
});

test('旧内容冲突：decide 与撤销均零副作用，已读 pending 队列不清空', async () => {
  const indexedDB = new MemoryIndexedDb();
  const buffer = new OfflineBuffer({ indexedDB });
  const service = serviceFor(buffer);
  const initial = await service.detail(listing.items[0], emptyState());
  listing.items[0].sha256 = initial.item.sha256;
  const approved = await service.decide(emptyState(), listing.items[0], initial.item.sha256, 'approved');
  const before = structuredClone(approved.state);
  const queueBefore = await buffer.allActions();
  await assert.rejects(service.decide(before, listing.items[0], '0'.repeat(64), 'rejected'), (error) => error.code === 'CONTENT_CHANGED');
  const changedBytes = encoder.encode('---\ntags: [mtr/trial]\n---\n已被外部修改');
  service.adapter.readFileHandle = async () => ({ getFile: async () => ({ arrayBuffer: async () => Uint8Array.from(changedBytes).buffer }) });
  await assert.rejects(service.reset(before, listing.items[0].vault_relative_path, 'undo', listing.items[0].sha256, listing.items[0].sha256), (error) => error.code === 'CONTENT_CHANGED');
  await assert.rejects(service.reset(before, listing.items[0].vault_relative_path, 'undo', listing.items[0].sha256), (error) => error.code === 'CONTENT_CHANGED');
  assert.deepEqual(await buffer.allActions(), queueBefore);
  assert.deepEqual(before, approved.state);
  assert.equal(before.history.length, 1);
  await buffer.close();
});

test('批量上限 500 与单篇冲突不回滚已提交动作', async () => {
  const buffer = new OfflineBuffer({ indexedDB: new MemoryIndexedDb() });
  const service = serviceFor(buffer);
  const detail = await service.detail(listing.items[0], emptyState());
  listing.items[0].sha256 = detail.item.sha256;
  const paths = Array.from({ length: 501 }, (_, index) => `2-Permanent/${index}.md`);
  await assert.rejects(applyBatch({ listing, state: emptyState(), paths, action: 'approved' }), (error) => error.code === 'BATCH_LIMIT_EXCEEDED');
  const invalidDetail = await service.detail(listing.items[1], emptyState());
  listing.items[1].sha256 = invalidDetail.item.sha256;
  const result = await applyBatch({
    listing, state: emptyState(), paths: ['2-Permanent/a.md', '2-Permanent/invalid.md', '2-Permanent/missing.md'], action: 'approved',
    decide: (state, item, sha, action, reason) => service.decide(state, item, sha, action, reason),
  });
  assert.equal(result.completed.length, 2);
  assert.equal(result.failures[0].code, 'REVIEW_ITEM_NOT_FOUND');
  assert.equal(Object.keys(result.state.items).length, 2);
  assert.equal(result.progress.pending, 0);
  assert.equal(result.state.items['2-Permanent/invalid.md'].action, 'approved');
  await buffer.close();
});

test('IndexedDB 不可用/打开失败明确报错；二次确认后只清本机三 store', async () => {
  const unavailable = new OfflineBuffer({ indexedDB: null });
  await assert.rejects(unavailable.open(), (error) => error.code === 'OFFLINE_UNAVAILABLE');
  const broken = new OfflineBuffer({ indexedDB: { open() { const request = {}; queueMicrotask(() => { request.error = new Error('denied'); request.onerror?.(); }); return request; } } });
  await assert.rejects(broken.open(), (error) => error.code === 'OFFLINE_STORAGE_FAILED');
  const indexedDB = new MemoryIndexedDb();
  const buffer = new OfflineBuffer({ indexedDB });
  const service = serviceFor(buffer);
  const detail = await service.detail(listing.items[0], emptyState());
  await service.decide(emptyState(), listing.items[0], detail.item.sha256, 'approved');
  await buffer.exportBackup({ userId: 'offline-local' });
  await buffer.saveDirectoryHandle({ name: 'fixture', kind: 'directory' });
  await buffer.clearLocalData();
  assert.equal((await buffer.allActions()).length, 0);
  assert.equal(await buffer.loadDirectoryHandle(), null);
  const snapshot = indexedDB.snapshot('mtrp-online-workbench');
  assert.deepEqual(snapshot.pending_actions, []);
  assert.deepEqual(snapshot.export_backup, []);
  await buffer.close();
});

test('BlobURL 仅用于浏览器下载且每个 URL 只回收一次', async () => {
  const clicks = [];
  const revoked = [];
  const documentRef = { body: { append: (node) => clicks.push(node) }, createElement: () => ({ click() {}, remove() {} }) };
  const urlApi = { createObjectURL: () => `blob:test-${clicks.length}`, revokeObjectURL: (url) => revoked.push(url) };
  const result = { document: { user_id: 'local-browser', exported_at: '2026-01-01T00:00:00.000Z', items: [] } };
  const downloads = downloadExportPair(result, { documentRef, urlApi });
  const resolved = await downloads;
  assert.equal(clicks.length, 2);
  assert.deepEqual(resolved.map((entry) => entry.revoked), [true, true]);
  assert.equal(new Set(revoked).size, 2);
});
