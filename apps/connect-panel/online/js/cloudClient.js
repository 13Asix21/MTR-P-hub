/**
 * 云服务客户端封装：静态页面走 CDN 形式的 WorkBuddy Cloud SDK。
 * endpoint / publishableKey / oauthRelayBaseUrl 全部来自 workbuddy_cloud_service 返回的 publicConfig，
 * 不在源码里硬编码，也不从 location 或环境变量推断。
 */
export const PUBLIC_CONFIG = Object.freeze({
  resourceId: 'wbcs_y6GgboBEjJV4klPCDTDUGv',
  endpoint: 'https://mtrp-review-workbench.app.workbuddy.host',
  oauthRelayBaseUrl: 'https://www.workbuddy.cn/v2/as/genie-baas/oauth',
  publishableKey: 'wbpk_iNmjdm65daTU0MIeuXIXYp_hd3n8HH291nBbWUJ8umGfwgT4yvX25bO',
});

export const LIBRARIES = Object.freeze([
  { id: 'm', name: 'M 库 · 乐享知识库', hint: '长期知识沉淀，只有服务端代理能访问，需填空间 ID 与代理地址' },
  { id: 't', name: 'T 库 · Obsidian 试运行区', hint: '填库内相对目录；整库留空表示扫描整个库' },
  { id: 'r', name: 'R 库 · Obsidian 冷区', hint: '可与 T 共用同一个 Obsidian 库，填写不同的标签或目录' },
  { id: 'p', name: 'P 库 · GitHub 项目资产', hint: '填写 owner/repo 与分支；公开仓库无需 token' },
]);

let clientPromise = null;
let sdkPromise = null;

function loadSdk() {
  if (globalThis.WorkBuddyCloud) return Promise.resolve(globalThis.WorkBuddyCloud);
  if (sdkPromise) return sdkPromise;
  sdkPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://cdn.jsdelivr.net/npm/@tencent-ai/workbuddy-cloud-sdk@dev/lib/index.global.js';
    script.async = true;
    script.onload = () => (globalThis.WorkBuddyCloud
      ? resolve(globalThis.WorkBuddyCloud)
      : reject(new Error('云 SDK 已加载但未暴露全局对象。')));
    script.onerror = () => reject(new Error('云 SDK 加载失败，请检查网络后重试。'));
    document.head.append(script);
  });
  return sdkPromise;
}

/** 初始化一次，Auth / Database / Storage 共用同一个实例。 */
export function getCloud() {
  if (clientPromise) return clientPromise;
  clientPromise = loadSdk().then((sdk) => sdk.createWorkBuddyCloud({
    endpoint: PUBLIC_CONFIG.endpoint,
    oauthRelayBaseUrl: PUBLIC_CONFIG.oauthRelayBaseUrl,
    publishableKey: PUBLIC_CONFIG.publishableKey,
  }));
  return clientPromise;
}

/** 云服务只在正式发布域可用；localhost 与预览页会被拒。 */
export function cloudAvailable(scope = globalThis) {
  return Boolean(scope.isSecureContext && scope.crypto?.subtle);
}

export async function currentSession() {
  const cloud = await getCloud();
  const { data, error } = await cloud.auth.getSession();
  if (error) return { session: null, error };
  return { session: data ?? null, error: null };
}

export function signInCallback(cloud) {
  const url = new URL(globalThis.location.href);
  if (!url.searchParams.has('code') && !url.searchParams.has('error')) return { pending: false };
  const provider = url.searchParams.get('provider');
  if (provider !== 'workbuddy' && provider !== 'wechat') {
    return { pending: false, error: new Error('授权回调缺少有效的登录方式，请重新登录。') };
  }
  const handler = provider === 'workbuddy'
    ? cloud.auth.handleWorkBuddyCallback
    : cloud.auth.handleWechatWebCallback;
  if (typeof handler !== 'function') {
    return { pending: false, error: new Error('当前云 SDK 版本不支持该登录方式。') };
  }
  return { pending: true, provider, handler };
}

/** 成功后只清 code/state/provider，保留 sharecode 等业务参数。 */
export function clearCallbackParams() {
  const url = new URL(globalThis.location.href);
  for (const key of ['code', 'state', 'provider']) url.searchParams.delete(key);
  globalThis.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
}
