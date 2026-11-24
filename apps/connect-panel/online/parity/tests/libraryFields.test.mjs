import test from 'node:test';
import assert from 'node:assert/strict';

import { ADVANCED_FIELDS, FIELD_HELP, buildConfig, defaultsFor, toFormValues } from '../../js/libraryFields.js';

test('M 库默认值已能直接连通，无需手填', () => {
  const d = defaultsFor('m');
  assert.equal(d.space_id, 'bda1340e5541477d9698d82dc9a4b451', '默认应为真实的 M库范式');
  assert.equal(d.proxy_url, 'https://mtrp-lexiang-proxy.app.workbuddy.host');
  assert.equal(d.sharecode, 'QOPTB45RQL');
});

test('P 库默认值只需一个 repo', () => {
  const d = defaultsFor('p');
  assert.equal(d.repo, '13Asix21/MTR-P-hub');
  assert.equal(d.branch, 'main');
});

test('P 库把 owner/repo 合并成一格再拆回', () => {
  const config = buildConfig('p', { repo: '13Asix21/MTR-P-hub', branch: 'main', proxy_url: '' });
  assert.deepEqual(config, { owner: '13Asix21', repo: 'MTR-P-hub', branch: 'main', proxy_url: '', sharecode: '' });
  const back = toFormValues('p', config);
  assert.equal(back.repo, '13Asix21/MTR-P-hub');
});

test('P 库接受完整 GitHub URL 并自动清理', () => {
  const config = buildConfig('p', { repo: 'https://github.com/13Asix21/MTR-P-hub.git' });
  assert.equal(config.owner, '13Asix21');
  assert.equal(config.repo, 'MTR-P-hub');
});

test('P 库空分支回退 main', () => {
  assert.equal(buildConfig('p', { repo: 'a/b', branch: '   ' }).branch, 'main');
});

test('T/R 库只需要一个目录，其余留空', () => {
  const config = buildConfig('t', { vault_root: '2-Permanent' });
  assert.deepEqual(config, { mode: 'local', vault_root: '2-Permanent', mcp_endpoint: '', managed_roots: [], tags: [] });
  assert.equal(buildConfig('r', {}).vault_root, '');
  assert.equal(buildConfig('r', {}).mode, 'local');
});

test('每个必填项都提供了获取引导或默认值', () => {
  for (const [library, fields] of Object.entries(FIELD_HELP)) {
    for (const field of fields) {
      if (!field.required) continue;
      const hasDefault = Boolean(field.defaultValue);
      const hasLink = Boolean(field.helpLink);
      assert.ok(hasDefault || hasLink, `${library}.${field.key} 必填项既无默认值也无获取链接`);
    }
  }
});

test('M 库常规字段含目标切换，代理细节仍收进高级', () => {
  const keys = FIELD_HELP.m.map((f) => f.key);
  assert.deepEqual(keys, ['target', 'space_id', 'path', 'company_path', 'name_prefix']);
  assert.equal(FIELD_HELP.m[0].defaultValue, 'personal', '默认走个人库');
  assert.deepEqual(ADVANCED_FIELDS.m.map((f) => f.key), ['proxy_url', 'sharecode']);
});

test('公司库字段只在 target=company 时显示', () => {
  const byKey = Object.fromEntries(FIELD_HELP.m.map((f) => [f.key, f]));
  assert.equal(byKey.company_path.onlyTarget, 'company');
  assert.equal(byKey.name_prefix.onlyTarget, 'company');
  assert.equal(byKey.path.onlyTarget, 'personal');
  assert.equal(byKey.space_id.onlyTarget, undefined, '知识库 ID 两种目标共用');
});

test('T/R 没有高级字段，界面不会出现空折叠', () => {
  assert.equal(ADVANCED_FIELDS.t.length, 0);
  assert.equal(ADVANCED_FIELDS.r.length, 0);
});

import { extractSpaceId } from '../../js/libraryFields.js';

test('从乐享知识库链接识别 32 位空间 ID', () => {
  const id = 'bda1340e5541477d9698d82dc9a4b451';
  assert.equal(extractSpaceId(`https://lexiang.tencent.com/space/${id}`).id, id);
  assert.equal(extractSpaceId(`https://x.com/kb/${id}?tab=1`).id, id);
  assert.equal(extractSpaceId(`https://x.com/page?space_id=${id}`).id, id);
  assert.equal(extractSpaceId(id).id, id);
  assert.equal(extractSpaceId(`  ${id.toUpperCase()}  `).id, id, '大写应归一为小写');
});

test('无法识别时给出明确提示，不返回空串冒充成功', () => {
  const r = extractSpaceId('这一段没有 ID');
  assert.equal(r.id, '');
  assert.match(r.error, /没能/u);
  const empty = extractSpaceId('');
  assert.equal(empty.id, '');
  assert.match(empty.error, /请粘贴/u);
});
