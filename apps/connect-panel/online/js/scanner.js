import { sha256Bytes } from './hasher.js?v=20261123q';
import {
  MarkdownParseError,
  compareCodePoints,
  decodeUtf8Sig,
  extractWikiLinks,
  parseDocumentSync,
  splitFrontmatter,
  splitLines,
} from './parser.js?v=20261123q';

/**
 * 默认扫描整个 Obsidian 库根目录（`.`）。各类 Para / Zettelkasten / 扁平结构都能兼容，
 * 不再假定 `2-Permanent` 这一类单一目录名。
 */
export const DEFAULT_MANAGED_ROOTS = Object.freeze(['.']);
export const DEFAULT_EXCLUDED_ROOTS = Object.freeze([
  '.obsidian',
  '.trash',
  '.git',
  '.github',
  '.obsidian-git',
  '2-Permanent/Resources/MTR-P-System',
  '2-Permanent/Areas/AI-Assets/_templates',
]);
export const MAX_FILE_BYTES = 1_048_576;
export const MAX_SCAN_FILES = 5000;

export class ScanError extends Error {
  constructor(code, message, httpStatus = 400) {
    super(message);
    this.name = 'ScanError';
    this.code = code;
    this.httpStatus = httpStatus;
  }
}

/** Python str.casefold 的完整 Unicode 兼容实现；普通字符走 JS lower-case。 */
export { pythonCasefold as casefold } from './pythonCasefold.js?v=20261123q';
import { pythonCasefold as casefold } from './pythonCasefold.js?v=20261123q';

export function normalizeRoot(rawRoot, field) {
  if (typeof rawRoot !== 'string' || !rawRoot.trim()) {
    throw new ScanError('ROOT_INVALID', `${field} 中存在空路径。`);
  }
  const value = rawRoot.trim().replaceAll('\\', '/').replace(/\/+$/u, '');
  // '.' 表示库根目录本身，是唯一合法的「整库」写法；其他位置的点仍按穿越拒绝。
  if (value === '.') return [];
  if (/^[A-Za-z][A-Za-z0-9+.-]*:\/\//u.test(value) || value.startsWith('/')) {
    throw new ScanError('ROOT_MUST_BE_RELATIVE', `${field} 必须是 Vault 内的相对路径。`);
  }
  if (value.includes(':')) {
    throw new ScanError('ROOT_DRIVE_RELATIVE_REJECTED', `${field} 不能包含盘符、冒号或 ADS 路径。`);
  }
  const parts = value.split('/');
  if (parts.length === 0 || parts.some((part) => part === '' || part === '.' || part === '..')) {
    throw new ScanError('ROOT_TRAVERSAL_REJECTED', `${field} 不能包含父目录跳转。`);
  }
  return parts;
}

export function normalizeRoots(rawRoots, field) {
  if (!Array.isArray(rawRoots)) {
    throw new ScanError('ROOT_INVALID', `${field} 必须是路径数组。`);
  }
  if (rawRoots.length > 64) {
    throw new ScanError('TOO_MANY_ROOTS', `${field} 最多允许 64 项。`);
  }
  const roots = rawRoots.map((root) => normalizeRoot(root, field));
  if (field === 'managed_roots' && roots.length === 0) {
    throw new ScanError('MANAGED_ROOT_REQUIRED', '至少需要一个 managed root。');
  }
  return roots;
}

export function isExcluded(parts, excludedParts) {
  const key = parts.map(casefold);
  return excludedParts.some((root) => {
    const rootKey = root.map(casefold);
    return rootKey.length <= key.length && rootKey.every((part, index) => part === key[index]);
  });
}

export function newStatistics() {
  return {
    scanned_files: 0,
    parsed_files: 0,
    trial_notes: 0,
    rarely_notes: 0,
    unmanaged_notes: 0,
    conflict_notes: 0,
    skipped_files: 0,
    skipped_directories: 0,
    excluded_directories: 0,
    error_count: 0,
    limit_reached: false,
  };
}

export function collectFileStatistics(statistics, scanned) {
  statistics.scanned_files += 1;
  if (scanned.parse_status === 'FILE_TOO_LARGE') {
    statistics.skipped_files += 1;
  } else if (scanned.parse_status !== 'ok') {
    statistics.error_count += 1;
  } else {
    statistics.parsed_files += 1;
    statistics[`${scanned.classification}_notes`] += 1;
  }
}

