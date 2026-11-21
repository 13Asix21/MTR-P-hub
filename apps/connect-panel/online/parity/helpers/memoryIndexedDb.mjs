/** 仅供 Node 测试的 IndexedDB 接口模拟；生产代码不会导入它。 */
export class MemoryIndexedDb {
  constructor() { this.databases = new Map(); }

  open(name, version) {
    const request = {};
    queueMicrotask(() => {
      let database = this.databases.get(name);
      const isNew = !database;
      if (isNew) {
        database = { name, version, stores: new Map(), next: new Map(), closed: false };
        this.databases.set(name, database);
      }
      request.result = this.connection(database);
      if (isNew || database.version < version) request.onupgradeneeded?.();
      request.onsuccess?.();
    });
    return request;
  }

  connection(database) {
    return {
      objectStoreNames: { contains: (name) => database.stores.has(name) },
      createObjectStore: (name, options = {}) => {
        database.stores.set(name, new Map());
        database.next.set(name, 1);
        return { keyPath: options.keyPath };
      },
      close: () => { database.closed = true; },
      transaction: (names, mode = 'readonly') => {
        const transaction = { mode, objectStore: (name) => this.store(database, name, mode) };
        queueMicrotask(() => { if (!transaction.failed) transaction.oncomplete?.(); });
        return transaction;
      },
    };
  }

  store(database, name, mode) {
    if (!database.stores.has(name)) throw new Error(`missing object store ${name}`);
    const rows = database.stores.get(name);
    const requestOf = (operation) => {
      const request = {};
      queueMicrotask(() => {
        try { request.result = operation(); request.onsuccess?.(); }
        catch (error) { request.error = error; request.onerror?.(); }
      });
      return request;
    };
    return {
      get: (key) => requestOf(() => structuredClone(rows.get(key))),
      getAll: () => requestOf(() => [...rows.values()].map((value) => structuredClone(value))),
      add: (value) => requestOf(() => {
        if (mode !== 'readwrite') throw new Error('read-only');
        const row = structuredClone(value);
        const key = row.seq ?? row.key ?? database.next.get(name);
        if (rows.has(key)) throw new Error('duplicate key');
        if (name === 'pending_actions' || name === 'export_backup') {
          row.seq = key;
          database.next.set(name, Math.max(database.next.get(name), key + 1));
        }
        rows.set(key, row);
        return key;
      }),
      put: (value) => requestOf(() => {
        if (mode !== 'readwrite') throw new Error('read-only');
        const row = structuredClone(value);
        const key = row.seq ?? row.key;
        if ('seq' in row && !rows.has(key)) database.next.set(name, key + 1);
        rows.set(key, row);
        return key;
      }),
      delete: (key) => requestOf(() => { if (mode !== 'readwrite') throw new Error('read-only'); rows.delete(key); }),
      clear: () => requestOf(() => { if (mode !== 'readwrite') throw new Error('read-only'); rows.clear(); }),
    };
  }

  snapshot(name) {
    const database = this.databases.get(name);
    return Object.fromEntries([...database.stores].map(([store, rows]) => [store, [...rows.values()].map((row) => structuredClone(row))]));
  }
}
