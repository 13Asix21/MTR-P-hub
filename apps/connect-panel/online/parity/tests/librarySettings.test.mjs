import test from 'node:test';
import assert from 'node:assert/strict';

import {
  LibraryConfigError,
  LIBRARY_IDS,
  deleteConnection,
  emptyConfig,
  loadConnections,
  normalizeConfig,
  probeGithub,
  rowToEntry,
  saveConnection,
} from '../../js/librarySettings.js';

test('四库 ID 与空配置齐全', () => {
  assert.deepEqual(LIBRARY_IDS, ['m', 't', 'r', 'p']);
  assert.deepEqual(emptyConfig('t'), { mode: 'local', vault_root: '', mcp_endpoint: '', managed_roots: [], tags: [] });
  assert.deepEqual(emptyConfig('p'), { owner: '', repo: '', branch: 'main', proxy_url: '', sharecode: '' });
});

test('拒绝把 token 等凭据写进云端配置', () => {
  for (const key of ['token', 'access_token', 'pat', 'secret', 'password', 'authorization']) {
    assert.throws(
      () => normalizeConfig('p', { owner: '13Asix21', repo: 'MTR-P-hub', [key]: 'abc' }),
      (error) => error instanceof LibraryConfigError && error.code === 'SECRET_NOT_ALLOWED',
      `${key} 应被拒绝`,
    );
  }
});

test('Obsidian 目录只接受相对路径', () => {
  assert.equal(normalizeConfig('t', { vault_root: ' 2-Permanent/ ' }).vault_root, '2-Permanent');
  assert.equal(normalizeConfig('t', { vault_root: '' }).vault_root, '');
  for (const bad of ['C:/Users/x', '/abs/path', '../escape', 'file:///x']) {
    assert.throws(
      () => normalizeConfig('r', { vault_root: bad }),
      (error) => error instanceof LibraryConfigError && error.code === 'VAULT_ROOT_INVALID',
      `${bad} 应被拒绝`,
    );
  }
});

test('目录与标签按逗号切分并去空', () => {
  const config = normalizeConfig('t', {
    vault_root: '',
    managed_roots: ['Inbox', ' ', ' 3-Knowledge-Graph '],
    tags: ['mtr/trial', ''],
  });
  assert.deepEqual(config.managed_roots, ['Inbox', '3-Knowledge-Graph']);
  assert.deepEqual(config.tags, ['mtr/trial']);
});

test('GitHub 配置校验 owner/repo/branch 与代理地址', () => {
  const ok = normalizeConfig('p', { owner: '13Asix21', repo: 'MTR-P-hub', branch: 'main' });
  assert.equal(ok.owner, '13Asix21');
  assert.equal(ok.branch, 'main');
  assert.equal(normalizeConfig('p', {}).branch, 'main');
  assert.throws(() => normalizeConfig('p', { owner: 'bad owner!' }), /GitHub 所有者/u);
  assert.throws(() => normalizeConfig('p', { owner: 'a', repo: 'b', proxy_url: 'ftp://x' }), /代理地址/u);
});

test('乐享代理地址必须是 http(s)', () => {
  assert.equal(normalizeConfig('m', { space_id: ' sp1 ', proxy_url: 'https://gw/x' }).space_id, 'sp1');
  assert.throws(() => normalizeConfig('m', { proxy_url: 'gw/x' }), /代理地址/u);
});

function fakeCloud({ rows = [], failOn = null } = {}) {
  const calls = [];
  const cloud = {
    calls,
    database: {
      from(table) {
        assert.equal(table, 'library_connections');
        const builder = {
          select() { return builder; },
          order() { return builder; },
          eq() { return builder; },
          upsert(payload, options) { calls.push({ op: 'upsert', payload, options }); builder._write = payload; return builder; },
          delete() { calls.push({ op: 'delete' }); return builder; },
          async then(resolve) {
            if (failOn) return resolve({ data: null, error: failOn });
            return resolve({ data: builder._write ? [builder._write] : rows, error: null });
          },
        };
        return builder;
      },
    },
  };
  return cloud;
}

