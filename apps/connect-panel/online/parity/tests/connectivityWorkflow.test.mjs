import test from 'node:test';
import assert from 'node:assert/strict';

import {
  STEP_STATE,
  WORKFLOW_STEPS,
  checkGithub,
  checkM,
  checkVault,
  summarize,
} from '../../js/connectivityWorkflow.js';

test('工作流步骤顺序固定且含五个环节', () => {
  assert.deepEqual(WORKFLOW_STEPS.map((s) => s.id), ['m', 't', 'r', 'p', 'agent']);
});

test('M 库缺空间/代理/访问码时逐项报错', () => {
  assert.equal(checkM(null).state, STEP_STATE.failed);
  assert.match(checkM({ space_id: '' }).message, /知识库 ID/u);
  assert.match(checkM({ space_id: 's' }).message, /代理地址/u);
  assert.match(checkM({ space_id: 's', proxy_url: 'https://p' }).message, /访问码/u);
  assert.equal(checkM({ space_id: 's', proxy_url: 'https://p', sharecode: 'c' }).state, STEP_STATE.ok);
});

test('T/R 本地模式只需目录，空目录表示整库', () => {
  const r = checkVault({ mode: 'local', vault_root: '' });
  assert.equal(r.state, STEP_STATE.ok);
  assert.match(r.message, /整个库/u);
  assert.match(checkVault({ mode: 'local', vault_root: '2-Permanent' }).message, /2-Permanent/u);
});

test('T/R MCP 模式必须给出合法端点', () => {
  assert.match(checkVault({ mode: 'mcp', mcp_endpoint: '' }).message, /缺少端点/u);
  assert.equal(checkVault({ mode: 'mcp', mcp_endpoint: 'ftp://x' }).state, STEP_STATE.failed);
  const ok = checkVault({ mode: 'mcp', mcp_endpoint: 'https://mcp.example.com/mcp' });
  assert.equal(ok.state, STEP_STATE.ok);
  assert.match(ok.message, /mcp\.example\.com/u);
});

test('未知模式明确失败，不静默当成本地', () => {
  assert.equal(checkVault({ mode: 's3' }).state, STEP_STATE.failed);
});

test('未配置 T/R 视为跳过而非失败', () => {
  assert.equal(checkVault(null).state, STEP_STATE.skipped);
});

test('GitHub 缺仓库信息失败，齐全则待测', () => {
  assert.equal(checkGithub({ owner: '', repo: '' }).state, STEP_STATE.failed);
  const ok = checkGithub({ owner: 'a', repo: 'b' });
  assert.equal(ok.state, STEP_STATE.ok);
  assert.match(ok.message, /a\/b/u);
});

test('汇总统计正确判断全通过', () => {
  const all = summarize([
    { state: STEP_STATE.ok },
    { state: STEP_STATE.ok },
  ]);
  assert.equal(all.allPassed, true);
  const mixed = summarize([{ state: STEP_STATE.ok }, { state: STEP_STATE.failed }]);
  assert.equal(mixed.allPassed, false);
  assert.equal(mixed.failed, 1);
  const skippedOnly = summarize([{ state: STEP_STATE.skipped }]);
  assert.equal(skippedOnly.allPassed, false, '全跳过不算通过');
});
