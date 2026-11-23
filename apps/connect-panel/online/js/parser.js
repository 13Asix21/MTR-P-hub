import { sha256Bytes } from './hasher.js?v=20261123l';
import { loadYaml } from '../vendor/js-yaml.mjs';

const INLINE_TAG = /(?<![\p{L}\p{N}_#])#(mtr\/(?:trial|rarely))(?![\p{L}\p{N}_/-])/gu;
const INLINE_CODE = /(`+)(.*?)\1/g;
const WIKI_LINK = /\[\[([^\[\]\r\n]+)\]\]/g;
export class MarkdownParseError extends Error {
  /** @param {'INVALID_YAML'|'INVALID_ENCODING'} code */
  constructor(code) {
    super(code);
    this.name = 'MarkdownParseError';
    this.errorCode = code;
  }
}

/** 与 Python splitlines(keepends=True) 相同，识别全部 Unicode 行边界。 */
export function splitLines(text) {
  if (typeof text !== 'string') {
    throw new TypeError('TEXT_REQUIRED');
  }
  return text.match(/[^\n\r\u000b\u000c\u001c-\u001e\u0085\u2028\u2029]*(?:\r\n|[\n\r\u000b\u000c\u001c-\u001e\u0085\u2028\u2029])|[^\n\r\u000b\u000c\u001c-\u001e\u0085\u2028\u2029]+$/gu) ?? [];
}

/** utf-8-sig 严格解码：只允许开头一个 BOM，非法字节不替换。 */
export function decodeUtf8Sig(rawBytes) {
  if (!(rawBytes instanceof Uint8Array)) {
    throw new TypeError('BYTES_REQUIRED');
  }
  try {
    const decoded = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(rawBytes);
    return decoded.startsWith('\uFEFF') ? decoded.slice(1) : decoded;
  } catch (error) {
    if (error instanceof TypeError) {
      throw new MarkdownParseError('INVALID_ENCODING');
    }
    throw error;
  }
}

/** @param {string[]} lines */
export function frontmatterEnd(lines) {
  if (lines.length === 0 || lines[0].trim() !== '---') {
    return null;
  }
  for (let index = 1; index < lines.length; index += 1) {
    if (lines[index].trim() === '---') {
      return index;
    }
  }
  throw new MarkdownParseError('INVALID_YAML');
}

/**
 * Python re 的 \w 是 Unicode 字母/数字/下划线；JS 用显式 Unicode 属性复刻。
 * @param {string[]} lines
 */
export function inlineTagsOutsideCode(lines) {
  const tags = new Set();
  let fenceCharacter = null;
  let fenceLength = 0;
  for (const line of lines) {
    const withoutNewline = line.replace(/[\r\n]+$/u, '');
    if (fenceCharacter !== null) {
      const closing = new RegExp(`^ {0,3}${fenceCharacter === '`' ? '`' : '~'}{${fenceLength},}\\s*$`, 'u');
      if (closing.test(withoutNewline)) {
        fenceCharacter = null;
        fenceLength = 0;
      }
      continue;
    }
    const opening = /^ {0,3}(`{3,}|~{3,})/u.exec(withoutNewline);
    if (opening) {
      fenceCharacter = opening[1][0];
      fenceLength = opening[1].length;
      continue;
    }
    if (withoutNewline.startsWith('    ') || withoutNewline.startsWith('\t')) {
      continue;
    }
    INLINE_CODE.lastIndex = 0;
    const visible = withoutNewline.replace(INLINE_CODE, '');
    INLINE_TAG.lastIndex = 0;
    for (const match of visible.matchAll(INLINE_TAG)) {
      tags.add(match[1]);
    }
  }
  return [...tags].sort(compareCodePoints);
}

