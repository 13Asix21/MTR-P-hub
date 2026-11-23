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
    throw new DirectoryAccessError('DIRECTORY_NOT_AUTHORIZED', '请点击按钮后选择 Obsidian 库文件夹。');
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

/**
 * 只校验「是一个可遍历的目录」，不要求库里有某个固定子目录。
 * PARA、Zettelkasten、扁平、仅中文目录等 Obsidian 库结构都应被接受。
 */
export async function validateVaultRoot(handle) {
  if (!handle || handle.kind !== 'directory' || typeof handle.getDirectoryHandle !== 'function' || typeof handle.entries !== 'function') {
    throw new DirectoryAccessError('VAULT_ROOT_INVALID', '请选择 Obsidian 库文件夹（库的根目录）。');
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
