import { pythonCasefold as casefold } from './pythonCasefold.js?v=20261123s';

export const REVIEW_HISTORY_LIMIT = 200;
export const REVIEW_REASON_MAX_CHARS = 500;
export const REVIEW_BATCH_MAX_ITEMS = 500;
export const ACTIONS = Object.freeze(['approved', 'rejected', 'skipped']);
export const HISTORY_OPS = Object.freeze(['DECIDE', 'UNDO', 'STALE_RESET']);
const SHA256_PATTERN = /^[0-9a-f]{64}$/u;

export class ReviewError extends Error {
  constructor(code, message, httpStatus = 400) {
    super(message);
    this.name = 'ReviewError';
    this.code = code;
    this.httpStatus = httpStatus;
  }
}

function invalid(message = '审核记录结构损坏，已拒绝操作。') {
  throw new ReviewError('REVIEW_RECORD_INVALID', message, 503);
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function exactKeys(value, expected) {
  return isObject(value) && Object.keys(value).length === expected.length && expected.every((key) => Object.hasOwn(value, key));
}

function timestamp(value, stored = true) {
  // JS 域的 now 只接收固定 +00:00 字面值；对拍输入也采用该形式。
  if (typeof value !== 'string' || !value.endsWith('+00:00') || !validIsoTimestamp(value)) {
    if (stored) {
      invalid('审核记录时间必须是带 +00:00 的 ISO8601 字符串。');
    }
    throw new ReviewError('REQUEST_VALIDATION_FAILED', '时间必须是带 +00:00 的 ISO8601 字符串。', 422);
  }
  return value;
}

function validIsoTimestamp(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\+00:00$/u.exec(value);
  if (!match) {
    return false;
  }
  const [, year, month, day, hour, minute, second] = match;
  const numeric = [year, month, day, hour, minute, second].map(Number);
  const date = new Date(Date.UTC(numeric[0], numeric[1] - 1, numeric[2]));
  return date.getUTCFullYear() === numeric[0] && date.getUTCMonth() === numeric[1] - 1 && date.getUTCDate() === numeric[2]
    && numeric[3] <= 23 && numeric[4] <= 59 && numeric[5] <= 59;
}

export function reviewPath(value, stored = true) {
  const valid = typeof value === 'string' && value.length >= 1 && value.length <= 2048 && !value.includes('\0')
    && value.normalize('NFC') === value && !value.startsWith('/') && !value.startsWith('\\')
    && !value.includes('\\') && !value.includes(':') && !/^[A-Za-z][A-Za-z0-9+.-]*:\/\//u.test(value)
    && value.split('/').every((part) => part !== '' && part !== '.' && part !== '..');
  if (!valid) {
    if (stored) {
      invalid('审核记录包含无效或非 NFC 的相对路径。');
    }
    throw new ReviewError('REVIEW_PATH_INVALID', '审核路径必须是安全的 NFC 相对 POSIX 路径。', 400);
  }
  return value;
}

function sha256(value, stored = true) {
  if (typeof value !== 'string' || !SHA256_PATTERN.test(value)) {
    if (stored) {
      invalid('审核记录包含无效 content_sha256。');
    }
    throw new ReviewError('REQUEST_VALIDATION_FAILED', 'content_sha256 必须是 64 位小写十六进制。', 422);
  }
  return value;
}

function reason(value, stored = true) {
  if (value === null && !stored) {
    return '';
  }
  if (typeof value !== 'string' || [...value].length > REVIEW_REASON_MAX_CHARS) {
    if (stored) {
      invalid('审核理由必须是至多 500 字符的字符串。');
    }
    throw new ReviewError('REQUEST_VALIDATION_FAILED', '审核理由不能超过 500 字符。', 422);
  }
  return value;
}

export function emptyState() {
  return { schema_version: '1', items: {}, history: [], last_position: null };
}

export function validateState(raw) {
  if (raw === null || raw === undefined) {
    return emptyState();
  }
  if (!exactKeys(raw, ['schema_version', 'items', 'history', 'last_position']) || raw.schema_version !== '1') {
    invalid('review 顶层结构或 schema_version 无效。');
  }
  if (!isObject(raw.items) || !Array.isArray(raw.history)) {
    invalid('review.items 必须是对象，review.history 必须是数组。');
  }
  if (raw.history.length > REVIEW_HISTORY_LIMIT) {
    invalid('review.history 超过 200 条上限。');
  }
  const items = {};
  for (const [path, record] of Object.entries(raw.items)) {
    if (!exactKeys(record, ['action', 'reason', 'content_sha256', 'decided_at', 'updated_at'])) {
      invalid('ReviewRecord 字段不完整或包含未知字段。');
    }
    if (!ACTIONS.includes(record.action)) {
      invalid('ReviewRecord.action 无效。');
    }
    items[reviewPath(path)] = {
      action: record.action,
      reason: reason(record.reason),
      content_sha256: sha256(record.content_sha256),
      decided_at: timestamp(record.decided_at),
      updated_at: timestamp(record.updated_at),
    };
  }
  const history = raw.history.map((event) => {
    if (!exactKeys(event, ['path', 'op', 'from', 'to', 'reason', 'content_sha256', 'at'])) {
      invalid('HistoryEvent 字段不完整或包含未知字段。');
    }
    if (!HISTORY_OPS.includes(event.op)) {
      invalid('HistoryEvent.op 无效。');
    }
    if (event.from !== null && !ACTIONS.includes(event.from)) {
      invalid('HistoryEvent.from 无效。');
    }
    if (event.to !== null && !ACTIONS.includes(event.to)) {
      invalid('HistoryEvent.to 无效。');
    }
    if (event.op === 'DECIDE' && !ACTIONS.includes(event.to)) {
      invalid('DECIDE 历史必须有目标动作。');
    }
    if (['UNDO', 'STALE_RESET'].includes(event.op) && (!ACTIONS.includes(event.from) || event.to !== null)) {
      invalid('撤销历史必须保留前动作且目标为空。');
    }
    return {
      path: reviewPath(event.path),
      op: event.op,
      from: event.from,
      to: event.to,
      reason: event.reason === null ? null : reason(event.reason),
      content_sha256: event.content_sha256 === null ? null : sha256(event.content_sha256),
      at: timestamp(event.at),
    };
  });
  return {
    schema_version: '1',
    items,
    history,
    last_position: raw.last_position === null ? null : reviewPath(raw.last_position),
  };
}

function withHistory(state, event) {
  return [...state.history, event].slice(-REVIEW_HISTORY_LIMIT);
}

export function decide(state, path, action, reviewReason, contentSha256, now) {
  const current = validateState(state);
  const normalizedPath = reviewPath(path, false);
  if (!ACTIONS.includes(action)) {
    throw new ReviewError('REQUEST_VALIDATION_FAILED', '审核动作无效。', 422);
  }
  const cleanReason = reason(reviewReason, false);
  const cleanSha = sha256(contentSha256, false);
  const at = timestamp(now, false);
  const previous = current.items[normalizedPath];
  const items = { ...current.items, [normalizedPath]: { action, reason: cleanReason, content_sha256: cleanSha, decided_at: at, updated_at: at } };
  const event = { path: normalizedPath, op: 'DECIDE', from: previous?.action ?? null, to: action, reason: cleanReason, content_sha256: cleanSha, at };
  return { schema_version: '1', items, history: withHistory(current, event), last_position: current.last_position };
}

export function reset(state, path, mode, now) {
  const current = validateState(state);
  const normalizedPath = reviewPath(path, false);
  if (mode !== 'undo' && mode !== 'stale_reset') {
    throw new ReviewError('REQUEST_VALIDATION_FAILED', '重置模式无效。', 422);
  }
  const previous = current.items[normalizedPath];
  if (!previous) {
    throw new ReviewError('REVIEW_ITEM_NOT_DECIDED', '该篇没有可撤销的审核记录。', 404);
  }
  const items = { ...current.items };
  delete items[normalizedPath];
  const at = timestamp(now, false);
  const event = {
    path: normalizedPath,
    op: mode === 'undo' ? 'UNDO' : 'STALE_RESET',
    from: previous.action,
    to: null,
    reason: null,
    content_sha256: previous.content_sha256,
    at,
  };
  return { schema_version: '1', items, history: withHistory(current, event), last_position: current.last_position };
}

export function reviewStatusFor(record, currentSha) {
  if (record === null || record === undefined) {
    return 'pending';
  }
  return record.content_sha256 !== currentSha ? 'stale' : record.action;
}

export function computeProgress(listing, state) {
  const current = validateState(state);
  const counts = { reviewed: 0, approved: 0, rejected: 0, skipped: 0, pending: 0, stale: 0, invalid: 0, pass_rate: null };
  const items = listing.items ?? [];
  for (const item of items) {
    if (item.parse_status !== 'ok') {
      counts.invalid += 1;
    }
    const status = reviewStatusFor(current.items[item.vault_relative_path], String(item.sha256 ?? ''));
    if (ACTIONS.includes(status)) {
      counts[status] += 1;
    } else if (status === 'stale') {
      counts.stale += 1;
    }
  }
  counts.reviewed = ACTIONS.reduce((total, action) => total + counts[action], 0);
  const total = Number(listing.total ?? items.length);
  counts.pending = total - counts.reviewed;
  counts.pass_rate = counts.reviewed === 0 ? null : counts.approved / counts.reviewed;
  return counts;
}

export function computeGlobal(listing, allStates) {
  const states = Object.fromEntries(Object.entries(allStates).map(([userId, state]) => [userId, validateState(state)]));
  const globalCounts = { approved: 0, rejected: 0, skipped: 0, diverged: 0, pending: 0 };
  const conflicts = [];
  let conflictTotal = 0;
  for (const item of listing.items ?? []) {
    const decisions = {};
    for (const [userId, state] of Object.entries(states)) {
      const record = state.items[item.vault_relative_path];
      if (record && record.content_sha256 === String(item.sha256 ?? '')) {
        decisions[userId] = record.action;
      }
    }
    const actions = [...new Set(Object.values(decisions))].sort();
    if (actions.length > 1) {
      globalCounts.diverged += 1;
      conflictTotal += 1;
      if (conflicts.length < 100) {
        conflicts.push({ path: item.vault_relative_path, actions, decisions });
      }
    } else if (actions.length === 1) {
      globalCounts[actions[0]] += 1;
    } else {
      globalCounts.pending += 1;
    }
  }
  const perUser = Object.keys(states).sort().map((userId) => ({ user_id: userId, counts: computeProgress(listing, states[userId]) }));
  return { ...globalCounts, per_user: perUser, conflicts, conflicts_truncated: conflictTotal > conflicts.length };
}

function parentDirectory(path) {
  const parts = path.split('/');
  parts.pop();
  return parts.join('/');
}

export function coverageByDirectory(listing, state) {
  const current = validateState(state);
  const directories = {};
  for (const item of listing.items ?? []) {
    const path = String(item.vault_relative_path ?? '');
    const directory = parentDirectory(path);
    const counts = directories[directory] ??= { total: 0, reviewed: 0, approved: 0, rejected: 0, skipped: 0, pending: 0, stale: 0, invalid: 0 };
    counts.total += 1;
    if (item.parse_status !== 'ok') {
      counts.invalid += 1;
    }
    const status = reviewStatusFor(current.items[path], String(item.sha256 ?? ''));
    if (ACTIONS.includes(status)) {
      counts[status] += 1;
      counts.reviewed += 1;
    } else if (status === 'stale') {
      counts.stale += 1;
    }
  }
  for (const counts of Object.values(directories)) {
    counts.pending = counts.total - counts.reviewed;
  }
  return Object.fromEntries(Object.entries(directories).sort(([left], [right]) => casefold(left) < casefold(right) ? -1 : casefold(left) > casefold(right) ? 1 : 0));
}

export function buildExportItems(listing, state, include, freshSha, userId = 'default') {
  if (include !== 'approved' && include !== 'all') {
    throw new ReviewError('REQUEST_VALIDATION_FAILED', '导出范围无效。', 422);
  }
  const current = validateState(state);
  const items = [];
  let excludedStale = 0;
  let approvedTotal = 0;
  for (const listingItem of listing.items ?? []) {
    const path = String(listingItem.vault_relative_path ?? '');
    const record = current.items[path];
    if (!record) {
      continue;
    }
    if (record.action === 'approved') {
      approvedTotal += 1;
    }
    if (include === 'approved' && record.action !== 'approved') {
      continue;
    }
    const stale = freshSha(path) !== record.content_sha256;
    if (include === 'approved' && stale) {
      excludedStale += 1;
      continue;
    }
    const exported = {
      vault_relative_path: path,
      action: record.action,
      reason: record.reason,
      content_sha256: record.content_sha256,
      decided_at: record.decided_at,
      decided_by: userId,
      mtr_state: listingItem.mtr_state ?? null,
      tags: [...(listingItem.tags ?? [])],
      parse_status: listingItem.parse_status ?? null,
      size_bytes: listingItem.size_bytes ?? null,
      mtime: listingItem.mtime ?? null,
    };
    if (include === 'all') {
      exported.stale = stale;
    }
    items.push(exported);
  }
  items.sort((left, right) => casefold(left.vault_relative_path) < casefold(right.vault_relative_path) ? -1 : casefold(left.vault_relative_path) > casefold(right.vault_relative_path) ? 1 : 0);
  return { items, excluded_stale: excludedStale, approved_total: approvedTotal };
}
