import { compareCodePoints } from './parser.js?v=20261124h';
import { classifyByName, collectListing, compactScannedFact, DEFAULT_EXCLUDED_ROOTS, DEFAULT_MANAGED_ROOTS, fileExtension, isExcluded, MAX_FILE_BYTES, MAX_SCAN_FILES, normalizeRoots, casefold } from './scanner.js?v=20261124h';

// 本文件只在浏览器 Worker 或 Node 测试中执行；fileHandle 始终是只读句柄。
const workerScope = typeof self === 'undefined' ? null : self;
if (workerScope && typeof window === 'undefined') workerScope.onmessage = async (event) => {
  const { id, fileHandle, relative, mtime, size } = event.data;
  try {
    const bytes = new Uint8Array(await (await fileHandle.getFile()).arrayBuffer());
    const { scanFileBytes } = await import('./scanner.js?v=20261124h');
    const fact = await scanFileBytes(relative, bytes, { size_bytes: size, mtime, maxFileBytes: MAX_FILE_BYTES });
    fact.raw_bytes = [];
    workerScope.postMessage({ id, fact });
  } catch {
    workerScope.postMessage({ id, fact: { relative_posix: relative, size_bytes: size, mtime, content_sha256: '', tags: [], classification: 'unknown', parse_status: 'FILE_READ_FAILED', error_code: 'FILE_READ_FAILED', error_message: '文件读取失败。', raw_bytes: [] } });
  }
};

export function workerConcurrency(scope = globalThis) {
  return Math.min(8, Math.max(2, scope.navigator?.hardwareConcurrency ?? 4));
}

async function fileMetadata(handle) {
  const file = await handle.getFile();
  const mtime = `${new Date(file.lastModified).toISOString().slice(0, -1)}+00:00`;
  return { size: file.size, mtime };
}

async function collectEntries(root, options, onProgress) {
  const managed = normalizeRoots(options.managedRoots ?? DEFAULT_MANAGED_ROOTS, 'managed_roots');
  const excluded = normalizeRoots(options.excludedRoots ?? DEFAULT_EXCLUDED_ROOTS, 'excluded_roots');
  const skips = [];
  const entries = [];
  const pending = [];
  const maximumBytes = options.maxFileBytes ?? MAX_FILE_BYTES;
  const limit = options.maxScanFiles ?? MAX_SCAN_FILES;
  let scannedFiles = 0;
  for (const parts of [...managed].reverse()) {
    try {
      let directory = root;
      for (const part of parts) directory = await directory.getDirectoryHandle(part, { create: false });
      pending.push({ parts, directory });
    } catch { skips.push({ kind: 'managed_root_not_found', relative_posix: parts.join('/') }); }
  }
  while (pending.length) {
    if (scannedFiles >= limit) { skips.push({ kind: 'limit_reached' }); break; }
    const { parts, directory } = pending.pop();
    if (options.signal?.aborted) throw new DOMException('扫描已中断。', 'AbortError');
    const children = [];
    try {
      for await (const [name, handle] of directory.entries()) children.push({ name, handle });
    } catch { skips.push({ kind: 'directory_read_failed', relative_posix: parts.join('/') }); continue; }
    children.sort((left, right) => compareCodePoints(casefold(left.name), casefold(right.name)));
    for (const { name, handle } of children) {
      const next = [...parts, name];
      if (isExcluded(next, excluded)) {
        if (handle.kind === 'directory') skips.push({ kind: 'excluded_directory' });
        else if (name.toLowerCase().endsWith('.md')) skips.push({ kind: 'excluded_file' });
        continue;
      }
      if (handle.kind === 'directory') { pending.push({ parts: next, directory: handle }); continue; }
      const category = classifyByName(name);
      if (handle.kind !== 'file' || category === 'ignore') continue;
      if (scannedFiles >= limit) { skips.push({ kind: 'limit_reached' }); pending.length = 0; break; }
      scannedFiles += 1;
      try {
        const metadata = await fileMetadata(handle);
        if (category === 'preview-node') {
          // 仅预览格式：不送 Worker 解析，只登记元信息。
          entries.push({ previewOnly: true, relative: next.join('/'), previewKind: fileExtension(name), ...metadata });
          continue;
        }
        entries.push({ fileHandle: handle, relative: next.join('/'), ...metadata });
        if (metadata.size > maximumBytes) skips.push({ kind: 'file_too_large_metadata' });
      } catch { entries.push({ relative: next.join('/'), size: 0, mtime: '', readFailed: true }); }
      onProgress?.({ scanned: scannedFiles, currentDirectory: parts.join('/') });
    }
  }
  return { entries, skips, managed, excluded };
}

