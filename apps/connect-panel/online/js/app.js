import { createBrowserAdapter } from './browserScan.js?v=20261123s';
import { onlineVaultId, downloadExportPair, previewExport } from './export.js?v=20261123s';
import { isSupported, loadHandle, pickOnlyReadDirectory, requestReadPermission, storeHandle } from './fsAccess.js?v=20261123s';
import { OfflineBuffer } from './offlineBuffer.js?v=20261123s';
import { ReviewActionService } from './reviewActions.js?v=20261123s';
import { scanVaultWithWorkers } from './scanWorker.js?v=20261123s';
import { WorkbenchUi } from './ui.js?v=20261123s';
import { transferApprovedNote } from './transfer.js?v=20261123s';

/** 仅在 HTTPS 或可信回环静态源中注册，不把注册成功描述为已安装。 */
export function registerAppShell(scope = globalThis) {
  if (!scope.isSecureContext || !scope.navigator?.serviceWorker) return Promise.resolve(null);
  return scope.navigator.serviceWorker.register('./sw.js', { scope: './', type: 'module' });
}

export function createWorkbench(documentRef, services = {}) {
  let workbench = null;
  const offlineBuffer = services.offlineBuffer ?? new OfflineBuffer({
    onError: (error) => workbench?.showStorageError(error),
  });
  workbench = new WorkbenchUi(documentRef, {
    pickDirectory: pickOnlyReadDirectory,
    createAdapter: createBrowserAdapter,
    scan: scanVaultWithWorkers,
    offlineBuffer,
    loadDirectoryHandle: loadHandle,
    saveDirectoryHandle: storeHandle,
    requestReadPermission,
    vaultId: onlineVaultId,
    createActionService: (options) => new ReviewActionService(options),
    previewExport,
    downloadExportPair,
    // 展开顺序很重要：默认实现必须放在 services 之后，否则会被 undefined 覆盖。
    ...services,
    transferToLibrary: services.transferToLibrary ?? transferApprovedNote,
  });
  if (services.offlineBuffer) services.offlineBuffer.onError = (error) => workbench.showStorageError(error);
  return workbench;
}

const root = document.querySelector('#app');
if (!(root instanceof HTMLElement)) throw new Error('应用入口结构不完整。');
const workbench = createWorkbench(document);
if (isSupported(globalThis)) workbench.mount(root);
else workbench.mount(root).renderUnsupported();
registerAppShell().catch((error) => {
  console.error('应用壳缓存注册失败', error);
  const notice = document.querySelector('#app-notice');
  if (notice) notice.textContent = '应用壳离线缓存注册失败；本机审核仍使用 IndexedDB，请保持页面可访问并定期导出。';
});
