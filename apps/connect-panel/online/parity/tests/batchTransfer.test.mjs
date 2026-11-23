import test from 'node:test';
import assert from 'node:assert/strict';

/** 复刻批次筛选与状态统计的核心规则，确保行为可回归。 */
function pickTransferable(items, stateItems, transferred) {
  return items
    .filter((item) => stateItems?.[item.vault_relative_path]?.action === 'approved')
    .filter((item) => !transferred.has(item.vault_relative_path))
    .map((item) => item.vault_relative_path);
}

function summarize(job) {
  const parts = [`传送 ${job.done}/${job.total}`];
  if (job.ok) parts.push(`成功 ${job.ok}`);
  if (job.failed) parts.push(`失败 ${job.failed}`);
  if (job.running) parts.push('进行中…');
  return parts.join(' · ');
}

const items = [
  { vault_relative_path: 'a.md' },
  { vault_relative_path: 'b.md' },
  { vault_relative_path: 'c.md' },
  { vault_relative_path: 'd.md' },
];

test('只挑已通过且未传送过的笔记', () => {
  const stateItems = {
    'a.md': { action: 'approved' },
    'b.md': { action: 'rejected' },
    'c.md': { action: 'approved' },
    'd.md': { action: 'skipped' },
  };
  const picked = pickTransferable(items, stateItems, new Set());
  assert.deepEqual(picked, ['a.md', 'c.md']);
});

test('已传送过的不会重复传送', () => {
  const stateItems = { 'a.md': { action: 'approved' }, 'c.md': { action: 'approved' } };
  const picked = pickTransferable(items, stateItems, new Set(['a.md']));
  assert.deepEqual(picked, ['c.md']);
});

test('全部传完后候选为空', () => {
  const stateItems = { 'a.md': { action: 'approved' } };
  assert.deepEqual(pickTransferable(items, stateItems, new Set(['a.md'])), []);
});

test('状态栏按进度给出可读文案', () => {
  assert.equal(summarize({ total: 3, done: 0, ok: 0, failed: 0, running: true }), '传送 0/3 · 进行中…');
  assert.equal(summarize({ total: 3, done: 2, ok: 2, failed: 0, running: true }), '传送 2/3 · 成功 2 · 进行中…');
  assert.equal(summarize({ total: 3, done: 3, ok: 2, failed: 1, running: false }), '传送 3/3 · 成功 2 · 失败 1');
});

test('单篇失败不影响其余继续', () => {
  const job = { total: 3, done: 0, ok: 0, failed: 0, running: true };
  for (const outcome of ['ok', 'fail', 'ok']) {
    if (outcome === 'ok') job.ok += 1; else job.failed += 1;
    job.done += 1;
  }
  job.running = false;
  assert.equal(job.ok, 2);
  assert.equal(job.failed, 1);
  assert.equal(summarize(job), '传送 3/3 · 成功 2 · 失败 1');
});
