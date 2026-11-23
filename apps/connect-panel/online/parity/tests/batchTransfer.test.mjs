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

test('成功后状态栏使用明确的成功文案', () => {
  const job = { total: 2, done: 2, ok: 2, failed: 0, running: false };
  const text = job.failed === 0
    ? `✓ 传送成功：${job.ok} 篇已写入乐享知识库「M库范式」`
    : '';
  assert.match(text, /传送成功/u);
  assert.match(text, /2 篇/u);
  assert.match(text, /乐享知识库/u);
});

test('部分失败时明确区分成功与失败数量', () => {
  const job = { total: 3, done: 3, ok: 2, failed: 1, running: false };
  const tail = job.ok > 0 ? `成功 ${job.ok} 篇` : '全部失败';
  const text = `⚠ 传送结束：${tail}，失败 ${job.failed} 篇`;
  assert.match(text, /成功 2 篇/u);
  assert.match(text, /失败 1 篇/u);
});

test('传送状态列区分已传送 / 待传送 / 不适用', () => {
  const cell = (sent, status) => (sent ? '✓ 已传送' : (status === 'approved' ? '待传送' : '—'));
  assert.equal(cell(true, 'approved'), '✓ 已传送');
  assert.equal(cell(false, 'approved'), '待传送');
  assert.equal(cell(false, 'pending'), '—');
});

/** 复刻按钮状态决策，保证「传送完置灰」不会回归。 */
function batchButtonState({ configured, approvedTotal, pending }) {
  if (!configured) return { disabled: true, label: '批量传送到 M 库', hint: '未配置' };
  if (approvedTotal > 0 && pending.length === 0) {
    return { disabled: true, label: '已全部传送到 M 库', hint: `${approvedTotal} 篇均已传送` };
  }
  if (pending.length === 0) return { disabled: true, label: '批量传送到 M 库', hint: '请先批量通过' };
  return { disabled: false, label: `批量传送到 M 库（${pending.length}）`, hint: '' };
}

test('未配置 M 库时按钮禁用并说明原因', () => {
  const s = batchButtonState({ configured: false, approvedTotal: 2, pending: ['a.md'] });
  assert.equal(s.disabled, true);
  assert.match(s.hint, /未配置/u);
});

test('有可传送项时按钮可用并显示待传数量', () => {
  const s = batchButtonState({ configured: true, approvedTotal: 3, pending: ['a.md', 'b.md'] });
  assert.equal(s.disabled, false);
  assert.equal(s.label, '批量传送到 M 库（2）');
});

test('全部传送完毕后按钮置灰，文案改为已全部传送', () => {
  const s = batchButtonState({ configured: true, approvedTotal: 2, pending: [] });
  assert.equal(s.disabled, true);
  assert.equal(s.label, '已全部传送到 M 库');
  assert.match(s.hint, /2 篇均已传送/u);
});

test('尚未批量通过时按钮禁用并提示先通过', () => {
  const s = batchButtonState({ configured: true, approvedTotal: 0, pending: [] });
  assert.equal(s.disabled, true);
  assert.match(s.hint, /先批量通过/u);
});