export function collectSkipStatistics(statistics, event) {
  const kind = event.kind;
  if (kind === 'managed_root_link' || kind === 'link_directory') {
    statistics.skipped_directories += 1;
  } else if (['managed_root_not_found', 'directory_read_failed', 'excluded_stat_failed', 'file_stat_failed'].includes(kind)) {
    statistics.error_count += 1;
  } else if (kind === 'limit_reached') {
    statistics.limit_reached = true;
  } else if (kind === 'excluded_directory') {
    statistics.excluded_directories += 1;
  } else if (kind === 'excluded_file' || kind === 'link_file') {
    statistics.skipped_files += 1;
  }
}

export function emptyCounts() {
  return { parsed_ok: 0, invalid: 0, file_too_large: 0, mtr: { trial: 0, rarely: 0, unmanaged: 0, conflict: 0 } };
}

function itemFromScanned(scanned) {
  const item = {
    vault_relative_path: scanned.relative_posix.normalize('NFC'),
    sha256: scanned.content_sha256 ?? '',
    size_bytes: scanned.size_bytes,
    mtime: scanned.mtime,
    tags: [...(scanned.tags ?? [])],
    mtr_state: scanned.parse_status === 'ok' ? scanned.classification : 'unknown',
    parse_status: scanned.parse_status,
    links_out: 0,
    links_in: 0,
  };
  let targets = scanned._link_targets;
  if (!Array.isArray(targets)
      && scanned.raw_bytes?.length
      && scanned.parse_status !== 'INVALID_ENCODING') {
    let text = '';
    try {
      text = decodeUtf8Sig(scanned.raw_bytes);
    } catch {
      text = '';
    }
    if (text) targets = extractWikiLinks(splitLines(text));
  }
  if (Array.isArray(targets)) {
    item.links_out = targets.length;
    item._link_targets = targets;
  }
  return item;
}

/** Worker 回传前释放正文，只保留清单链接解析所需的小字段。 */
export function compactScannedFact(fact) {
  let targets = [];
  if (fact.raw_bytes?.length && fact.parse_status !== 'INVALID_ENCODING') {
    try {
      targets = extractWikiLinks(splitLines(decodeUtf8Sig(fact.raw_bytes)));
    } catch {
      targets = [];
    }
  }
  return { ...fact, raw_bytes: new Uint8Array(), _link_targets: targets };
}

function linkVariants(path) {
  const normalized = casefold(path.normalize('NFC'));
  return normalized.endsWith('.md') ? [normalized, normalized.slice(0, -3)] : [normalized, `${normalized}.md`];
}

function basename(path) {
  return path.split('/').at(-1) ?? path;
}

export function resolveLinks(items) {
  const fullIndex = new Map();
  const basenameCandidates = new Map();
  for (const item of items) {
    for (const variant of linkVariants(item.vault_relative_path)) {
      fullIndex.set(variant, fullIndex.has(variant) ? null : item);
    }
    for (const variant of linkVariants(basename(item.vault_relative_path))) {
      const candidates = basenameCandidates.get(variant) ?? [];
      candidates.push(item);
      basenameCandidates.set(variant, candidates);
    }
  }
  for (const item of items) {
    for (const rawTarget of item._link_targets ?? []) {
      const target = rawTarget.split('#', 1)[0].trim();
      if (!target) {
        continue;
      }
      let resolved = null;
      for (const variant of linkVariants(target)) {
        const candidate = fullIndex.get(variant);
        if (candidate !== null && candidate !== undefined) {
          resolved = candidate;
          break;
        }
      }
      if (resolved === null) {
        for (const variant of linkVariants(basename(target))) {
          const candidates = [...new Set(basenameCandidates.get(variant) ?? [])];
          if (candidates.length === 1) {
            resolved = candidates[0];
            break;
          }
        }
      }
      if (resolved !== null) {
        resolved.links_in += 1;
      }
    }
    delete item._link_targets;
  }
}

