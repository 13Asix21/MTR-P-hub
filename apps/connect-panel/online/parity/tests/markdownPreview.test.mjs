import test from 'node:test';
import assert from 'node:assert/strict';

import { escapeHtml, renderMarkdown, renderNotePreview, splitFrontmatterSafe } from '../../js/markdownPreview.js';

test('原始 HTML 被转义，不会执行脚本', () => {
  const html = renderMarkdown('<script>alert(1)</script>\n\n<div onclick="x">hi</div>');
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<div'), false);
  assert.equal(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'), true);
  assert.equal(html.includes('&lt;div onclick=&quot;x&quot;&gt;'), true);
});

test('危险协议链接被拒绝，安全链接保留', () => {
  const js = renderMarkdown('[a](javascript:alert(1))');
  assert.equal(js.includes('<a '), false, 'javascript: 不应生成链接');
  assert.equal(js.includes('href='), false);
  const data = renderMarkdown('[a](data:text/html;base64,xx)');
  assert.equal(data.includes('<a '), false, 'data: 不应生成链接');
  const ok = renderMarkdown('[站内](2-Permanent/a.md)');
  assert.match(ok, /<a href="2-Permanent\/a\.md"/u);
});

test('标题、列表、引用、分隔线、代码块', () => {
  const html = renderMarkdown('# 标题\n\n- 一\n- 二\n\n> 引用\n\n---\n\n```js\nconst a = 1 < 2;\n```');
  assert.match(html, /<h1 class="md-h1">标题<\/h1>/u);
  assert.match(html, /<ul class="md-list"><li>一<\/li><li>二<\/li><\/ul>/u);
  assert.match(html, /<blockquote class="md-quote"><p class="md-p">引用<\/p><\/blockquote>/u);
  assert.match(html, /<hr class="md-hr">/u);
  assert.match(html, /<pre class="md-pre"><code class="language-js">const a = 1 &lt; 2;<\/code><\/pre>/u);
});

test('表格按表头与分隔行渲染', () => {
  const html = renderMarkdown('| 项 | 值 |\n| --- | --- |\n| a | 1 |');
  assert.match(html, /<th>项<\/th><th>值<\/th>/u);
  assert.match(html, /<td>a<\/td><td>1<\/td>/u);
});

test('有序列表与强调、删除线、行内代码', () => {
  const html = renderMarkdown('1. 第一\n2. 第二\n\n**粗** *斜* ~~删~~ `code`');
  assert.match(html, /<ol class="md-list"><li>第一<\/li><li>第二<\/li><\/ol>/u);
  assert.match(html, /<strong>粗<\/strong>/u);
  assert.match(html, /<em>斜<\/em>/u);
  assert.match(html, /<del>删<\/del>/u);
  assert.match(html, /<code>code<\/code>/u);
});

test('Obsidian 双链与标签保留语义但不出链接跳转', () => {
  const html = renderMarkdown('见 [[目标笔记|别名]] 与 #mtr/trial');
  assert.match(html, /<span class="md-wikilink" title="Obsidian 双链：目标笔记">别名<\/span>/u);
  assert.match(html, /<span class="md-tag">#mtr\/trial<\/span>/u);
});

test('frontmatter 与正文分离', () => {
  const { frontmatterText, body } = splitFrontmatterSafe('---\ntags: [mtr/trial]\n---\n# 正文');
  assert.equal(frontmatterText, 'tags: [mtr/trial]');
  assert.equal(body, '# 正文');
});

test('没有 frontmatter 时整体作为正文', () => {
  const { frontmatterText, body } = splitFrontmatterSafe('# 直接正文');
  assert.equal(frontmatterText, '');
  assert.equal(body, '# 直接正文');
});

test('预览不会二次解析行内代码里的标记', () => {
  const html = renderMarkdown('`**不该加粗**`');
  assert.match(html, /<code>\*\*不该加粗\*\*<\/code>/u);
});

test('escapeHtml 覆盖五个特殊字符', () => {
  assert.equal(escapeHtml(`<&>"'`), '&lt;&amp;&gt;&quot;&#39;');
});

test('renderNotePreview 返回 frontmatter 与 html', () => {
  const out = renderNotePreview('---\ntitle: x\n---\n## 小节');
  assert.equal(out.frontmatterText, 'title: x');
  assert.match(out.html, /<h2 class="md-h2">小节<\/h2>/u);
});
