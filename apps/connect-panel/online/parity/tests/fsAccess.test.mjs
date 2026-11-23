import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { DirectoryAccessError, isSupported, loadHandle, pickOnlyReadDirectory, queryReadPermission, requestReadPermission, storeHandle, validateVaultRoot } from '../../js/fsAccess.js';

test('全站目录选择只有 fsAccess 一个真实调用点且强制只读', async () => {
  const matches = [];
  for (const name of await readdir(new URL('../../js/', import.meta.url))) {
    if (!name.endsWith('.js')) continue;
    const source = await readFile(new URL(`../../js/${name}`, import.meta.url), 'utf8');
    for (const match of source.matchAll(/showDirectoryPicker\s*\(/gu)) matches.push({ name, index: match.index });
  }
  assert.equal(matches.length, 1);
  assert.equal(matches[0].name, 'fsAccess.js');
});

const emptyVaultRoot = () => ({ kind: 'directory', getDirectoryHandle: async () => { throw new DOMException('缺失', 'NotFoundError'); }, entries: async function* () {} });

test('任意 Obsidian 库结构均可授权：不要求库内存在 2-Permanent；取消静默；非目录返回封闭错误码', async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'showDirectoryPicker');
  const originalActivation = Object.getOwnPropertyDescriptor(globalThis.navigator, 'userActivation');
  Object.defineProperty(globalThis.navigator, 'userActivation', { value: { isActive: true }, configurable: true });
  // PARA 之外的空库、纯中文目录库、扁平单层库都应通过根校验。
  const flat = { kind: 'directory', getDirectoryHandle: async () => { throw new DOMException('无 2-Permanent', 'NotFoundError'); }, entries: async function* () { yield ['第一篇笔记.md', { kind: 'file' }]; } };
  globalThis.showDirectoryPicker = async (options) => { assert.deepEqual(options, { mode: 'read' }); return flat; };
  assert.equal(await pickOnlyReadDirectory(), flat);
  const empty = emptyVaultRoot();
  assert.equal(await validateVaultRoot(empty), empty);
  globalThis.showDirectoryPicker = async () => { throw new DOMException('取消', 'AbortError'); };
  assert.equal(await pickOnlyReadDirectory(), null);
  globalThis.showDirectoryPicker = async () => ({ kind: 'file' });
  await assert.rejects(pickOnlyReadDirectory(), (error) => error instanceof DirectoryAccessError && error.code === 'VAULT_ROOT_INVALID');
  if (original) Object.defineProperty(globalThis, 'showDirectoryPicker', original); else delete globalThis.showDirectoryPicker;
  if (originalActivation) Object.defineProperty(globalThis.navigator, 'userActivation', originalActivation); else delete globalThis.navigator.userActivation;
});

test('句柄恢复清除失效授权，权限调用只申请 read', async () => {
  const handle = { kind: 'directory', getDirectoryHandle: async () => ({}), entries: async function* () {}, queryPermission: async (options) => options.mode, requestPermission: async (options) => options.mode };
  assert.equal(await queryReadPermission(handle), 'read');
  assert.equal(await requestReadPermission(handle), 'read');
  const saved = [];
  const store = { saveDirectoryHandle: async (value) => saved.push(value), loadDirectoryHandle: async () => handle, clearDirectoryHandle: async () => saved.push(null) };
  assert.equal(await storeHandle(store, handle), handle);
  assert.equal(await loadHandle(store), handle);
  assert.equal(isSupported({}), false);
});
