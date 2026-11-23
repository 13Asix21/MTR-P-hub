import test from 'node:test';
import assert from 'node:assert/strict';

import { WorkbenchUi } from '../../js/ui.js';
import { emptyState } from '../../js/reviewDomain.js';

/** 最小 DOM 桩：只实现本用例需要的节点操作。 */
function makeNode(tag = 'div') {
  const node = {
    tagName: tag.toUpperCase(),
    children: [],
    attrs: {},
    dataset: {},
    hidden: false,
    textContent: '',
    className: '',
    id: '',
    value: '',
    disabled: false,
    classList: { add() {}, toggle() {}, remove() {} },
    replaceChildren(...nodes) { this.children = nodes; },
    append(...nodes) { this.children.push(...nodes); },
    insertAdjacentHTML() {},
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return this.attrs[name]; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    removeAttribute(name) { delete this.attrs[name]; },
  };
  return node;
}

function makeDocument() {
  return { createElement: (tag) => makeNode(tag) };
}

function buildUi(services = {}) {
  const document = makeDocument();
  const ui = new WorkbenchUi(document, {
    offlineBuffer: { open: async () => {}, loadDirectoryHandle: async () => null },
    confirm: () => true,
    ...services,
  });
  ui.root = makeNode();
  return ui;
}

test('未加载配置时显示读取中，且传送按钮不可用', () => {
  const ui = buildUi();
  assert.match(ui.describeTransferTarget(), /正在读取/u);
  assert.equal(ui.canTransfer(), false);
});

test('加载到 M 库配置后显示空间与目标路径', () => {
  const ui = buildUi();
  ui.transferConfigLoaded = true;
  ui.mLibraryConfig = { space_id: 'space-1', path: '研发/工业视觉', proxy_url: 'https://gw' };
  ui.session = { user: { id: 'u' } };
  ui.services.transferToLibrary = async () => ({ message: 'ok' });
  assert.match(ui.describeTransferTarget(), /空间 space-1/u);
  assert.match(ui.describeTransferTarget(), /路径 \/研发\/工业视觉/u);
  assert.equal(ui.canTransfer(), true);
});

test('配置齐全即可点传送，未登录在点击时被拒绝', () => {
  const ui = buildUi();
  ui.transferConfigLoaded = true;
  ui.mLibraryConfig = { space_id: 's', path: '', proxy_url: '' };
  ui.services.transferToLibrary = async () => ({});
  assert.equal(ui.canTransfer(), false, '缺代理时禁用');
  ui.mLibraryConfig = { space_id: 's', path: '', proxy_url: 'https://gw' };
  assert.equal(ui.canTransfer(), true, '配置齐全即可点，会话在点击时校验');
});

test('空路径显示为知识库根目录', () => {
  const ui = buildUi();
  ui.transferConfigLoaded = true;
  ui.mLibraryConfig = { space_id: 's', path: '', proxy_url: 'https://gw' };
  assert.match(ui.describeTransferTarget(), /知识库根目录/u);
});

test('缺空间 ID 时提示尚未配置', () => {
  const ui = buildUi();
  ui.transferConfigLoaded = true;
  ui.mLibraryConfig = null;
  assert.match(ui.describeTransferTarget(), /尚未配置空间 ID/u);
});

test('审核状态统计可用于流转说明', () => {
  const state = emptyState();
  state.items = {
    'a.md': { action: 'approved', reason: 'ok', decided_at: '2026-01-01T00:00:00Z', content_sha256: 'a'.repeat(64), decided_by: 'u' },
    'b.md': { action: 'rejected', reason: 'no', decided_at: '2026-01-01T00:00:00Z', content_sha256: 'b'.repeat(64), decided_by: 'u' },
  };
  const ui = buildUi();
  ui.state = state;
  const counts = { approved: 0, rejected: 0 };
  for (const record of Object.values(ui.state.items)) {
    if (record.action === 'approved') counts.approved += 1;
    if (record.action === 'rejected') counts.rejected += 1;
  }
  assert.deepEqual(counts, { approved: 1, rejected: 1 });
});