/** @param {string[]} lines */
export function extractWikiLinks(lines) {
  const targets = [];
  let fenceCharacter = null;
  let fenceLength = 0;
  for (const line of lines) {
    const withoutNewline = line.replace(/[\r\n]+$/u, '');
    if (fenceCharacter !== null) {
      const closing = new RegExp(`^ {0,3}${fenceCharacter === '`' ? '`' : '~'}{${fenceLength},}\\s*$`, 'u');
      if (closing.test(withoutNewline)) {
        fenceCharacter = null;
        fenceLength = 0;
      }
      continue;
    }
    const opening = /^ {0,3}(`{3,}|~{3,})/u.exec(withoutNewline);
    if (opening) {
      fenceCharacter = opening[1][0];
      fenceLength = opening[1].length;
      continue;
    }
    if (withoutNewline.startsWith('    ') || withoutNewline.startsWith('\t')) {
      continue;
    }
    INLINE_CODE.lastIndex = 0;
    const visible = withoutNewline.replace(INLINE_CODE, '');
    for (const match of visible.matchAll(WIKI_LINK)) {
      const target = match[1].split('|', 1)[0].trim();
      if (target) {
        targets.push(target.normalize('NFC'));
      }
    }
  }
  return targets;
}

/** 按 Unicode 码点排序，等价 Python 对 str 元组的默认排序。 */
export function compareCodePoints(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function isPlainMapping(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

/** 保留 Python float 与 int 的区别；JS 原生 Number 本身不携带这一信息。 */
class YamlFloat {
  constructor(value) {
    this.value = value;
  }
}

function loadWithScalarTypes(raw) {
  const data = loadYaml(raw, {
    json: false,
    listener: (event, state) => {
      if (event === 'close' && state.kind === 'scalar' && state.tag === 'tag:yaml.org,2002:float' && Number.isInteger(state.result)) {
        // 在标量转 nested array/object 之前替换该精确位置，1e3 才能保留为 Python 1000.0。
        state.result = new YamlFloat(state.result);
      }
    },
  });
  return { data };
}

/** js-yaml CORE schema 与 ruamel safe 的 YAML 1.2 核心标量边界对齐。 */
function normalizeYaml(value) {
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? String(value) : value.toISOString().slice(0, 10);
  }
  if (typeof value === 'number' && Object.is(value, -0)) {
    return 0;
  }
  if (Array.isArray(value)) {
    return value.map(normalizeYaml);
  }
  if (isPlainMapping(value)) {
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, normalizeYaml(entry)]));
  }
  return value;
}

export function loadFrontmatter(raw) {
  let parsed;
  try {
    parsed = loadYaml(raw, { json: false });
  } catch {
    throw new MarkdownParseError('INVALID_YAML');
  }
  if (parsed === null || parsed === undefined) {
    return {};
  }
  if (!isPlainMapping(parsed)) {
    throw new MarkdownParseError('INVALID_YAML');
  }
  const tags = Object.hasOwn(parsed, 'tags') ? parsed.tags : [];
  if (typeof tags === 'string') {
    // Python str.split() 按 Unicode 空白拆分。
    parsed.tags = tags.split(/\s+/u).filter(Boolean).map((part) => part.replace(/^#+/u, ''));
  } else if (Array.isArray(tags) && tags.every((item) => typeof item === 'string')) {
    parsed.tags = tags.map((part) => part.replace(/^#+/u, ''));
  } else {
    throw new MarkdownParseError('INVALID_YAML');
  }
  return normalizeYaml(parsed);
}

/** 对拍 ObsidianMarkdownParser.parse；同步计算在 scanner 中可复用已得摘要。 */
export function parseDocumentSync(rawBytes, relativePath, contentSha256) {
  const text = decodeUtf8Sig(rawBytes);
  const lines = splitLines(text);
  const end = frontmatterEnd(lines);
  const fallbackTitle = relativePath.split('/').at(-1)?.replace(/\.[^.]*$/u, '') || 'Note';
  let title = fallbackTitle;
  let frontmatterTags = [];
  let bodyLines = lines;
  if (end !== null) {
    const data = loadFrontmatter(lines.slice(1, end).join(''));
    frontmatterTags = Object.hasOwn(data, 'tags') ? data.tags : [];
    if (typeof data.title === 'string' && data.title.trim()) {
      title = data.title.trim();
    }
    bodyLines = lines.slice(end + 1);
  }
  const tags = [...new Set([...frontmatterTags, ...inlineTagsOutsideCode(bodyLines)])].sort(compareCodePoints);
  const trial = tags.includes('mtr/trial');
  const rarely = tags.includes('mtr/rarely');
  const classification = trial && rarely ? 'conflict' : trial ? 'trial' : rarely ? 'rarely' : 'unmanaged';
  return {
    tags,
    title,
    content_sha256: contentSha256,
    classification,
    needs_classification: !(trial || rarely),
    reason_code: classification === 'conflict' ? 'MTR_TAG_CONFLICT' : null,
  };
}

/** @param {Uint8Array} rawBytes @param {string} relativePath */
export async function parseDocument(rawBytes, relativePath) {
  return parseDocumentSync(rawBytes, relativePath, await sha256Bytes(rawBytes));
}

/** 对齐 Python _frontmatter_value 的 JSON 展示语义，包括 ensure_ascii=False。 */
export function pythonJson(value) {
  const rendered = JSON.stringify(value, (_key, entry) => {
    const unwrapped = entry instanceof YamlFloat ? entry.value : entry;
    return typeof unwrapped === 'number' && !Number.isFinite(unwrapped) ? String(unwrapped) : unwrapped;
  }).replace(/":/gu, '": ').replace(/,"/gu, ', "').replace(/,([^\s\]}"])/gu, ', $1');
  return rendered.replace(/[\u007f-\uffff]/g, (character) => {
    if (character === '\u2028' || character === '\u2029') {
      return `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`;
    }
    return character;
  });
}

export function frontmatterValue(value) {
  if (value instanceof YamlFloat) {
    return [Number.isInteger(value.value) && Number.isFinite(value.value) ? `${value.value}.0` : String(value.value), 'number'];
  }
  if (value === null || value === undefined) {
    return ['', 'null'];
  }
  if (typeof value === 'boolean') {
    return [value ? 'true' : 'false', 'boolean'];
  }
  if (typeof value === 'string') {
    return [value, 'string'];
  }
  if (typeof value === 'number') {
    return [String(value), Number.isInteger(value) ? 'integer' : 'number'];
  }
  if (Array.isArray(value)) {
    return [pythonJson(value), 'array'];
  }
  if (isPlainMapping(value)) {
    return [pythonJson(value), 'object'];
  }
  return [String(value), typeof value];
}

export function splitFrontmatter(text, parseStatus) {
  const frontmatter = { raw: '', entries: [], parse_status: parseStatus };
  const lines = splitLines(text);
  if (lines.length === 0 || lines[0].trim() !== '---') {
    return [frontmatter, text];
  }
  const end = lines.findIndex((line, index) => index > 0 && line.trim() === '---');
  if (end < 0) {
    frontmatter.raw = lines.slice(1).join('');
    return [frontmatter, text];
  }
  const raw = lines.slice(1, end).join('');
  const body = lines.slice(end + 1).join('');
  frontmatter.raw = raw;
  if (parseStatus === 'ok') {
    // 详情层沿用 ruamel 默认 schema；数字呈现由 frontmatterValue 收紧到参照口径。
    let data;
    try {
      data = loadWithScalarTypes(raw).data;
    } catch {
      data = null;
    }
    if (isPlainMapping(data)) {
      frontmatter.entries = Object.entries(data).map(([key, value]) => {
        const [rendered, valueType] = frontmatterValue(value);
        return { key, value: rendered, value_type: valueType };
      });
    }
  }
  return [frontmatter, body];
}
