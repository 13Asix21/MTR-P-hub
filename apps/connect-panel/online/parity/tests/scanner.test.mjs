import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  ScanError,
  casefold,
  collectListing,
  isExcluded,
  loadFileDetail,
  normalizeRoots,
  scanFileBytes,
  walkCollect,
} from '../../js/scanner.js';
import { run as runJs } from '../run_js_engine.mjs';

import { fileURLToPath } from 'node:url';

const root = new URL('../fixtures/vault/', import.meta.url);
const fixtureVault = fileURLToPath(root);
const manifest = JSON.parse(await readFile(new URL('../fixtures/fixtures_manifest.json', import.meta.url), 'utf8'));

test('确定性 fixture 树覆盖全部 34 类及 12 个 INVALID_YAML', async () => {
  const report = await runJs(fixtureVault);
  // 整库扫描会在受管 44 篇之外，再纳入一次库根级的 outside-root.md。
  assert.equal(report.listing.total, manifest.expected_managed_markdown + 1);
  assert.equal(report.listing.items.some((item) => item.vault_relative_path === 'outside-root.md'), true);
  assert.equal(report.listing.total, report.listing.statistics.scanned_files);
  assert.equal(report.listing.counts.parsed_ok, report.listing.statistics.parsed_files);
  assert.equal(report.listing.counts.invalid, report.listing.statistics.error_count);
  assert.equal(report.listing.items.filter((item) => item.parse_status === 'INVALID_YAML').length, 12);
  assert.equal(report.listing.items.filter((item) => item.parse_status === 'INVALID_ENCODING').length, 1);
  assert.equal(report.listing.items.filter((item) => item.parse_status === 'FILE_TOO_LARGE').length, 1);
  assert.equal(report.listing.items.find((item) => item.vault_relative_path.endsWith('29-too-large.md')).sha256, '');
  assert.equal(report.listing.items.find((item) => item.vault_relative_path.endsWith('28-one-mib.md')).parse_status, 'ok');
  assert.equal(report.listing.warnings.length, 1);
  assert.equal(report.listing.statistics.excluded_directories, 2);
  // 两个 excluded root 是目录事件，只计 excluded_directories；唯一 skipped_file 为超限文件。
  assert.equal(report.listing.statistics.skipped_files, 1);
});

test('NFC/NFD 同键保留、NFC 路径排序与原始文件名详情重读', async () => {
  const listing = (await runJs(fixtureVault)).listing;
  const collisions = listing.items.filter((item) => item.vault_relative_path.includes('34-café.md'));
  assert.equal(collisions.length, 2);
  assert.ok(listing.items.some((item) => item.vault_relative_path.endsWith('32-näive.md')));
  const details = (await runJs(fixtureVault)).details;
  assert.equal(details.length, listing.total);
  const nfd = details.find((entry) => entry.path.endsWith('32-näive.md'));
  assert.equal(nfd.detail.body.text, 'NFD file name\n');
});

test('受管路径排除按完整段，A/B2 不误排；大写 MD 入选', async () => {
  assert.equal(isExcluded(['2-Permanent', 'Resources', 'MTR-P-System', 'x.md'], [['2-Permanent', 'Resources', 'MTR-P-System']]), true);
  assert.equal(isExcluded(['2-Permanent', 'Resources', 'MTR-P-System2', 'x.md'], [['2-Permanent', 'Resources', 'MTR-P-System']]), false);
  const listing = (await runJs(fixtureVault)).listing;
  assert.ok(listing.items.some((item) => item.vault_relative_path.endsWith('NOTE.MD')));
  assert.ok(listing.items.some((item) => item.vault_relative_path.endsWith('MTR-P-System2/kept.md')));
  assert.ok(!listing.items.some((item) => item.vault_relative_path.endsWith('.txt')));
});

test('扫描计数统计完整 11 键、链接全路径优先与 basename 唯一', () => {
  const fact = (path, links) => ({ relative_posix: path, size_bytes: 1, mtime: '', content_sha256: 'a'.repeat(64), tags: [], classification: 'unmanaged', parse_status: 'ok', raw_bytes: new TextEncoder().encode(links) });
  const listing = collectListing([fact('2-Permanent/a.md', '[[b]] [[dup]]'), fact('2-Permanent/b.md', ''), fact('2-Permanent/x/dup.md', ''), fact('2-Permanent/y/dup.md', '')]);
  assert.deepEqual(Object.keys(listing.statistics), ['scanned_files', 'parsed_files', 'trial_notes', 'rarely_notes', 'unmanaged_notes', 'conflict_notes', 'skipped_files', 'skipped_directories', 'excluded_directories', 'error_count', 'limit_reached']);
  assert.equal(listing.items.find((item) => item.vault_relative_path.endsWith('/b.md')).links_in, 1);
  assert.equal(listing.items.find((item) => item.vault_relative_path.endsWith('/a.md')).links_out, 2);
});

