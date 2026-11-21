export const requiresUserActivation = true;

export class DirectoryAccessError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'DirectoryAccessError';
    this.code = code;
  }
}

export function isSupported(scope = globalThis) {
  return typeof scope.showDirectoryPicker === 'function' && Boolean(scope.crypto?.subtle);
}

/** 全站唯一目录选择调用点；调用方必须是用户 click 处理函数。 */
export async function pickOnlyReadDirectory() {
  if (!isSupported(globalThis)) {
    throw new DirectoryAccessError('UNSUPPORTED_BROWSER', '本工具依赖 Chromium 的 File System Access API，请使用 Chrome 或 Edge 108 及以上版本打开。');
  }
  if (globalThis.navigator.userActivation && !globalThis.navigator.userActivation.isActive) {
    throw new DirectoryAccessError('DIRECTORY_NOT_AUTHORIZED', '请点击按钮后选择 Vault 文件夹。');
  }
  let handle;
  try {
    handle = await globalThis.showDirectoryPicker({ mode: 'read' });
  } catch (error) {
    if (error?.name === 'AbortError') return null;
    throw new DirectoryAccessError('DIRECTORY_NOT_AUTHORIZED', '未获得目录读取权限，请重新选择。');
  }
  await validateVaultRoot(handle);
  return handle;
}

export async function validateVaultRoot(handle) {
  if (!handle || handle.kind !== 'directory' || typeof handle.getDirectoryHandle !== 'function') {
    throw new DirectoryAccessError('VAULT_ROOT_INVALID', '请选择 Vault 根目录。');
  }
  try {
    await handle.getDirectoryHandle('2-Permanent', { create: false });
  } catch {
    throw new DirectoryAccessError('VAULT_ROOT_INVALID', '未找到 2-Permanent，请重新选择 Vault 根目录。');
  }
  return handle;
}

export async function queryReadPermission(handle) {
  if (!handle || typeof handle.queryPermission !== 'function') return 'prompt';
  return handle.queryPermission({ mode: 'read' });
}

/** 只能由「继续使用上次目录」用户手势调用。 */
export async function requestReadPermission(handle) {
  if (!handle || typeof handle.requestPermission !== 'function') return 'denied';
  return handle.requestPermission({ mode: 'read' });
}

export async function storeHandle(store, handle) {
  await validateVaultRoot(handle);
  await store.saveDirectoryHandle(handle);
  return handle;
}

export async function loadHandle(store) {
  try {
    const handle = await store.loadDirectoryHandle();
    if (handle) await validateVaultRoot(handle);
    return handle ?? null;
  } catch (error) {
    if (['NotFoundError', 'NotAllowedError', 'InvalidStateError'].includes(error?.name) || error?.code === 'VAULT_ROOT_INVALID') {
      await store.clearDirectoryHandle();
      return null;
    }
    throw error;
  }
}
