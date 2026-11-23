/**
 * 多格式文件预览：不引入第三方渲染库，避免破坏「零 CDN / 同源 vendored」基线。
 *
 * - PDF / 图片：交给浏览器原生渲染（Blob URL）
 * - DOCX / XLSX：是 ZIP 容器，自研最小解析，输出结构摘要而非像素级还原
 * - 其余二进制：只给文件信息，不假装能预览
 *
 * 所有分支都只读，不修改源文件。
 */

export const PREVIEW_KIND = Object.freeze({
  markdown: 'markdown',
  pdf: 'pdf',
  image: 'image',
  docx: 'docx',
  xlsx: 'xlsx',
  text: 'text',
  binary: 'binary',
});

const EXT_MAP = {
  md: PREVIEW_KIND.markdown,
  markdown: PREVIEW_KIND.markdown,
  pdf: PREVIEW_KIND.pdf,
  png: PREVIEW_KIND.image,
  jpg: PREVIEW_KIND.image,
  jpeg: PREVIEW_KIND.image,
  gif: PREVIEW_KIND.image,
  webp: PREVIEW_KIND.image,
  bmp: PREVIEW_KIND.image,
  svg: PREVIEW_KIND.image,
  docx: PREVIEW_KIND.docx,
  doc: PREVIEW_KIND.binary, // 旧二进制格式，浏览器无法解析
  xlsx: PREVIEW_KIND.xlsx,
  xls: PREVIEW_KIND.binary,
  pptx: PREVIEW_KIND.binary,
  txt: PREVIEW_KIND.text,
  csv: PREVIEW_KIND.text,
  json: PREVIEW_KIND.text,
};

export function detectKind(path) {
  const ext = String(path ?? '').split('.').pop()?.toLowerCase() ?? '';
  return EXT_MAP[ext] ?? PREVIEW_KIND.binary;
}

export function extensionOf(path) {
  const parts = String(path ?? '').split('.');
  return parts.length > 1 ? parts.pop().toLowerCase() : '';
}