test('maxScanFiles 截断仍给出完整同构统计', async () => {
  const adapter = {
    statDirectory: async () => ({ kind: 'directory' }),
    listDirectory: async (parts) => parts.length === 0 ? [{ name: 'a.md', kind: 'file', size: 1 }, { name: 'b.md', kind: 'file', size: 1 }] : [],
    readBytes: async () => new TextEncoder().encode('x'),
  };
  const listing = await walkCollect(adapter, { maxScanFiles: 1 });
  assert.equal(listing.total, 1);
  assert.equal(listing.limit_reached, true);
  assert.equal(listing.status, 'limited');
  assert.equal(listing.statistics.limit_reached, true);
  // 库根统一以 '.' 表示：'.' 归一化为「从库根开始」而不是字面目录名。
  assert.deepEqual([...listing.managed_roots], ['.']);
});

test('兼容任意 Obsidian 库结构：递归多层子目录且不要求 2-Permanent', async () => {
  const tree = {
    '': [{ name: '笔记.md', kind: 'file', size: 5 }, { name: 'Inbox', kind: 'directory' }, { name: '.obsidian', kind: 'directory' }, { name: '.trash', kind: 'directory' }],
    Inbox: [{ name: '想法', kind: 'directory' }, { name: '收集.md', kind: 'file', size: 5 }],
    'Inbox/想法': [{ name: '深度.md', kind: 'file', size: 5 }],
    '.obsidian': [{ name: 'app.json', kind: 'file', size: 3 }],
    '.trash': [{ name: '旧.md', kind: 'file', size: 5 }],
  };
  const adapter = {
    statDirectory: async (parts) => (parts.length === 0 || Object.hasOwn(tree, parts.join('/')) ? { kind: 'directory' } : { kind: 'missing' }),
    listDirectory: async (parts) => tree[parts.join('/')] ?? [],
    readBytes: async () => new TextEncoder().encode('---\ntags: [mtr/trial]\n---\n正文'),
  };
  const listing = await walkCollect(adapter, {});
  assert.deepEqual(listing.items.map((item) => item.vault_relative_path).sort(), ['Inbox/想法/深度.md', 'Inbox/收集.md', '笔记.md']);
  assert.equal(listing.items.some((item) => item.vault_relative_path.startsWith('.')), false);
});

test('casefold 处理架构指定 ß/İ 与连字边界', () => {
  assert.equal(casefold('Straße İ ﬃ'), 'strasse i\u0307 ffi');
});

test('根校验拒绝穿越、URL、盘符与空 managed roots', () => {
  for (const [input, code] of [['../x', 'ROOT_TRAVERSAL_REJECTED'], ['file://x', 'ROOT_MUST_BE_RELATIVE'], ['C:x', 'ROOT_DRIVE_RELATIVE_REJECTED']]) {
    assert.throws(() => normalizeRoots([input], 'managed_roots'), (error) => error instanceof ScanError && error.code === code);
  }
  assert.throws(() => normalizeRoots([], 'managed_roots'), (error) => error.code === 'MANAGED_ROOT_REQUIRED');
});

test('详情 INVALID_YAML 只读原文，INVALID_ENCODING 不替换坏字节', async () => {
  const bad = await scanFileBytes('2-Permanent/bad.md', new TextEncoder().encode('---\na: [1\n---\nbody [[a]]'));
  const detail = await loadFileDetail({ vault_relative_path: '2-Permanent/bad.md', links_in: 0 }, new TextEncoder().encode('---\na: [1\n---\nbody [[a]]'));
  assert.equal(bad.parse_status, 'INVALID_YAML');
  assert.equal(detail.body.raw_only, true);
  // 有闭合 --- 的 INVALID_YAML 详情仍按 _split_frontmatter 返回其后的正文原文。
  assert.equal(detail.body.text, 'body [[a]]');
  assert.equal(detail.links.out, 1);
  const encoding = await loadFileDetail({ vault_relative_path: '2-Permanent/bad.md' }, new Uint8Array([0xff, 0xfe]));
  assert.equal(encoding.item.parse_status, 'INVALID_ENCODING');
  assert.equal(encoding.body.text, '');
});
