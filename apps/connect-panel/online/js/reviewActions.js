import { readBrowserDetail, readCurrentSha } from './browserScan.js?v=20261124h';
import { sha256Bytes } from './hasher.js?v=20261124h';
import { ReviewError, decide, reset, validateState } from './reviewDomain.js?v=20261124h';

export function utcTimestamp(now = new Date()) {
  const date = now instanceof Date ? now : new Date(now);
  if (Number.isNaN(date.getTime())) throw new ReviewError('REQUEST_VALIDATION_FAILED', '无法生成审核时间。', 422);
  return `${date.toISOString().slice(0, -1)}+00:00`;
}

export function decisionFor(record) {
  return record ? { action: record.action, reason: record.reason, decided_at: record.decided_at } : null;
}

/** 详情复用清单链接计数，同时现算 SHA、解析状态与正文。 */
export async function loadReviewDetail(adapter, item, state, options = {}) {
  const relative = options.sourcePath ?? item.vault_relative_path;
  const detail = await readBrowserDetail(adapter, item, relative);
  const record = validateState(state).items[item.vault_relative_path];
  return {
    ...detail,
    decision: decisionFor(record),
    stale: Boolean(record && record.content_sha256 !== detail.item.sha256),
    read_only: true,
  };
}

function sourcePath(options, path) {
  return options.sourcePaths?.[path] ?? path;
}

async function currentSha(adapter, path, options) {
  const current = await readCurrentSha(adapter, sourcePath(options, path), sha256Bytes);
  if (current.bytes.byteLength > (options.maxFileBytes ?? 1_048_576)) {
    throw new ReviewError('FILE_TOO_LARGE', '文件读取期间超过单文件上限，已拒绝处理。', 413);
  }
  return current.sha256;
}

function fileError(error) {
  if (error?.code) return error;
  if (['NotFoundError', 'NotAllowedError', 'InvalidStateError', 'TypeError'].includes(error?.name)) {
    return new ReviewError('REVIEW_FILE_MISSING', '清单中的文件已消失或当前无法读取。', 404);
  }
  return error;
}

/**
 * 唯一动作入口：文件防护先完成，再写 IndexedDB，最后提交内存状态。
 * CONTENT_CHANGED 时尚未计算新状态或入队，保证 history 也零变化。
 */
export class ReviewActionService {
  constructor({ adapter, offlineBuffer, userId = 'offline-local', vaultId, sourcePaths = {}, now = () => new Date() }) {
    if (!offlineBuffer || typeof offlineBuffer.enqueueAction !== 'function') {
      throw new TypeError('ReviewActionService 需要真实离线缓冲接口。');
    }
    this.adapter = adapter;
    this.offlineBuffer = offlineBuffer;
    this.userId = userId;
    this.vaultId = vaultId;
    this.sourcePaths = sourcePaths;
    this.now = now;
    this.sequence = Promise.resolve();
  }

  serial(operation) {
    const result = this.sequence.then(operation, operation);
    this.sequence = result.catch(() => {});
    return result;
  }

  async detail(item, state) {
    const path = item.vault_relative_path;
    try {
      return await loadReviewDetail(this.adapter, item, state, { sourcePath: sourcePath(this, path) });
    } catch (error) {
      throw fileError(error);
    }
  }

  async decide(state, item, seenSha, action, reason = '') {
    return this.serial(async () => {
      const path = item.vault_relative_path;
      // 纯 domain 先校验输入；该临时值不会暴露、入队或提交给 UI。
      const at = utcTimestamp(this.now());
      decide(validateState(state), path, action, reason, seenSha, at);
      let current;
      try {
        current = await readCurrentSha(this.adapter, sourcePath(this, path), sha256Bytes);
      } catch (error) {
        throw fileError(error);
      }
      if (current.bytes.byteLength > 1_048_576) {
        throw new ReviewError('FILE_TOO_LARGE', '文件读取期间超过单文件上限，已拒绝处理。', 413);
      }
      if (current.sha256 !== seenSha) {
        throw new ReviewError('CONTENT_CHANGED', '文件内容已变化，本次审核未写入；请重新打开详情后再决定。', 409);
      }
      const updated = decide(validateState(state), path, action, reason, seenSha, at);
      const event = updated.history.at(-1);
      const queued = await this.offlineBuffer.enqueueAction({
        user_id: this.userId,
        vault_id: this.vaultId,
        op: 'DECIDE',
        path,
        action,
        to: event.to,
        from: event.from,
        reason: event.reason,
        content_sha256: event.content_sha256,
        at,
        client_id: 'local-browser',
        created_at: at,
      });
      return { state: updated, event, queued, sync_state: 'pending' };
    });
  }

  async reset(state, path, mode, expectedSha = null, seenSha = null) {
    return this.serial(async () => {
      const at = utcTimestamp(this.now());
      const previous = validateState(state).items[path];
      if (seenSha !== null) {
        if (!previous || seenSha !== previous.content_sha256) {
          throw new ReviewError('CONTENT_CHANGED', '当前详情与已保存审核不一致，未撤销或清空已读队列。', 409);
        }
        if (expectedSha !== null && expectedSha !== seenSha) {
          throw new ReviewError('CONTENT_CHANGED', '撤销请求的当前内容与详情 SHA 不一致，未清空已读队列。', 409);
        }
        const current = await this.freshSha(path);
        if (current === null || current !== seenSha) {
          throw new ReviewError('CONTENT_CHANGED', '当前文件与详情 SHA 不一致，未撤销或清空已读队列。', 409);
        }
      } else if (previous) {
        // 批量接口只携带清单 SHA；已审核记录一致时也必须现读当前文件。
        const current = await this.freshSha(path);
        if (current === null || current !== previous.content_sha256) {
          throw new ReviewError('CONTENT_CHANGED', '文件内容已变化，旧审核已标为 STALE；未撤销或清空已读队列。请重读后再决定。', 409);
        }
        if (mode === 'stale_reset') {
          throw new ReviewError('CONTENT_CHANGED', '请重新打开详情后再处理旧审核。', 409);
        }
      }
      const updated = reset(validateState(state), path, mode, at);
      const event = updated.history.at(-1);
      const queued = await this.offlineBuffer.enqueueAction({
        user_id: this.userId,
        vault_id: this.vaultId,
        op: event.op,
        path,
        from: event.from,
        to: null,
        reason: null,
        content_sha256: event.content_sha256,
        at,
        client_id: 'local-browser',
        created_at: at,
      });
      return { state: updated, event, queued, sync_state: 'pending' };
    });
  }

  async freshSha(path) {
    try {
      return await currentSha(this.adapter, path, this);
    } catch (error) {
      const normalized = fileError(error);
      if (['REVIEW_FILE_MISSING', 'FILE_TOO_LARGE', 'REVIEW_PATH_INVALID'].includes(normalized?.code)) return null;
      throw normalized;
    }
  }
}