export function formatSize(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '未知';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

/**
 * 解析 ZIP 中央目录，返回条目名与大小。
 * DOCX/XLSX 都是 ZIP；这里只读目录，不解压正文，避免大文件卡住页面。
 */
export function readZipEntries(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const dec = new TextDecoder('utf-8');
  // 从尾部向前找 EOCD（0x06054b50）
  let eocd = -1;
  const minPos = Math.max(0, bytes.length - 66000);
  for (let i = bytes.length - 22; i >= minPos; i -= 1) {
    if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) return null;
  const count = view.getUint16(eocd + 10, true);
  let offset = view.getUint32(eocd + 16, true);
  const entries = [];
  for (let i = 0; i < count; i += 1) {
    if (offset + 46 > bytes.length) break;
    if (view.getUint32(offset, true) !== 0x02014b50) break;
    const nameLen = view.getUint16(offset + 28, true);
    const extraLen = view.getUint16(offset + 30, true);
    const commentLen = view.getUint16(offset + 32, true);
    const compressed = view.getUint32(offset + 20, true);
    const raw = bytes.subarray(offset + 46, offset + 46 + nameLen);
    entries.push({ name: dec.decode(raw), size: compressed });
    offset += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

/** 从 ZIP 中取出单个条目的解压后文本（仅用于小型 XML，超过 2MB 不解压）。 */
export async function readZipText(bytes, targetName) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const dec = new TextDecoder('utf-8');
  let eocd = -1;
  const minPos = Math.max(0, bytes.length - 66000);
  for (let i = bytes.length - 22; i >= minPos; i -= 1) {
    if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) return null;
  const count = view.getUint16(eocd + 10, true);
  let offset = view.getUint32(eocd + 16, true);
  for (let i = 0; i < count; i += 1) {
    if (offset + 46 > bytes.length) return null;
    if (view.getUint32(offset, true) !== 0x02014b50) return null;
    const method = view.getUint16(offset + 10, true);
    const compressedSize = view.getUint32(offset + 20, true);
    const nameLen = view.getUint16(offset + 28, true);
    const extraLen = view.getUint16(offset + 30, true);
    const commentLen = view.getUint16(offset + 32, true);
    const localOffset = view.getUint32(offset + 42, true);
    const name = dec.decode(bytes.subarray(offset + 46, offset + 46 + nameLen));
    if (name === targetName) {
      if (compressedSize > 2 * 1024 * 1024) return null;
      // 定位本地文件头，跳过其可变的 name/extra 长度
      const lNameLen = view.getUint16(localOffset + 26, true);
      const lExtraLen = view.getUint16(localOffset + 28, true);
      const dataStart = localOffset + 30 + lNameLen + lExtraLen;
      const raw = bytes.subarray(dataStart, dataStart + compressedSize);
      if (method === 0) return dec.decode(raw);
      if (method !== 8) return null;
      if (typeof DecompressionStream !== 'function') return null;
      const stream = new Blob([raw]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
      const buf = await new Response(stream).arrayBuffer();
      return new TextDecoder('utf-8').decode(buf);
    }
    offset += 46 + nameLen + extraLen + commentLen;
  }
  return null;
}

function stripXml(text) {
  return String(text ?? '').replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
}

/** DOCX：读 word/document.xml，按段落与表格粗分，输出可读文本。 */
export async function describeDocx(bytes) {
  const xml = await readZipText(bytes, 'word/document.xml');
  if (!xml) return { ok: false, message: '无法读取 DOCX 正文（可能是加密或非常规结构）。' };
  const paragraphs = [];
  // 按 <w:p> 切段，段内拼接所有 <w:t> 文本
  const paraMatches = xml.match(/<w:p[\s>][\s\S]*?<\/w:p>|<w:p\/>/g) ?? [];
  for (const para of paraMatches) {
    const texts = [...para.matchAll(/<w:t[^>]*>([\s\S]*?)<\/w:t>/g)].map((m) => m[1]);
    const line = stripXml(texts.join(''));
    if (line) paragraphs.push(line);
  }
  const tables = (xml.match(/<w:tbl>/g) ?? []).length;
  return { ok: true, paragraphs, tables, chars: paragraphs.join('').length };
}

function colToIndex(ref) {
  const letters = String(ref ?? '').match(/^[A-Z]+/);
  if (!letters) return 0;
  let n = 0;
  for (const ch of letters[0]) n = n * 26 + (ch.charCodeAt(0) - 64);
  return n - 1;
}

function unescapeXml(text) {
  return String(text ?? '')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'").replace(/&#(\d+);/g, (_, d) => String.fromCharCode(Number(d)))
    .replace(/&amp;/g, '&');
}

/** 解析单元格取值：支持共享字符串、内联字符串、数字、布尔与公式结果。 */
function cellValue(cellXml, shared) {
  const type = (cellXml.match(/\st="([^"]+)"/) ?? [])[1] ?? 'n';
  const vMatch = cellXml.match(/<v[^>]*>([\s\S]*?)<\/v>/);
  const inline = cellXml.match(/<is[\s\S]*?<t[^>]*>([\s\S]*?)<\/t>/);
  if (type === 'inlineStr') return unescapeXml(inline ? inline[1] : '');
  if (!vMatch) return '';
  const raw = vMatch[1];
  if (type === 's') return shared[Number(raw)] ?? '';
  if (type === 'b') return raw === '1' ? 'TRUE' : 'FALSE';
  if (type === 'str') return unescapeXml(raw);
  return raw;
}

/**
 * XLSX 完整表格渲染：读出每个工作表的行列与单元格文本。
 * 只做只读解析，不计算公式（显示公式缓存值）。
 */
export async function describeXlsx(bytes, options = {}) {
  const maxRows = options.maxRows ?? 300;
  const maxCols = options.maxCols ?? 40;
  const entries = readZipEntries(bytes);
  if (!entries) return { ok: false, message: '无法读取 XLSX（可能是加密或非常规结构）。' };

  const sharedXml = await readZipText(bytes, 'xl/sharedStrings.xml');
  const shared = sharedXml
    ? [...sharedXml.matchAll(/<si>([\s\S]*?)<\/si>/g)].map((m) => {
        const texts = [...m[1].matchAll(/<t[^>]*>([\s\S]*?)<\/t>/g)].map((t) => unescapeXml(t[1]));
        return texts.join('');
      })
    : [];

  const workbook = await readZipText(bytes, 'xl/workbook.xml');
  const sheetNames = workbook ? [...workbook.matchAll(/<sheet[^>]*name="([^"]+)"/g)].map((m) => unescapeXml(m[1])) : [];
  const sheetEntries = entries
    .filter((e) => /^xl\/worksheets\/sheet\d+\.xml$/.test(e.name))
    .sort((a, b) => (a.name < b.name ? -1 : 1));

  const sheets = [];
  for (let i = 0; i < sheetEntries.length; i += 1) {
    const xml = await readZipText(bytes, sheetEntries[i].name);
    if (!xml) {
      sheets.push({ name: sheetNames[i] ?? `Sheet${i + 1}`, rows: [], truncated: false });
      continue;
    }
    const rows = [];
    let truncated = false;
    const rowMatches = xml.match(/<row[\s>][\s\S]*?<\/row>/g) ?? [];
    for (const rowXml of rowMatches) {
      if (rows.length >= maxRows) { truncated = true; break; }
      const cells = [];
      const cellMatches = rowXml.match(/<c[\s>][\s\S]*?<\/c>|<c[^>]*\/>/g) ?? [];
      for (const cellXml of cellMatches) {
        const ref = (cellXml.match(/\sr="([A-Z]+\d+)"/) ?? [])[1];
        const index = ref ? colToIndex(ref) : cells.length;
        if (index >= maxCols) { truncated = true; continue; }
        while (cells.length < index) cells.push('');
        cells[index] = cellValue(cellXml, shared);
      }
      if (cells.some((c) => String(c).trim())) rows.push(cells);
    }
    sheets.push({ name: sheetNames[i] ?? `Sheet${i + 1}`, rows, truncated });
  }
  return { ok: true, sheets, sharedCount: shared.length };
}

/** 生成 Blob URL；调用方负责在切换后 revoke，避免内存泄漏。 */
export function blobUrl(bytes, kind) {
  const type = kind === PREVIEW_KIND.pdf ? 'application/pdf' : 'application/octet-stream';
  return URL.createObjectURL(new Blob([bytes], { type }));
}