/** Node 测试可注入 workerFactory；生产使用同源 Worker，主线程不持有正文。 */
export async function scanVaultWithWorkers(root, options = {}) {
  const { entries, skips, managed, excluded } = await collectEntries(root, options, options.onProgress);
  const facts = new Array(entries.length);
  const count = Math.max(1, Math.min(options.concurrency ?? workerConcurrency(), entries.length || 1));
  const factory = options.workerFactory ?? (() => new Worker(new URL('./scanWorker.js', import.meta.url), { type: 'module' }));
  let next = 0;
  async function consume() {
    const worker = factory();
    try {
      while (next < entries.length) {
        if (options.signal?.aborted) throw new DOMException('扫描已中断。', 'AbortError');
        const index = next++;
        const entry = entries[index];
        if (entry.previewOnly) {
          facts[index] = {
            relative_posix: entry.relative,
            size_bytes: entry.size,
            mtime: entry.mtime,
            content_sha256: '',
            tags: [],
            classification: 'preview_only',
            parse_status: 'PREVIEW_ONLY',
            error_code: '',
            error_message: '',
            preview_kind: entry.previewKind,
            raw_bytes: new Uint8Array(),
          };
          continue;
        }
        if (entry.readFailed) {
          facts[index] = { relative_posix: entry.relative, size_bytes: 0, mtime: '', content_sha256: '', tags: [], classification: 'unknown', parse_status: 'FILE_READ_FAILED', error_code: 'FILE_READ_FAILED', error_message: '文件读取失败。', raw_bytes: new Uint8Array() };
          continue;
        }
        if (entry.size > (options.maxFileBytes ?? MAX_FILE_BYTES)) {
          facts[index] = { relative_posix: entry.relative, size_bytes: entry.size, mtime: entry.mtime, content_sha256: '', tags: [], classification: 'unknown', parse_status: 'FILE_TOO_LARGE', error_code: 'FILE_TOO_LARGE', error_message: '文件超过单文件读取上限，已跳过。', raw_bytes: new Uint8Array() };
          continue;
        }
        try {
          facts[index] = await new Promise((resolve, reject) => {
            const timeout = setTimeout(() => reject(new Error('WORKER_TIMEOUT')), options.workerTimeoutMs ?? 30000);
            worker.onmessage = ({ data }) => {
              clearTimeout(timeout);
              const fact = compactScannedFact(data.fact);
              fact.raw_bytes = new Uint8Array();
              resolve(fact);
            };
            worker.onerror = () => { clearTimeout(timeout); reject(new Error('WORKER_FAILED')); };
            worker.postMessage({ id: index, ...entry });
          });
        } catch (error) {
          if (error?.name === 'AbortError' || options.signal?.aborted) throw new DOMException('扫描已中断。', 'AbortError');
          facts[index] = { relative_posix: entry.relative, size_bytes: entry.size, mtime: entry.mtime, content_sha256: '', tags: [], classification: 'unknown', parse_status: 'FILE_READ_FAILED', error_code: 'FILE_READ_FAILED', error_message: '文件读取失败。', raw_bytes: new Uint8Array() };
        }
      }
    } finally { worker.terminate(); }
  }
  await Promise.all(Array.from({ length: count }, consume));
  // 与 walkCollect 一致：库根必须回写成 '.'，否则会被归一成非法空路径。
  const managedRoots = managed.map((parts) => (parts.length === 0 ? '.' : parts.join('/')));
  return collectListing(facts, skips.filter((event) => event.kind !== 'file_too_large_metadata'), {
    managedRoots,
    excludedRoots: excluded.map((parts) => parts.join('/')),
    path: root.name,
    scannedAt: options.scannedAt,
  });
}
