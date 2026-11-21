import { sha256Bytes } from './hasher.js';
import { buildExportItems } from './reviewDomain.js';

export const EXPORT_CSV_FIELDS = Object.freeze([
  'vault_relative_path', 'action', 'reason', 'content_sha256', 'decided_at',
  'decided_by', 'mtr_state', 'tags', 'parse_status', 'stale', 'size_bytes', 'mtime',
]);

function csvCell(value) {
  let text = String(value ?? '');
  if (/[",\r\n]/u.test(text)) text = `"${text.replaceAll('"', '""')}"`;
  return text;
}

/** Python csv.DictWriter 同构：UTF-8 BOM、CRLF、必要时 RFC4180 引号。 */
export function exportCsv(items) {
  const rows = [EXPORT_CSV_FIELDS.join(',')];
  for (const item of items) {
    rows.push([
      item.vault_relative_path,
      item.action,
      item.reason,
      item.content_sha256,
      item.decided_at,
      item.decided_by,
      item.mtr_state || '',
      (item.tags ?? []).map(String).join(';'),
      item.parse_status || '',
      item.stale ? 'true' : 'false',
      item.size_bytes ?? '',
      item.mtime || '',
    ].map(csvCell).join(','));
  }
  return `\uFEFF${rows.join('\r\n')}\r\n`;
}

export async function onlineVaultId(vaultRoot, listing) {
  const input = new TextEncoder().encode(`${vaultRoot}|2-Permanent|${listing.total}`);
  return (await sha256Bytes(input)).slice(0, 16);
}

/**
 * freshSha 必须来自 FileSystemFileHandle.getFile() 现读；传入的函数不可退回扫描缓存 SHA。
 */
export async function buildExport({ listing, state, include = 'approved', userId, vaultRoot, freshSha, exportedAt = new Date().toISOString(), vaultId }) {
  const currentHashes = {};
  for (const path of Object.keys(state.items)) {
    currentHashes[path] = await freshSha(path);
  }
  const selection = buildExportItems(listing, state, include, (path) => currentHashes[path] ?? null, userId);
  return {
    document: {
      schema_version: '1',
      exported_at: exportedAt,
      user_id: userId,
      vault_id: vaultId ?? await onlineVaultId(vaultRoot, listing),
      scope: {
        vault_root: vaultRoot,
        managed_roots: [...listing.managed_roots],
        excluded_roots: [...listing.excluded_roots],
        scanned_at: listing.scanned_at,
        total_scanned: listing.total,
        include,
        runtime: 'online',
        user_agent_family: 'chromium',
        vault_id_source: 'online',
      },
      counts: {
        exported_items: selection.items.length,
        approved_total: selection.approved_total,
        excluded_stale: selection.excluded_stale,
        scope_note: include === 'approved'
          ? 'items 仅含 approved 且当前内容 hash 复核一致'
          : 'items 含全部已决篇；stale 字段按导出时现算标注',
      },
      items: selection.items,
    },
    preview: { items_count: selection.items.length, excluded_stale: selection.excluded_stale, include },
  };
}

export async function previewExport(options) {
  // 同一次现算生成预览与完整文档，避免预览和下载之间读取不同文件版本。
  return buildExport(options);
}

export function exportJson(document) {
  return `${JSON.stringify(document, null, 2)}\n`;
}

export function exportFileStem(userId, exportedAt) {
  const timestamp = exportedAt.replace(/[-:]/gu, '').replace('T', '-').slice(0, 15);
  return `review-${userId}-${timestamp}`;
}

/** 每次下载独立创建并回收 BlobURL；点击失败也不泄漏 URL。 */
export function downloadBlob({ blob, filename, documentRef = globalThis.document, urlApi = globalThis.URL }) {
  if (!blob || typeof filename !== 'string' || !filename || !documentRef || !urlApi) {
    return Promise.reject(new TypeError('下载参数不完整，未创建 BlobURL。'));
  }
  return new Promise((resolve, reject) => {
    const url = urlApi.createObjectURL(blob);
    let anchor;
    try {
      anchor = documentRef.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      anchor.hidden = true;
      documentRef.body.append(anchor);
      anchor.click();
      anchor.remove();
    } catch (error) {
      anchor?.remove();
      urlApi.revokeObjectURL(url);
      reject(error);
      return;
    }
    // 浏览器可能延后开始下载；回收前给消费端一个短暂任务窗口。
    setTimeout(() => {
      urlApi.revokeObjectURL(url);
      resolve({ filename, revoked: true });
    }, 1000);
  });
}

export function downloadExportPair(result, { documentRef, urlApi, suffix = '' } = {}) {
  const stem = exportFileStem(result.document.user_id, result.document.exported_at) + suffix;
  return Promise.all([
    downloadBlob({
      blob: new Blob([exportJson(result.document)], { type: 'application/json;charset=utf-8' }),
      filename: `${stem}.json`, documentRef, urlApi,
    }),
    downloadBlob({
      blob: new Blob([exportCsv(result.document.items)], { type: 'text/csv;charset=utf-8' }),
      filename: `${stem}.csv`, documentRef, urlApi,
    }),
  ]);
}
