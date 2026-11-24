import { computeProgress, REVIEW_BATCH_MAX_ITEMS } from './reviewDomain.js?v=20261124j';

/** 统一的批量动作入口；任何单篇失败都不回滚、删除或清空其它已读动作。 */
export async function applyBatch({ listing, state, paths, action, reason = '', decide, reset }) {
  if (!Array.isArray(paths) || paths.length === 0 || paths.length > REVIEW_BATCH_MAX_ITEMS) {
    const error = new Error(`批量审核每次须选择 1–${REVIEW_BATCH_MAX_ITEMS} 篇。`);
    error.code = 'BATCH_LIMIT_EXCEEDED';
    throw error;
  }
  if (new Set(paths).size !== paths.length) {
    const error = new Error('批量选择中存在重复路径。');
    error.code = 'BATCH_SELECTION_INVALID';
    throw error;
  }
  const byPath = new Map(listing.items.map((item) => [item.vault_relative_path, item]));
  let current = state;
  const completed = [];
  const failures = [];
  for (const path of paths) {
    const item = byPath.get(path);
    if (!item) {
      failures.push({ path, code: 'REVIEW_ITEM_NOT_FOUND', message: '该篇不在当前扫描清单中。' });
      continue;
    }
    try {
      if (['approved', 'rejected', 'skipped'].includes(action)) {
        if (!item.sha256 || ['FILE_READ_FAILED', 'FILE_TOO_LARGE', 'INVALID_ENCODING'].includes(item.parse_status)) {
          const error = new Error('该篇没有可复核的有效原始字节，请先重新扫描可读文件。');
          error.code = 'REVIEW_FILE_MISSING';
          throw error;
        }
        current = (await decide(current, item, item.sha256, action, reason)).state;
      } else if (action === 'undo') {
        current = (await reset(current, path, 'undo', item.sha256)).state;
      } else {
        const error = new Error('批量动作无效。');
        error.code = 'REQUEST_VALIDATION_FAILED';
        throw error;
      }
      completed.push(path);
    } catch (error) {
      failures.push({ path, code: error.code ?? 'REVIEW_ACTION_FAILED', message: error.message });
    }
  }
  return { state: current, completed, failures, progress: computeProgress(listing, current) };
}

/** 不假定网络或云账号存在，只报告本机档案的真实状态。 */
export function localProfileStatus(state, pending = 0, online = true) {
  return {
    identity: '本地浏览器档案',
    sync: '仅当前浏览器，不是云同步',
    connectivity: online ? '浏览器在线；审核仍只写本机' : '浏览器离线；审核仍只写本机',
    reviewed: Object.keys(state.items).length,
    pending,
  };
}
