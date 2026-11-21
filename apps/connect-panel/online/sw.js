import { APP_SHELL_CACHE, cachedShell, installShell, isStaticShellRequest, removeOldShellCaches } from './js/pwa.js';

self.addEventListener('install', (event) => {
  event.waitUntil(installShell(self.caches).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  // 只升级静态 shell；IndexedDB 的审核队列与目录句柄永不由此删除。
  event.waitUntil(removeOldShellCaches(self.caches).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  // API、跨源、下载 Blob、目录/文件句柄及一切非白名单请求不进入 SW 缓存。
  if (!isStaticShellRequest(event.request, self.registration.scope)) return;
  event.respondWith(cachedShell(event.request, self.caches).catch(async () => {
    if (event.request.mode === 'navigate') return self.caches.match('./index.html', { cacheName: APP_SHELL_CACHE });
    return new Response('静态资源不可用，请恢复网络后重试。', { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
  }));
});