test('读取云端配置：缺失库补空，已存在的行转成面板对象', async () => {
  const cloud = fakeCloud({
    rows: [
      { library: 't', label: '我的库', config: { vault_root: '2-Permanent' }, enabled: true, updated_at: '2026-11-23T00:00:00Z' },
      { library: 'p', label: '', config: { owner: '13Asix21', repo: 'MTR-P' }, enabled: false, updated_at: null },
    ],
  });
  const map = await loadConnections(cloud);
  assert.equal(map.t.saved, true);
  assert.equal(map.t.config.vault_root, '2-Permanent');
  assert.equal(map.p.saved, true);
  assert.equal(map.m.saved, false);
  assert.deepEqual(map.m.config, emptyConfig('m'));
  assert.equal(rowToEntry({ library: 'm', config: null, enabled: undefined }).enabled, true);
});

test('保存配置不发送 owner_id，uuid 冲突按 library 合并', async () => {
  const cloud = fakeCloud();
  const saved = await saveConnection(cloud, 'p', {
    config: { owner: '13Asix21', repo: 'MTR-P-hub' },
    ownerName: '雷爹',
  });
  const call = cloud.calls.find((item) => item.op === 'upsert');
  assert.equal('owner_id' in call.payload, false, '客户端不得发送 owner_id');
  assert.equal(call.payload.owner_name, '雷爹');
  assert.deepEqual(call.options, { onConflict: 'owner_id,library' });
  assert.equal(saved.library, 'p');
});

test('保存前先校验：带 token 的配置不会发出请求', async () => {
  const cloud = fakeCloud();
  await assert.rejects(
    saveConnection(cloud, 'm', { config: { space_id: 'x', token: 'secret' } }),
    (error) => error.code === 'SECRET_NOT_ALLOWED',
  );
  assert.equal(cloud.calls.length, 0);
});

test('写入被 RLS 过滤时明确报错，不伪报成功', async () => {
  const cloud = fakeCloud();
  cloud.database.from = () => ({
    upsert() { return this; },
    select() { return this; },
    async then(resolve) { resolve({ data: [], error: null }); },
  });
  await assert.rejects(
    saveConnection(cloud, 'p', { config: { owner: 'a', repo: 'b' } }),
    (error) => error.code === 'NOT_ALLOWED',
  );
});

test('数据库错误码转成可读错误', async () => {
  const cloud = fakeCloud({ failOn: { code: '42501', message: 'permission denied' } });
  await assert.rejects(loadConnections(cloud), /读取云端连接配置失败/u);
  const cloud2 = fakeCloud({ failOn: { code: '42501', message: 'denied' } });
  await assert.rejects(
    saveConnection(cloud2, 'm', { config: { space_id: 's' } }),
    (error) => error.code === 'NOT_ALLOWED',
  );
});

test('删除返回是否真的删到行', async () => {
  const cloud = fakeCloud({ rows: [] });
  assert.equal(await deleteConnection(cloud, 'm'), false);
  const cloud2 = fakeCloud({ rows: [{ library: 'm' }] });
  assert.equal(await deleteConnection(cloud2, 'm'), true);
});

test('GitHub 探活：公开仓库直连，404 与限流有明确提示', async () => {
  const calls = [];
  const fetcher = async (url) => {
    calls.push(url);
    if (url.includes('missing')) return { status: 404, ok: false };
    return { status: 200, ok: true, json: async () => ({ full_name: '13Asix21/MTR-P-hub', default_branch: 'main', private: false, pushed_at: '2026-11-23' }) };
  };
  const info = await probeGithub({ owner: '13Asix21', repo: 'MTR-P-hub', proxy_url: '' }, fetcher);
  assert.equal(info.full_name, '13Asix21/MTR-P-hub');
  assert.match(calls[0], /^https:\/\/api\.github\.com\/repos\/13Asix21\/MTR-P-hub$/u);
  await assert.rejects(
    probeGithub({ owner: 'a', repo: 'missing', proxy_url: '' }, fetcher),
    (error) => error.code === 'GITHUB_NOT_FOUND',
  );
  await probeGithub({ owner: 'a', repo: 'b', proxy_url: 'https://gw/mtrp' }, fetcher);
  assert.match(calls.at(-1), /^https:\/\/gw\/mtrp\/repos\/a\/b$/u);
});