/** 单遍事实收集：statistics 与 items 由同一份 scanned fact 同时派生。 */
export function collectListing(facts, skipEvents = [], options = {}) {
  // 与 walkCollect 共用同一归一语义：'.' 回写成 '.'，避免整库模式漏成空串。
  const managedRoots = normalizeRoots(options.managedRoots ?? DEFAULT_MANAGED_ROOTS, 'managed_roots')
    .map((parts) => (parts.length === 0 ? '.' : parts.join('/')));
  const excludedRoots = normalizeRoots(options.excludedRoots ?? DEFAULT_EXCLUDED_ROOTS, 'excluded_roots')
    .map((parts) => parts.join('/'));
  const statistics = newStatistics();
  const items = [];
  const sourcePaths = {};
  for (const fact of facts) {
    collectFileStatistics(statistics, fact);
    const item = itemFromScanned(fact);
    items.push(item);
    sourcePaths[item.vault_relative_path] = fact.relative_posix;
  }
  for (const event of skipEvents) {
    collectSkipStatistics(statistics, event);
  }
  resolveLinks(items);
  items.sort((left, right) => compareCodePoints(casefold(left.vault_relative_path), casefold(right.vault_relative_path)));
  const counts = emptyCounts();
  for (const item of items) {
    if (item.parse_status === 'ok') {
      counts.parsed_ok += 1;
      counts.mtr[item.mtr_state] += 1;
    } else if (item.parse_status === 'FILE_TOO_LARGE') {
      counts.file_too_large += 1;
    } else {
      counts.invalid += 1;
    }
  }
  const hasErrors = statistics.error_count > 0;
  const status = statistics.parsed_files === 0 && hasErrors && statistics.scanned_files === 0
    ? 'error'
    : statistics.limit_reached || hasErrors || counts.file_too_large > 0 ? 'limited' : 'ok';
  const normalizedPaths = items.map((item) => item.vault_relative_path);
  return {
    scanned_at: options.scannedAt ?? new Date().toISOString(),
    path: options.path ?? '',
    managed_roots: [...managedRoots],
    excluded_roots: [...excludedRoots],
    status,
    limit_reached: statistics.limit_reached,
    statistics,
    total: items.length,
    counts,
    items,
    warnings: new Set(normalizedPaths).size === normalizedPaths.length ? [] : ['NFC 归一后存在同键路径；清单保留全部条目，请人工核对。'],
    _source_paths: sourcePaths,
  };
}

/** 构造 ScannedFile 同构事实；文件读取和 mtime 由注入的 walk 负责。 */
export async function scanFileBytes(relativePath, rawBytes, metadata = {}) {
  const base = {
    relative_posix: relativePath,
    size_bytes: metadata.size_bytes ?? rawBytes.byteLength,
    mtime: metadata.mtime ?? '',
    content_sha256: '',
    tags: [],
    classification: 'unknown',
    parse_status: 'ok',
    error_code: null,
    error_message: null,
    raw_bytes: new Uint8Array(),
  };
  if (base.size_bytes > (metadata.maxFileBytes ?? MAX_FILE_BYTES)) {
    return { ...base, raw_bytes: new Uint8Array(), parse_status: 'FILE_TOO_LARGE', error_code: 'FILE_TOO_LARGE', error_message: '文件超过单文件读取上限，已跳过。' };
  }
  const contentSha256 = await sha256Bytes(rawBytes);
  try {
    const document = parseDocumentSync(rawBytes, relativePath, contentSha256);
    return { ...base, content_sha256: contentSha256, tags: document.tags, classification: document.classification, raw_bytes: rawBytes };
  } catch (error) {
    if (error instanceof MarkdownParseError) {
      return { ...base, content_sha256: contentSha256, parse_status: error.errorCode, error_code: error.errorCode, error_message: 'Markdown 编码或 frontmatter 无法解析。', raw_bytes: rawBytes };
    }
    return { ...base, content_sha256: contentSha256, parse_status: 'MARKDOWN_PARSE_FAILED', error_code: 'MARKDOWN_PARSE_FAILED', error_message: '文件解析失败，已跳过。', raw_bytes: rawBytes };
  }
}

/**
 * 注入式 walk 契约：listDirectory(parts) 返回 {name,kind,size,mtime}[]；
 * readBytes(parts) 返回 Uint8Array。engine 不 import 磁盘或 DOM API。
 */
