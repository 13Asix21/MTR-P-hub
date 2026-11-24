import { decide, emptyState, reset, reviewPath, validateState } from './reviewDomain.js?v=20261124c';

export const OFFLINE_DATABASE_NAME = 'mtrp-online-workbench';
export const OFFLINE_DATABASE_VERSION = 1;
export const OFFLINE_MAX_ACTIONS = 5000;
export const OFFLINE_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
export const OFFLINE_SYNC_UNAVAILABLE = '仅本机离线暂存，云同步未启用';

export class OfflineBufferError extends Error {
  constructor(code, message, options = {}) {
    super(message, options);
    this.name = 'OfflineBufferError';
    this.code = code;
  }
}

export function offlineStorageError(error) {
  if (error instanceof OfflineBufferError) return error;
  return new OfflineBufferError('OFFLINE_STORAGE_FAILED', '本机 IndexedDB 不可用或写入失败，可能受隐私模式、浏览器策略或存储空间限制。', { cause: error });
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(offlineStorageError(request.error));
  });
}

function transactionDone(transaction, reportError = offlineStorageError) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(reportError(transaction.error));
    transaction.onabort = () => reject(reportError(transaction.error));
  });
}

function requiredIdentity(value, field) {
  if (typeof value !== 'string' || !value || value.length > 256) {
    throw new OfflineBufferError('OFFLINE_IDENTITY_INVALID', `${field} 必须是非空标识。`);
  }
  return value;
}

function cloneAction(action) {
  return structuredClone(action);
}

/**
 * 生产离线缓冲只使用 IndexedDB。测试可以注入满足同一接口的 indexedDB，
 * 但本模块不含 localStorage 或数组型生产存储替代实现。
 */
export class OfflineBuffer {
  constructor(options = {}) {
    this.indexedDB = options.indexedDB ?? globalThis.indexedDB;
    this.databaseName = options.databaseName ?? OFFLINE_DATABASE_NAME;
    this.databasePromise = null;
    this.drainPromise = null;
    this.onError = options.onError ?? null;
  }

  reportError(error) {
    const normalized = offlineStorageError(error);
    try { this.onError?.(normalized); } catch { /* 错误通知不能改写事务失败结果。 */ }
    return normalized;
  }

