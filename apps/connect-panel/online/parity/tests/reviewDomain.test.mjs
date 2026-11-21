import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  ReviewError,
  buildExportItems,
  computeProgress,
  decide,
  emptyState,
  reset,
  reviewStatusFor,
  validateState,
} from '../../js/reviewDomain.js';

const sha = 'a'.repeat(64);
const path = '2-Permanent/a.md';
const at = '2026-01-01T00:00:00+00:00';
const script = JSON.parse(await readFile(new URL('../domain_cases.json', import.meta.url), 'utf8'));

function expectCode(code, callback) {
  assert.throws(callback, (error) => error instanceof ReviewError && error.code === code);
}

test('改判、撤销、STALE_RESET 与历史来源完整', () => {
  let state = decide(emptyState(), path, 'approved', null, sha, at);
  assert.equal(state.items[path].reason, '');
  state = decide(state, path, 'rejected', 'reason', sha, '2026-01-01T00:00:01+00:00');
  assert.equal(state.history.at(-1).from, 'approved');
  assert.equal(reviewStatusFor(state.items[path], 'b'.repeat(64)), 'stale');
  state = reset(state, path, 'stale_reset', '2026-01-01T00:00:02+00:00');
  assert.equal(state.items[path], undefined);
  assert.equal(state.history.at(-1).op, 'STALE_RESET');
  state = decide(state, path, 'approved', '', 'b'.repeat(64), '2026-01-01T00:00:03+00:00');
  state = reset(state, path, 'undo', '2026-01-01T00:00:04+00:00');
  assert.equal(state.history.at(-1).op, 'UNDO');
  expectCode('REVIEW_ITEM_NOT_DECIDED', () => reset(state, path, 'undo', at));
});

test('进度的 STALE 独立、INVALID 含所有非 ok、分母不减少', () => {
  const state = decide(emptyState(), path, 'approved', '', sha, at);
  const listing = { total: 2, items: [{ vault_relative_path: path, sha256: 'b'.repeat(64), parse_status: 'ok' }, { vault_relative_path: '2-Permanent/b.md', sha256: sha, parse_status: 'INVALID_YAML' }] };
  assert.deepEqual(computeProgress(listing, state), { reviewed: 0, approved: 0, rejected: 0, skipped: 0, pending: 2, stale: 1, invalid: 1, pass_rate: null });
});

test('approved 导出剔除 stale，all 标注 stale，INVALID_YAML 可导出', () => {
  const state = decide(decide(emptyState(), path, 'approved', '', sha, at), '2-Permanent/b.md', 'approved', '', 'b'.repeat(64), at);
  const listing = { items: [path, '2-Permanent/b.md'].map((item) => ({ vault_relative_path: item, parse_status: item.endsWith('b.md') ? 'INVALID_YAML' : 'ok', mtr_state: 'unknown', tags: [] })) };
  const approved = buildExportItems(listing, state, 'approved', () => 'b'.repeat(64));
  assert.equal(approved.items.length, 1);
  assert.equal(approved.excluded_stale, 1);
  assert.equal(approved.approved_total, 2);
  const all = buildExportItems(listing, state, 'all', () => 'b'.repeat(64));
  assert.equal(all.items.find((item) => item.vault_relative_path === path).stale, true);
  assert.equal(all.items.find((item) => item.vault_relative_path.endsWith('b.md')).stale, false);
});

test('历史最多 200 且保留末 200 条', () => {
  let state = emptyState();
  for (let index = 0; index < 205; index += 1) {
    state = decide(state, path, 'approved', '', sha, `2026-01-02T00:${String(Math.floor(index / 60)).padStart(2, '0')}:${String(index % 60).padStart(2, '0')}+00:00`);
  }
  assert.equal(state.history.length, 200);
  assert.equal(state.history[0].at, '2026-01-02T00:00:05+00:00');
});

const invalidInputs = [
  ['非法路径', (state) => decide(state, '../x.md', 'approved', '', sha, at), 'REVIEW_PATH_INVALID'],
  ['NFD 新路径', (state) => decide(state, '2-Permanent/cafe\u0301.md', 'approved', '', sha, at), 'REVIEW_PATH_INVALID'],
  ['盘符', (state) => decide(state, 'C:x.md', 'approved', '', sha, at), 'REVIEW_PATH_INVALID'],
  ['动作', (state) => decide(state, path, 'banana', '', sha, at), 'REQUEST_VALIDATION_FAILED'],
  ['大写 sha', (state) => decide(state, path, 'approved', '', sha.toUpperCase(), at), 'REQUEST_VALIDATION_FAILED'],
  ['理由 501', (state) => decide(state, path, 'approved', 'x'.repeat(501), sha, at), 'REQUEST_VALIDATION_FAILED'],
  ['无时区', (state) => decide(state, path, 'approved', '', sha, '2026-01-01T00:00:00'), 'REQUEST_VALIDATION_FAILED'],
  ['非法时间', (state) => decide(state, path, 'approved', '', sha, '2026-02-30T00:00:00+00:00'), 'REQUEST_VALIDATION_FAILED'],
];
for (const [name, callback, code] of invalidInputs) {
  test(`审核域输入负例：${name}`, () => expectCode(code, () => callback(emptyState())));
}

test('validateState 严格拒绝未知键、非 NFC 键、坏 history、长数组', () => {
  const valid = decide(emptyState(), path, 'approved', '', sha, at);
  for (const invalid of [
    { ...valid, extra: true },
    { ...valid, items: { '2-Permanent/cafe\u0301.md': valid.items[path] } },
    { ...valid, history: [{ path, op: 'UNDO', from: null, to: null, reason: null, content_sha256: null, at }] },
    { ...valid, history: new Array(201).fill(valid.history[0]) },
  ]) {
    expectCode('REVIEW_RECORD_INVALID', () => validateState(invalid));
  }
  assert.deepEqual(validateState(null), emptyState());
});

test('固定 domain 脚本含 DECIDE/STALE/UNDO/导出与负例', () => {
  const ops = new Set(script.steps.map((step) => step.op));
  for (const op of ['decide', 'reset', 'export', 'status', 'reset_error', 'decide_error', 'invalid_states', 'history_limit']) {
    assert.ok(ops.has(op), op);
  }
});
