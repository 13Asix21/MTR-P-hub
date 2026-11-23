import test from 'node:test';
import assert from 'node:assert/strict';

import { LibraryConfigError } from '../../js/librarySettings.js';
import { noteTitle, splitForTransfer, transferApprovedNote, TRANSFER_PATH } from '../../js/transfer.js';

const session = { user: { id: 'u1', name: '雷爹' } };
const library = { space_id: 'space-1', path: '研发/工业视觉', proxy_url: 'https://gw.example.com/mtrp', sharecode: 'SC1234' };

function detail(raw = '---\ntags: [mtr/trial]\n---\n# 标题\n\n正文', sha = 'a'.repeat(64)) {
  return { raw_text: raw, item: { sha256: sha } };
}

test('版本：去掉 frontmatter 后只发送正文', () => {
  const out = splitForTransfer('---\ntitle: x\n---\n# 正文');
  assert.equal(out.frontmatter, 'title: x');
  assert.equal(out.body, '# 正文');
  const none = splitForTransfer('# 无 frontmatter');
  assert.equal(none.frontmatter, '');
  assert.equal(none.body, '# 无 frontmatter');
});

test('标题取文件名并去掉 .md', () => {
  assert.equal(noteTitle('2-Permanent/视觉检测/缺陷检测现状.md'), '缺陷检测现状');
  assert.equal(noteTitle('无扩展名'), '无扩展名');
});

test('未配置空间、代理、登录时明确拒绝，不发请求', async () => {
  let called = false;
  const fetcher = async () => { called = true; return { ok: true, json: async () => ({}) }; };
  const base = { path: 'a/b.md', detail: detail(), session };
  await assert.rejects(
    transferApprovedNote({ ...base, library: { ...library, space_id: '' } }, fetcher),
    (e) => e.code === 'M_NOT_CONFIGURED',
  );
  await assert.rejects(
    transferApprovedNote({ ...base, library: { ...library, proxy_url: '' } }, fetcher),
    (e) => e.code === 'M_PROXY_REQUIRED',
  );
  await assert.rejects(
    transferApprovedNote({ ...base, library, session: null }, fetcher),
    (e) => e.code === 'LOGIN_REQUIRED',
  );
  assert.equal(called, false);
});

test('正常传送：带上目标路径与来源 SHA，不含 token', async () => {
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, json: async () => ({ ok: true, entry_id: 'entry-42' }) };
  };
  const result = await transferApprovedNote({ path: '2-Permanent/x.md', detail: detail(), library, session }, fetcher);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `https://gw.example.com/mtrp${TRANSFER_PATH}?sharecode=SC1234`);
  const body = JSON.parse(calls[0].init.body);
  assert.equal(body.space_id, 'space-1');
  assert.equal(body.path, '研发/工业视觉');
  assert.equal(body.title, 'x');
  assert.equal(body.source_path, '2-Permanent/x.md');
  assert.equal(body.content_sha256, 'a'.repeat(64));
  assert.equal(body.markdown, '# 标题\n\n正文');
  assert.equal('token' in body, false, '不得把凭据发给代理');
  assert.equal(JSON.stringify(body).match(/token|authorization/iu), null);
  assert.match(result.message, /entry-42/u);
});

test('代理不可达与 HTTP 失败给出可读错误', async () => {
  await assert.rejects(
    transferApprovedNote({ path: 'a.md', detail: detail(), library, session }, async () => { throw new Error('Failed to fetch'); }),
    (e) => e.code === 'PROXY_UNREACHABLE',
  );
  await assert.rejects(
    transferApprovedNote({ path: 'a.md', detail: detail(), library, session }, async () => ({ ok: false, status: 502, json: async () => ({}) })),
    (e) => e.code === 'TRANSFER_FAILED',
  );
  await assert.rejects(
    transferApprovedNote({ path: 'a.md', detail: detail(), library, session }, async () => ({ ok: true, json: async () => ({ ok: false, message: '空间不存在' }) })),
    (e) => e.code === 'TRANSFER_REJECTED',
  );
});

test('空正文与缺 SHA 不发起传送', async () => {
  let called = false;
  await assert.rejects(
    transferApprovedNote({ path: 'a.md', detail: { raw_text: '', item: { sha256: 'x' } }, library, session }, async () => { called = true; }),
    (e) => e.code === 'EMPTY_CONTENT',
  );
  assert.equal(called, false);
});

test('代理地址已含 /mtrp/transfer 时不重复拼接', async () => {
  const calls = [];
  const fetcher = async (url) => { calls.push(url); return { ok: true, json: async () => ({ ok: true, entry_id: 'e' }) }; };
  await transferApprovedNote(
    { path: 'a.md', detail: detail(), library: { ...library, proxy_url: 'https://p/mtrp/transfer' }, session },
    fetcher,
  );
  assert.equal(calls[0], 'https://p/mtrp/transfer?sharecode=SC1234');
});

test('缺少代理 sharecode 时拒绝发送', async () => {
  let called = false;
  await assert.rejects(
    transferApprovedNote({ path: 'a.md', detail: detail(), library: { ...library, sharecode: '' }, session }, async () => { called = true; }),
    (e) => e.code === 'SHARECODE_REQUIRED',
  );
  assert.equal(called, false);
});

test('403 明确提示 sharecode 失效', async () => {
  await assert.rejects(
    transferApprovedNote({ path: 'a.md', detail: detail(), library, session }, async () => ({ ok: false, status: 403, json: async () => ({}) })),
    (e) => e.code === 'SHARECODE_REJECTED',
  );
});

test('整篇导入：用字节数确认未截断，并说明改名结果', async () => {
  const fetcher = async () => ({ ok: true, json: async () => ({ ok: true, entry_id: 'e1', bytes: 16838, renamed: true }) });
  const r = await transferApprovedNote({ path: 'a/b.md', detail: detail(), library, session }, fetcher);
  assert.match(r.message, /已整篇传入/u);
  assert.equal(r.bytes, 16838);
  assert.match(r.message, /16838 字节/u);
});

test('改名未生效时如实提示，不宣称成功改名', async () => {
  const fetcher = async () => ({ ok: true, json: async () => ({ ok: true, entry_id: 'e2', bytes: 900, renamed: false }) });
  const r = await transferApprovedNote({ path: 'a.md', detail: detail(), library, session }, fetcher);
  assert.match(r.message, /标题改名未生效/u);
});

test('代理返回警告时一并透出', async () => {
  const fetcher = async () => ({ ok: true, json: async () => ({ ok: true, entry_id: 'e3', bytes: 10, renamed: true, warning: '权限不足' }) });
  const r = await transferApprovedNote({ path: 'a.md', detail: detail(), library, session }, fetcher);
  assert.match(r.message, /权限不足/u);
});