  async open() {
    if (this.databasePromise) return this.databasePromise;
    if (!this.indexedDB || typeof this.indexedDB.open !== 'function') {
      throw new OfflineBufferError('OFFLINE_UNAVAILABLE', '当前浏览器无法打开本机离线缓冲。');
    }
    this.databasePromise = new Promise((resolve, reject) => {
      const request = this.indexedDB.open(this.databaseName, OFFLINE_DATABASE_VERSION);
      request.onupgradeneeded = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains('dir_handle')) {
          database.createObjectStore('dir_handle', { keyPath: 'key' });
        }
        if (!database.objectStoreNames.contains('pending_actions')) {
          database.createObjectStore('pending_actions', { keyPath: 'seq', autoIncrement: true });
        }
        if (!database.objectStoreNames.contains('export_backup')) {
          database.createObjectStore('export_backup', { keyPath: 'seq', autoIncrement: true });
        }
      };
      request.onsuccess = () => {
        const database = request.result;
        database.onversionchange = () => database.close();
        resolve(database);
      };
    request.onerror = () => reject(this.reportError(request.error));
    request.onblocked = () => reject(this.reportError(new OfflineBufferError('OFFLINE_BLOCKED', '本机离线缓冲被其他页面占用，请关闭旧页面后重试。')));
    });
    try {
      return await this.databasePromise;
    } catch (error) {
      this.databasePromise = null;
      throw error;
    }
  }

  async store(name, mode = 'readonly') {
    const database = await this.open();
    const transaction = database.transaction([name], mode);
    return { objectStore: transaction.objectStore(name), done: transactionDone(transaction, (error) => this.reportError(error)) };
  }

  async enqueueAction(action) {
    const allowed = ['DECIDE', 'UNDO', 'STALE_RESET', 'SET_POSITION'];
    if (!action || !allowed.includes(action.op)) {
      throw new OfflineBufferError('OFFLINE_ACTION_INVALID', '离线动作类型无效。');
    }
    const row = {
      ...cloneAction(action),
      user_id: requiredIdentity(action.user_id, 'user_id'),
      vault_id: requiredIdentity(action.vault_id, 'vault_id'),
      path: action.op === 'SET_POSITION' && action.path === null ? null : reviewPath(action.path),
      retry_count: 0,
      sync_state: 'pending',
    };
    delete row.seq;
    const { objectStore, done } = await this.store('pending_actions', 'readwrite');
    const request = objectStore.add(row);
    const [seq] = await Promise.all([requestResult(request), done]);
    return { ...row, seq };
  }

  async allActions() {
    const { objectStore, done } = await this.store('pending_actions');
    const [rows] = await Promise.all([requestResult(objectStore.getAll()), done]);
    return rows.sort((left, right) => left.seq - right.seq).map(cloneAction);
  }

  async getStatus(userId, now = new Date()) {
    const rows = await this.allActions();
    const own = userId === undefined ? rows : rows.filter((row) => row.user_id === userId);
    const oldest = own.reduce((value, row) => Math.min(value, Date.parse(row.created_at)), Number.POSITIVE_INFINITY);
    const timestamp = now instanceof Date ? now.getTime() : new Date(now).getTime();
    const expired = Number.isFinite(oldest) && timestamp - oldest >= OFFLINE_MAX_AGE_MS;
    return {
      total: own.length,
      pending: own.filter((row) => row.sync_state !== 'done').length,
      foreign_accounts: userId === undefined ? 0 : rows.length - own.length,
      limit_reached: own.length >= OFFLINE_MAX_ACTIONS || expired,
      sync_state: 'pending',
      message: OFFLINE_SYNC_UNAVAILABLE,
    };
  }

  async markDone(seq) {
    const { objectStore, done } = await this.store('pending_actions', 'readwrite');
    const row = await requestResult(objectStore.get(seq));
    if (row && row.sync_state !== 'done') {
      row.sync_state = 'done';
      await requestResult(objectStore.put(row));
    }
    await done;
    return row ? cloneAction(row) : null;
  }

  /**
   * sink 是显式 async (action) => void 接口，不包含旧 SELECT-then-upsert/LWW。
   * 未提供 sink 时绝不尝试网络，也绝不把 pending 改写成已同步。
   */
  async drainQueue({ userId, vaultId, sink }) {
    requiredIdentity(userId, 'user_id');
    if (typeof sink !== 'function') {
      const status = await this.getStatus(userId);
      return { attempted: 0, completed: 0, failed: 0, ...status };
    }
    if (this.drainPromise) return this.drainPromise;
    this.drainPromise = (async () => {
      const rows = await this.allActions();
      const eligible = rows.filter((row) => row.user_id === userId
        && (vaultId === undefined || row.vault_id === vaultId) && row.sync_state !== 'done');
      let completed = 0;
      let failed = 0;
      for (const row of eligible) {
        // 每次重读，避免并发 sink 已完成时重复发送。
        const { objectStore, done } = await this.store('pending_actions', 'readwrite');
        const current = await requestResult(objectStore.get(row.seq));
        if (!current || current.sync_state === 'done') {
          await done;
          continue;
        }
        current.sync_state = 'sending';
        await requestResult(objectStore.put(current));
        await done;
        try {
          await sink(cloneAction(current));
          await this.markDone(current.seq);
          completed += 1;
        } catch {
          const failedStore = await this.store('pending_actions', 'readwrite');
          const latest = await requestResult(failedStore.objectStore.get(current.seq));
          if (latest && latest.sync_state !== 'done') {
            latest.sync_state = 'pending';
            latest.retry_count = Number(latest.retry_count ?? 0) + 1;
            await requestResult(failedStore.objectStore.put(latest));
          }
          await failedStore.done;
          failed += 1;
        }
      }
      return {
        attempted: eligible.length,
        completed,
        failed,
        ...(await this.getStatus(userId)),
      };
    })();
    try {
      return await this.drainPromise;
    } finally {
      this.drainPromise = null;
    }
  }

  /** 仅回放当前账号、当前 Vault 的未同步事件；旧账号永远不参与。 */
  async replayPending({ userId, vaultId, baseState = emptyState() }) {
    let state = validateState(baseState);
    const rows = await this.allActions();
    for (const row of rows) {
      if (row.user_id !== userId || row.vault_id !== vaultId || row.sync_state === 'done') continue;
      if (row.op === 'DECIDE') {
        state = decide(state, row.path, row.to ?? row.action, row.reason ?? '', row.content_sha256, row.at);
      } else if (row.op === 'UNDO' || row.op === 'STALE_RESET') {
        if (!state.items[row.path]) continue;
        state = reset(state, row.path, row.op === 'UNDO' ? 'undo' : 'stale_reset', row.at);
      } else if (row.op === 'SET_POSITION') {
        state = { ...state, last_position: row.path === null ? null : reviewPath(row.path) };
      }
    }
    return state;
  }

  /** 上限时保存可下载 JSON 快照；不删除任何 pending 动作。 */
  async exportBackup({ userId, exportedAt = new Date().toISOString() } = {}) {
    const pendingActions = await this.allActions();
    const scoped = userId === undefined ? pendingActions : pendingActions.filter((row) => row.user_id === userId);
    const document = {
      schema_version: '1',
      exported_at: exportedAt,
      kind: 'mtrp-online-offline-backup',
      pending_actions: scoped,
    };
    const { objectStore, done } = await this.store('export_backup', 'readwrite');
    const request = objectStore.add({ created_at: exportedAt, document });
    const [seq] = await Promise.all([requestResult(request), done]);
    return { seq, document };
  }

  async saveDirectoryHandle(handle) {
    const { objectStore, done } = await this.store('dir_handle', 'readwrite');
    await requestResult(objectStore.put({ key: 'primary', handle }));
    await done;
  }

  async loadDirectoryHandle() {
    const { objectStore, done } = await this.store('dir_handle');
    const [row] = await Promise.all([requestResult(objectStore.get('primary')), done]);
    return row?.handle ?? null;
  }

  async clearDirectoryHandle() {
    const { objectStore, done } = await this.store('dir_handle', 'readwrite');
    await requestResult(objectStore.delete('primary'));
    await done;
  }

  async close() {
    if (!this.databasePromise) return;
    const database = await this.databasePromise;
    database.close();
    this.databasePromise = null;
  }

  /**
   * 用户确认后仅删除本应用 IndexedDB 中的台账、备份和目录句柄。
   * Service Worker 的 app-shell 缓存不受影响；绝不调用 Vault 的删除或写接口。
   */
  async clearLocalData() {
    const database = await this.open();
    const names = ['dir_handle', 'pending_actions', 'export_backup'];
    const transaction = database.transaction(names, 'readwrite');
    for (const name of names) transaction.objectStore(name).clear();
    await transactionDone(transaction, (error) => this.reportError(error));
  }
}
