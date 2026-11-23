import test from 'node:test';
import assert from 'node:assert/strict';
import { deflateRawSync } from 'node:zlib';

import {
  PREVIEW_KIND,
  blobUrl as _blobUrl,
  describeDocx,
  describeXlsx,
  detectKind,
  extensionOf,
  formatSize,
  readZipEntries,
} from '../../js/filePreview.js';

/** 构造一个最小合法 ZIP（仅存储/压缩两种方式），用于验证目录解析。 */
function buildZip(files) {
  const chunks = [];
  const central = [];
  let offset = 0;
  for (const { name, content, deflate = false } of files) {
    const nameBytes = Buffer.from(name, 'utf8');
    const data = Buffer.from(content, 'utf8');
    const stored = deflate ? deflateRawSync(data) : data;
    const method = deflate ? 8 : 0;
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(method, 8);
    local.writeUInt32LE(0, 14); // crc 省略，解析器不校验
    local.writeUInt32LE(stored.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    local.writeUInt16LE(0, 28);
    chunks.push(local, nameBytes, stored);

    const entry = Buffer.alloc(46);
    entry.writeUInt32LE(0x02014b50, 0);
    entry.writeUInt16LE(20, 4);
    entry.writeUInt16LE(20, 6);
    entry.writeUInt16LE(method, 10);
    entry.writeUInt32LE(stored.length, 20);
    entry.writeUInt32LE(data.length, 24);
    entry.writeUInt16LE(nameBytes.length, 28);
    entry.writeUInt32LE(offset, 42);
    central.push(Buffer.concat([entry, nameBytes]));

    offset += local.length + nameBytes.length + stored.length;
  }
  const centralBuf = Buffer.concat(central);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(files.length, 8);
  eocd.writeUInt16LE(files.length, 10);
  eocd.writeUInt32LE(centralBuf.length, 12);
  eocd.writeUInt32LE(offset, 16);
  return new Uint8Array(Buffer.concat([...chunks, centralBuf, eocd]));
}

test('按扩展名识别预览类型', () => {
  assert.equal(detectKind('a/b.md'), PREVIEW_KIND.markdown);
  assert.equal(detectKind('报告.PDF'), PREVIEW_KIND.pdf);
  assert.equal(detectKind('截图.PNG'), PREVIEW_KIND.image);
  assert.equal(detectKind('方案.docx'), PREVIEW_KIND.docx);
  assert.equal(detectKind('台账.xlsx'), PREVIEW_KIND.xlsx);
  assert.equal(detectKind('旧版.doc'), PREVIEW_KIND.binary);
  assert.equal(detectKind('未知'), PREVIEW_KIND.binary);
});

test('扩展名与大小格式化', () => {
  assert.equal(extensionOf('a/b/c.PDF'), 'pdf');
  assert.equal(extensionOf('无扩展名'), '');
  assert.equal(formatSize(512), '512 B');
  assert.equal(formatSize(2048), '2.0 KB');
  assert.equal(formatSize(3 * 1024 * 1024), '3.00 MB');
  assert.equal(formatSize(NaN), '未知');
});

test('ZIP 目录可解析出条目名', () => {
  const zip = buildZip([
    { name: 'word/document.xml', content: '<w:document/>' },
    { name: 'xl/workbook.xml', content: '<workbook/>', deflate: true },
  ]);
  const entries = readZipEntries(zip);
  assert.ok(entries);
  assert.deepEqual(entries.map((e) => e.name).sort(), ['word/document.xml', 'xl/workbook.xml']);
});

test('DOCX 按段落提取文本', async () => {
  const xml = '<w:document><w:body>'
    + '<w:p><w:r><w:t>第一段</w:t></w:r></w:p>'
    + '<w:p><w:r><w:t>第二</w:t></w:r><w:r><w:t>段</w:t></w:r></w:p>'
    + '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格内文字</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    + '</w:body></w:document>';
  const zip = buildZip([{ name: 'word/document.xml', content: xml, deflate: true }]);
  const out = await describeDocx(zip);
  assert.equal(out.ok, true);
  assert.ok(out.paragraphs.includes('第一段'));
  assert.ok(out.paragraphs.includes('第二段'), '同段多个 run 应拼接');
  assert.ok(out.paragraphs.includes('表格内文字'));
  assert.equal(out.tables, 1);
});

test('DOCX 结构异常时明确失败，不返回空成功', async () => {
  const zip = buildZip([{ name: 'other.xml', content: '<x/>' }]);
  const out = await describeDocx(zip);
  assert.equal(out.ok, false);
  assert.match(out.message, /无法读取/u);
});

test('XLSX 渲染行列与共享字符串', async () => {
  const shared = '<sst><si><t>姓名</t></si><si><t>年龄</t></si><si><t>张三</t></si></sst>';
  const sheet = '<worksheet><sheetData>'
    + '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
    + '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>30</v></c></row>'
    + '</sheetData></worksheet>';
  const workbook = '<workbook><sheets><sheet name="人员" sheetId="1"/></sheets></workbook>';
  const zip = buildZip([
    { name: 'xl/sharedStrings.xml', content: shared, deflate: true },
    { name: 'xl/workbook.xml', content: workbook },
    { name: 'xl/worksheets/sheet1.xml', content: sheet, deflate: true },
  ]);
  const out = await describeXlsx(zip);
  assert.equal(out.ok, true);
  assert.equal(out.sheets.length, 1);
  assert.equal(out.sheets[0].name, '人员');
  assert.deepEqual(out.sheets[0].rows[0], ['姓名', '年龄']);
  assert.deepEqual(out.sheets[0].rows[1], ['张三', '30']);
});

test('XLSX 空单元格保留位置，不左移错位', async () => {
  const sheet = '<worksheet><sheetData>'
    + '<row r="1"><c r="A1"><v>1</v></c><c r="C1"><v>3</v></c></row>'
    + '</sheetData></worksheet>';
  const zip = buildZip([
    { name: 'xl/workbook.xml', content: '<workbook><sheets><sheet name="S"/></sheets></workbook>' },
    { name: 'xl/worksheets/sheet1.xml', content: sheet, deflate: true },
  ]);
  const out = await describeXlsx(zip);
  assert.deepEqual(out.sheets[0].rows[0], ['1', '', '3']);
});

test('超过 maxRows 时标记截断', async () => {
  const rows = Array.from({ length: 5 }, (_, i) => `<row r="${i + 1}"><c r="A${i + 1}"><v>${i}</v></c></row>`).join('');
  const zip = buildZip([
    { name: 'xl/workbook.xml', content: '<workbook/>' },
    { name: 'xl/worksheets/sheet1.xml', content: `<worksheet><sheetData>${rows}</sheetData></worksheet>`, deflate: true },
  ]);
  const out = await describeXlsx(zip, { maxRows: 2 });
  assert.equal(out.sheets[0].rows.length, 2);
  assert.equal(out.sheets[0].truncated, true);
});
