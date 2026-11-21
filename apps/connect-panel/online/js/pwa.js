export const APP_SHELL_CACHE = 'mtrp-shell-o01-v2';
export const VENDOR_HASH = '45dc3dd03dc07a06705a2c2989b8c7f709013f04bd5386e3279d4e447f07ebd7';
export const VENDOR_CACHE = `mtrp-vendor-js-yaml-${VENDOR_HASH.slice(0, 16)}`;

// 精确同源白名单。不包含 /api、导出 Blob、File System Access 句柄或审核正文。
export const APP_SHELL_URLS = Object.freeze([
  './', './index.html', './css/styles.css', './manifest.webmanifest',
  './icons/icon.svg', './icons/icon-192.png', './icons/icon-512.png', './icons/maskable-512.png',
  './js/app.js', './js/browserScan.js', './js/export.js', './js/fsAccess.js', './js/hasher.js',
  './js/offlineBuffer.js', './js/parser.js', './js/pwa.js', './js/pythonCasefold.js',
  './js/reviewActions.js', './js/reviewDomain.js', './js/reviewWorkflow.js', './js/scanner.js',
  './js/scanWorker.js', './js/ui.js', './vendor/js-yaml.min.js', './vendor/js-yaml.mjs',
  './vendor/js-yaml.LICENSE',
]);

export function isStaticShellRequest(request, scopeUrl) {
  if (request.method !== 'GET') return false;
  const url = new URL(request.url, scopeUrl);
  const scope = new URL(scopeUrl);
  if (url.origin !== scope.origin || !url.pathname.startsWith(scope.pathname)) return false;
  const relativePath = url.pathname.slice(scope.pathname.length);
  if (relativePath.startsWith('api/')) return false;
  return APP_SHELL_URLS.includes(`./${relativePath}`);
}

/** 核心独立导出便于无 ServiceWorker 全局环境做精确模拟。 */
export async function installShell(caches, cacheNames = APP_SHELL_CACHE, vendorCache = VENDOR_CACHE) {
  const cache = await caches.open(cacheNames);
  await cache.addAll(APP_SHELL_URLS);
  const vendor = await caches.open(vendorCache);
  // 厂商文件与源 SHA 同版本，缓存名变化才会替换；不混入应用动态数据。
  await vendor.addAll(['./vendor/js-yaml.min.js', './vendor/js-yaml.mjs']);
}

export async function removeOldShellCaches(caches, keep = [APP_SHELL_CACHE, VENDOR_CACHE]) {
  const names = await caches.keys();
  await Promise.all(names.filter((name) => name.startsWith('mtrp-shell-') || name.startsWith('mtrp-vendor-'))
    .filter((name) => !keep.includes(name)).map((name) => caches.delete(name)));
  // 明确没有 indexedDB.deleteDatabase、caches.delete 审核数据或目录句柄操作。
}

export async function cachedShell(request, caches, cacheNames = APP_SHELL_CACHE) {
  try {
    // 在线时优先取新 shell 并更新自身静态缓存，避免旧版本模块跨文件混用。
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheNames);
      await cache.put(request, response.clone());
    }
    return response;
  } catch {
    const match = await caches.match(request, { cacheName: cacheNames, ignoreSearch: true });
    if (match) return match;
    throw new Error('STATIC_SHELL_OFFLINE');
  }
}