export async function walkCollect(adapter, options = {}) {
  const managed = normalizeRoots(options.managedRoots ?? DEFAULT_MANAGED_ROOTS, 'managed_roots');
  const excluded = normalizeRoots(options.excludedRoots ?? DEFAULT_EXCLUDED_ROOTS, 'excluded_roots');
  // 库根用 '.' 回写，与 Python 参考引擎的 managed_roots 表示保持一致。
  const managedRoots = managed.map((parts) => (parts.length === 0 ? '.' : parts.join('/')));
  const excludedRoots = excluded.map((parts) => parts.join('/'));
  const maxScanFiles = options.maxScanFiles ?? MAX_SCAN_FILES;
  const facts = [];
  const skips = [];
  const pending = [];
  for (const parts of [...managed].reverse()) {
    const directory = await adapter.statDirectory?.(parts);
    if (directory?.kind === 'link') {
      skips.push({ kind: 'managed_root_link', relative_posix: parts.join('/'), code: 'LINK_SKIPPED', message: 'managed root 是链接或 junction，已跳过。' });
    } else if (directory?.kind !== 'directory') {
      skips.push({ kind: 'managed_root_not_found', relative_posix: parts.join('/'), code: 'MANAGED_ROOT_NOT_FOUND', message: 'managed root 不存在或不是目录。' });
    } else {
      pending.push(parts);
    }
  }
  let scannedFiles = 0;
  while (pending.length > 0) {
    if (scannedFiles >= maxScanFiles) {
      skips.push({ kind: 'limit_reached', relative_posix: '', code: '', message: '' });
      break;
    }
    const prefix = pending.pop();
    let entries;
    try {
      entries = await adapter.listDirectory(prefix);
    } catch {
      skips.push({ kind: 'directory_read_failed', relative_posix: prefix.join('/'), code: 'DIRECTORY_READ_FAILED', message: '目录读取失败。' });
      continue;
    }
    entries = [...entries].sort((left, right) => compareCodePoints(casefold(left.name), casefold(right.name)));
    for (const entry of entries) {
      const parts = [...prefix, entry.name];
      const relative = parts.join('/');
      if (isExcluded(parts, excluded)) {
        if (entry.kind === 'directory') {
          skips.push({ kind: 'excluded_directory', relative_posix: '', code: '', message: '' });
        } else if (entry.name.toLowerCase().endsWith('.md')) {
          skips.push({ kind: 'excluded_file', relative_posix: '', code: '', message: '' });
        }
        continue;
      }
      if (entry.kind === 'link' || entry.kind === 'link-directory') {
        skips.push({ kind: entry.kind === 'link-directory' ? 'link_directory' : 'link_file', relative_posix: '', code: '', message: '' });
        skips.push({ kind: 'link_issue', relative_posix: relative, code: 'LINK_SKIPPED', message: '链接或 junction 已跳过，未跟随目标。' });
        continue;
      }
      if (entry.kind === 'directory') {
        pending.push(parts);
        continue;
      }
      if (entry.kind !== 'file' || !entry.name.toLowerCase().endsWith('.md')) {
        continue;
      }
      if (scannedFiles >= maxScanFiles) {
        skips.push({ kind: 'limit_reached', relative_posix: '', code: '', message: '' });
        pending.length = 0;
        break;
      }
      scannedFiles += 1;
      if (entry.size > (options.maxFileBytes ?? MAX_FILE_BYTES)) {
        facts.push({ relative_posix: relative, size_bytes: entry.size, mtime: entry.mtime ?? '', content_sha256: '', tags: [], classification: 'unknown', parse_status: 'FILE_TOO_LARGE', error_code: 'FILE_TOO_LARGE', error_message: '文件超过单文件读取上限，已跳过。', raw_bytes: new Uint8Array() });
        continue;
      }
      try {
        facts.push(await scanFileBytes(relative, await adapter.readBytes(parts), { size_bytes: entry.size, mtime: entry.mtime, maxFileBytes: options.maxFileBytes }));
      } catch {
        facts.push({ relative_posix: relative, size_bytes: entry.size, mtime: entry.mtime ?? '', content_sha256: '', tags: [], classification: 'unknown', parse_status: 'FILE_READ_FAILED', error_code: 'FILE_READ_FAILED', error_message: '文件读取失败。', raw_bytes: new Uint8Array() });
      }
    }
  }
  return collectListing(facts, skips, { managedRoots, excludedRoots, scannedAt: options.scannedAt, path: options.path });
}

/** 详情纯函数；decision/stale 由调用方注入。 */
export async function loadFileDetail(item, rawBytes) {
  let detailItem = { ...item, sha256: await sha256Bytes(rawBytes), size_bytes: rawBytes.byteLength, tags: [], mtr_state: 'unknown', parse_status: 'ok', links_out: 0 };
  try {
    const document = parseDocumentSync(rawBytes, item.vault_relative_path, detailItem.sha256);
    detailItem = { ...detailItem, tags: document.tags, mtr_state: document.classification, parse_status: 'ok', sha256: document.content_sha256 };
  } catch (error) {
    detailItem.parse_status = error instanceof MarkdownParseError ? error.errorCode : 'MARKDOWN_PARSE_FAILED';
  }
  let text = '';
  try {
    text = decodeUtf8Sig(rawBytes);
  } catch {
    detailItem.parse_status = 'INVALID_ENCODING';
    detailItem.mtr_state = 'unknown';
  }
  if (text && detailItem.parse_status !== 'INVALID_ENCODING') {
    detailItem.links_out = extractWikiLinks(splitLines(text)).length;
  }
  const [frontmatter, body] = splitFrontmatter(text, detailItem.parse_status);
  return {
    item: detailItem,
    frontmatter,
    body: { text: body, raw_only: detailItem.parse_status !== 'ok' },
    links: { out: detailItem.links_out, in: detailItem.links_in ?? 0 },
  };
}