test('未填仓库时拒绝探活', async () => {
  await assert.rejects(
    probeGithub({ owner: '', repo: '', proxy_url: '' }, async () => { throw new Error('不应发起请求'); }),
    (error) => error.code === 'GITHUB_INCOMPLETE',
  );
});

test('GitHub token 不会被拼进云端配置（buildConfig 剔除本机字段）', async () => {
  const { buildConfig } = await import('../../js/libraryFields.js');
  const config = buildConfig('p', { repo: 'a/b', github_token: 'ghp_secret' });
  assert.equal('github_token' in config, false, 'token 不得进入云端配置');
  assert.equal(JSON.stringify(config).includes('ghp_'), false);
});

test('若误把 token 塞进配置，normalizeConfig 会主动拒绝而非静默保存', () => {
  assert.throws(
    () => normalizeConfig('p', { owner: 'a', repo: 'b', github_token: 'ghp_secret' }),
    (e) => e.code === 'SECRET_NOT_ALLOWED',
  );
});

test('GitHub token 本机读写可往返，清除后为空', async () => {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => store.set(k, v),
    removeItem: (k) => store.delete(k),
  };
  const { rememberGithubToken, readGithubToken } = await import('../../js/librarySettings.js');
  rememberGithubToken('ghp_x');
  assert.equal(readGithubToken(), 'ghp_x');
  rememberGithubToken('');
  assert.equal(readGithubToken(), '');
  delete globalThis.localStorage;
});

test('有代理时走 github-check 并带回可写性', async () => {
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url, headers: init?.headers ?? {} });
    return { ok: true, status: 200, json: async () => ({ ok: true, writable: true, message: '可读写：a/b' }) };
  };
  globalThis.localStorage = { getItem: () => 'ghp_t', setItem: () => {}, removeItem: () => {} };
  const { checkGithubAccess } = await import('../../js/librarySettings.js');
  const r = await checkGithubAccess({ owner: 'a', repo: 'b', proxy_url: 'https://p', sharecode: 'SC1' }, fetcher);
  assert.equal(r.writable, true);
  assert.match(calls[0].url, /\/mtrp\/github-check\?sharecode=SC1&owner=a&repo=b/u);
  assert.equal(calls[0].headers['x-mtrp-github-token'], 'ghp_t');
  delete globalThis.localStorage;
});

test('GitHub 未配代理时明确要求走 MCP 代理，不再匿名兜底', async () => {
  const { checkGithubAccess } = await import('../../js/librarySettings.js');
  await assert.rejects(
    checkGithubAccess({ owner: 'a', repo: 'b', proxy_url: '', sharecode: '' }, async () => { throw new Error('不应发请求'); }),
    (e) => e.code === 'GITHUB_PROXY_REQUIRED',
  );
  await assert.rejects(
    checkGithubAccess({ owner: 'a', repo: 'b', proxy_url: 'https://p', sharecode: '' }, async () => { throw new Error('不应发请求'); }),
    (e) => e.code === 'GITHUB_PROXY_REQUIRED',
  );
});

test('GitHub 经代理返回 transport=mcp 时透传出来', async () => {
  const { checkGithubAccess } = await import('../../js/librarySettings.js');
  const fetcher = async () => ({ ok: true, status: 200, json: async () => ({ ok: true, transport: 'mcp', message: '连接正常（MCP）：a/b' }) });
  const out = await checkGithubAccess({ owner: 'a', repo: 'b', proxy_url: 'https://p', sharecode: 'SC' }, fetcher);
  assert.equal(out.transport, 'mcp');
  assert.match(out.message, /MCP/u);
});

test('P 库 sharecode 会随配置保存，不被丢弃', () => {
  const config = normalizeConfig('p', { owner: 'a', repo: 'b', proxy_url: 'https://p', sharecode: 'QOPTB45RQL' });
  assert.equal(config.sharecode, 'QOPTB45RQL');
  assert.equal(emptyConfig('p').sharecode, '');
});

test('P 库 sharecode 格式非法时拒绝', () => {
  assert.throws(
    () => normalizeConfig('p', { owner: 'a', repo: 'b', sharecode: 'bad code!' }),
    (e) => e.code === 'SHARECODE_INVALID',
  );
});
