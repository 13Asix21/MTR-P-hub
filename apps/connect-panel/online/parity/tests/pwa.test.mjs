import test from 'node:test';
import assert from 'node:assert/strict';
import { access, readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { APP_SHELL_CACHE, APP_SHELL_URLS, VENDOR_CACHE, VENDOR_HASH, cachedShell, installShell, isStaticShellRequest, removeOldShellCaches } from '../../js/pwa.js';

const root = new URL('../../', import.meta.url);

test('PWA standalone、本地 SVG/PNG、应用壳包含全部生产依赖且 vendor hash 匹配', async () => {
  const manifest = JSON.parse(await readFile(new URL('manifest.webmanifest', root), 'utf8'));
  assert.equal(manifest.display, 'standalone');
  assert.equal(manifest.start_url, './');
  assert.deepEqual(manifest.icons.map((icon) => icon.sizes), ['any', '192x192', '512x512', '512x512']);
  for (const url of APP_SHELL_URLS) await access(new URL(url, root));
  const vendor = await readFile(new URL('vendor/js-yaml.min.js', root));
  assert.equal(createHash('sha256').update(vendor).digest('hex'), VENDOR_HASH);
  const html = await readFile(new URL('index.html', root), 'utf8');
  assert.match(html, /rel="manifest"/u);
  assert.match(html, /rel="icon"/u);
});

test('SW 只缓存白名单同源静态 GET，不缓存 API、跨源、下载或文件', () => {
  const scope = 'https://workbench.example/review/';
  assert.equal(isStaticShellRequest({ method: 'GET', url: `${scope}js/ui.js?v=1` }, scope), true);
  assert.equal(isStaticShellRequest({ method: 'GET', url: `${scope}api/review` }, scope), false);
  assert.equal(isStaticShellRequest({ method: 'GET', url: `${scope}review/api/review` }, scope), false);
  assert.equal(isStaticShellRequest({ method: 'GET', url: 'https://workbench.example/review/js/ui.js' }, 'https://workbench.example/review'), false);
  assert.equal(isStaticShellRequest({ method: 'POST', url: `${scope}js/ui.js` }, scope), false);
  assert.equal(isStaticShellRequest({ method: 'GET', url: 'https://evil.example/js/ui.js' }, scope), false);
  assert.equal(isStaticShellRequest({ method: 'GET', url: `${scope}vault/2-Permanent/a.md` }, scope), false);
  assert.equal(isStaticShellRequest({ method: 'GET', url: 'blob:https://workbench.example/id' }, scope), false);
});

test('模拟缓存升级仅清理旧 shell/vendor，IndexedDB 删除函数不存在', async () => {
  const writes = [];
  const deleted = [];
  const cache = { addAll: async (urls) => writes.push(urls) };
  const caches = {
    open: async () => cache,
    keys: async () => ['mtrp-shell-old', 'mtrp-vendor-old', 'unrelated-cache'],
    delete: async (name) => deleted.push(name),
  };
  await installShell(caches);
  assert.equal(writes[0], APP_SHELL_URLS);
  await removeOldShellCaches(caches);
  assert.deepEqual(deleted.sort(), ['mtrp-shell-old', 'mtrp-vendor-old']);
  assert.equal(deleted.includes(APP_SHELL_CACHE), false);
  assert.equal(deleted.includes(VENDOR_CACHE), false);
  const source = await readFile(new URL('sw.js', root), 'utf8');
  assert.doesNotMatch(source, /indexedDB|deleteDatabase|pending_actions|export_backup|dir_handle|\/api\//u);
});

test('静态缓存优先在线更新，断网回退缓存且不伪造离线响应', async () => {
  const response = new Response('shell');
  const originalFetch = globalThis.fetch;
  let requests = 0;
  const writes = [];
  const caches = {
    match: async () => response,
    open: async () => ({ put: async (...args) => writes.push(args) }),
  };
  globalThis.fetch = async () => { requests += 1; return response; };
  try {
    assert.equal(await cachedShell({ url: 'x' }, caches), response);
    assert.equal(requests, 1);
    assert.equal(writes.length, 1);
    globalThis.fetch = async () => { throw new Error('offline'); };
    assert.equal(await cachedShell({ url: 'x' }, caches), response);
  } finally { globalThis.fetch = originalFetch; }
});

test('目录 API 保持只读，无云 SDK、网络台账或伪登录', async () => {
  const files = ['js/app.js', 'js/ui.js', 'js/fsAccess.js', 'js/browserScan.js', 'js/offlineBuffer.js', 'js/reviewActions.js'];
  for (const file of files) {
    const source = await readFile(new URL(file, root), 'utf8');
    assert.doesNotMatch(source, /createWritable|removeEntry|showOpenFilePicker|showSaveFilePicker|getWriter|CloudService|supabase|firebase|fetch\s*\(|XMLHttpRequest|mode:\s*['"]readwrite['"]/u, file);
  }
  const accessSource = await readFile(new URL('js/fsAccess.js', root), 'utf8');
  assert.match(accessSource, /showDirectoryPicker\(\{ mode: 'read' \}\)/u);
  assert.match(accessSource, /queryPermission\(\{ mode: 'read' \}\)/u);
  assert.match(accessSource, /requestPermission\(\{ mode: 'read' \}\)/u);
});
