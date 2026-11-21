import test from 'node:test';
import assert from 'node:assert/strict';
import {
  MarkdownParseError,
  decodeUtf8Sig,
  extractWikiLinks,
  inlineTagsOutsideCode,
  parseDocumentSync,
  splitFrontmatter,
  splitLines,
} from '../../js/parser.js';

const sha = 'a'.repeat(64);
const bytes = (text) => new TextEncoder().encode(text);

test('Python splitlines 的 Unicode 与 CRLF 边界逐项一致', () => {
  assert.deepEqual(splitLines('a\r\nb\nc\rd\u000be\u0085f\u2028g'), ['a\r\n', 'b\n', 'c\r', 'd\u000b', 'e\u0085', 'f\u2028', 'g']);
  assert.deepEqual(splitLines(''), []);
  assert.deepEqual(splitLines('a\n'), ['a\n']);
});

test('BOM 仅在开头剥离，非法 UTF-8 明确 INVALID_ENCODING', () => {
  assert.equal(decodeUtf8Sig(new Uint8Array([0xef, 0xbb, 0xbf, 0x41])), 'A');
  assert.throws(() => decodeUtf8Sig(new Uint8Array([0xff])), (error) => error instanceof MarkdownParseError && error.errorCode === 'INVALID_ENCODING');
});

const invalidDocuments = [
  ['无闭合', '---\ntitle: a\nbody'],
  ['YAML 语法', '---\na: [1,2\n---\nbody'],
  ['非 mapping', '---\n- a\n---\nbody'],
  ['重复键', '---\ntitle: a\ntitle: b\n---\nbody'],
  ['tags int', '---\ntags: 12\n---\nbody'],
  ['tags mixed', '---\ntags: [a, 2]\n---\nbody'],
  ['tags bool', '---\ntags: true\n---\nbody'],
  ['tags null', '---\ntags: null\n---\nbody'],
  ['tags object', '---\ntags: {a: b}\n---\nbody'],
  ['tags nested', '---\ntags: [[a]]\n---\nbody'],
  ['多文档', '---\ntitle: a\n...\ntitle: b\n---\nbody'],
  ['未闭合引号', "---\ntitle: 'oops\n---\nbody"],
];
for (const [name, text] of invalidDocuments) {
  test(`INVALID_YAML 负例实际执行：${name}`, () => {
    assert.throws(() => parseDocumentSync(bytes(text), 'bad.md', sha), (error) => error instanceof MarkdownParseError && error.errorCode === 'INVALID_YAML');
  });
}

test('BOM/CRLF/空 mapping/字符串 tags 正常解析', () => {
  const document = parseDocumentSync(new Uint8Array([...bytes('\uFEFF---\r\ntags: "a b #mtr/rarely"\r\n---\r\nbody\r\n')]), 'x.md', sha);
  assert.deepEqual(document.tags, ['a', 'b', 'mtr/rarely']);
  assert.equal(document.classification, 'rarely');
  assert.equal(parseDocumentSync(bytes('---\n---\nbody'), 'empty.md', sha).needs_classification, true);
});

test('标签数组、冲突、负边界与 Unicode lookbehind', () => {
  const document = parseDocumentSync(bytes("---\ntags: ['#z', mtr/trial]\n---\n#mtr/rarely\n"), 'x.md', sha);
  assert.deepEqual(document.tags, ['mtr/rarely', 'mtr/trial', 'z']);
  assert.equal(document.classification, 'conflict');
  assert.equal(document.reason_code, 'MTR_TAG_CONFLICT');
  assert.deepEqual(inlineTagsOutsideCode(['x#mtr/trial #mtr/trialx #mtr/trial-extra\n']), []);
  assert.deepEqual(inlineTagsOutsideCode(['中#mtr/trial (#mtr/rarely)\n']), ['mtr/rarely']);
});

test('围栏/行内代码/缩进中的标签和链接均不计', () => {
  const lines = splitLines('```md\n#mtr/trial [[a]]\n```\n~~~\n#mtr/rarely\n~~~~\n`#mtr/trial [[b]]`\n    #mtr/trial\n\t[[c]]\n#mtr/rarely [[target|label]] [[#heading]]\n');
  assert.deepEqual(inlineTagsOutsideCode(lines), ['mtr/rarely']);
  // 与 review_scan._extract_wiki_links 一致：提取时保留 #heading，解析反链时才剔除空目标。
  assert.deepEqual(extractWikiLinks(lines), ['target', '#heading']);
});

test('详情 frontmatter 保留 Python 数字类型与 JSON 空格', () => {
  const [frontmatter, body] = splitFrontmatter('---\na: 1e3\nb: 1.20\nc: [a, 2]\nd: {x: 1}\n---\nbody', 'ok');
  assert.equal(frontmatter.entries[0].value, '1000.0');
  assert.equal(frontmatter.entries[0].value_type, 'number');
  assert.equal(frontmatter.entries[1].value, '1.2');
  assert.equal(frontmatter.entries[2].value, '["a", 2]');
  assert.equal(frontmatter.entries[3].value, '{"x": 1}');
  assert.equal(body, 'body');
});
