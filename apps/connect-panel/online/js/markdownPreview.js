/**
 * 安全 Markdown 渲染：只输出受控白名单标签，原始 HTML 一律转义。
 * 不引入第三方解析器，避免在审核页面执行笔记里的脚本。
 */
const ESCAPE = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/gu, (ch) => ESCAPE[ch]);
}

function safeHref(raw) {
  const value = String(raw ?? '').trim();
  if (!value) return null;
  // 只允许 http(s)/mailto 与站内相对路径；javascript: data: vbscript: 一律拒绝。
  if (/^(?:https?:|mailto:)/iu.test(value)) return value;
  if (/^[\w./#?=&%-]+$/u.test(value) && !value.startsWith('//')) return value;
  return null;
}

function inline(text) {
  let out = escapeHtml(text);
  // 行内代码优先，避免其内部再被解析成强调或链接。
  const codes = [];
  out = out.replace(/`([^`]+)`/gu, (_, code) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });
  out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/gu, (match, alt, src) => {
    const href = safeHref(src);
    return href ? `<img class="md-image" alt="${escapeHtml(alt)}" src="${escapeHtml(href)}" loading="lazy">` : escapeHtml(match);
  });
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/gu, (match, label, href) => {
    const target = safeHref(href);
    return target
      ? `<a href="${escapeHtml(target)}" rel="noreferrer noopener" target="_blank">${label}</a>`
      : escapeHtml(match);
  });
  out = out.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/gu, (_, target, alias) => {
    const label = alias || target;
    return `<span class="md-wikilink" title="Obsidian 双链：${escapeHtml(target)}">${label}</span>`;
  });
  out = out.replace(/(\*\*|__)(.+?)\1/gu, '<strong>$2</strong>');
  out = out.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/gu, '$1<em>$2</em>');
  out = out.replace(/~~(.+?)~~/gu, '<del>$1</del>');
  out = out.replace(/(^|\s)#([\p{L}\p{N}_/-]{1,64})/gu, '$1<span class="md-tag">#$2</span>');
  return out.replace(/\u0000(\d+)\u0000/gu, (_, index) => `<code>${codes[Number(index)]}</code>`);
}

function splitRow(line) {
  return line.trim().replace(/^\||\|$/gu, '').split('|').map((cell) => cell.trim());
}

function isDivider(line) {
  return /^\s*\|?[\s:-]*-[\s|:-]*\|?\s*$/u.test(line) && line.includes('-');
}

/** 把 Markdown 转成安全 HTML 片段（不含外层容器）。 */
export function renderMarkdown(source) {
  const lines = String(source ?? '').replace(/\r\n?/gu, '\n').split('\n');
  const html = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    // 围栏代码块
    const fence = line.match(/^\s*(```|~~~)\s*([\w+-]*)\s*$/u);
    if (fence) {
      const [, marker, lang] = fence;
      const body = [];
      index += 1;
      while (index < lines.length && !new RegExp(`^\\s*${marker}\\s*$`, 'u').test(lines[index])) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1;
      const cls = lang ? ` class="language-${escapeHtml(lang)}"` : '';
      html.push(`<pre class="md-pre"><code${cls}>${escapeHtml(body.join('\n'))}</code></pre>`);
      continue;
    }

    // 标题
    const heading = line.match(/^(#{1,6})\s+(.*)$/u);
    if (heading) {
      const level = heading[1].length;
      html.push(`<h${level} class="md-h${level}">${inline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    // 分隔线
    if (/^\s*([-*_])(\s*\1){2,}\s*$/u.test(line)) {
      html.push('<hr class="md-hr">');
      index += 1;
      continue;
    }

    // 引用
    if (/^\s*>/u.test(line)) {
      const body = [];
      while (index < lines.length && /^\s*>/u.test(lines[index])) {
        body.push(lines[index].replace(/^\s*>\s?/u, ''));
        index += 1;
      }
      html.push(`<blockquote class="md-quote">${renderMarkdown(body.join('\n'))}</blockquote>`);
      continue;
    }

    // 表格：表头 + 分隔行
    if (line.includes('|') && index + 1 < lines.length && isDivider(lines[index + 1])) {
      const head = splitRow(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        rows.push(splitRow(lines[index]));
        index += 1;
      }
      const thead = head.map((cell) => `<th>${inline(cell)}</th>`).join('');
      const tbody = rows
        .map((row) => `<tr>${row.map((cell) => `<td>${inline(cell)}</td>`).join('')}</tr>`)
        .join('');
      html.push(`<table class="md-table"><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table>`);
      continue;
    }

    // 有序 / 无序列表
    const bullet = line.match(/^(\s*)([-*+])\s+(.*)$/u);
    const ordered = line.match(/^(\s*)(\d+)[.)]\s+(.*)$/u);
    if (bullet || ordered) {
      const tag = bullet ? 'ul' : 'ol';
      const items = [];
      const indent = (bullet ?? ordered)[1].length;
      while (index < lines.length) {
        const m = bullet ? lines[index].match(/^(\s*)([-*+])\s+(.*)$/u) : lines[index].match(/^(\s*)(\d+)[.)]\s+(.*)$/u);
        if (!m || m[1].length !== indent) break;
        items.push(`<li>${inline(m[3])}</li>`);
        index += 1;
      }
      html.push(`<${tag} class="md-list">${items.join('')}</${tag}>`);
      continue;
    }

    // 空行
    if (!line.trim()) {
      index += 1;
      continue;
    }

    // 段落：连续非空行合并
    const para = [];
    while (index < lines.length && lines[index].trim() && !/^\s*(#{1,6}\s|>|```|~~~|[-*+]\s|\d+[.)]\s)/u.test(lines[index])) {
      para.push(lines[index]);
      index += 1;
    }
    html.push(`<p class="md-p">${inline(para.join(' '))}</p>`);
  }
  return html.join('\n');
}

/** frontmatter 单独展示，正文只渲染 body。 */
export function splitFrontmatterSafe(text) {
  const normalized = String(text ?? '').replace(/^\uFEFF/u, '');
  const match = normalized.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/u);
  if (!match) return { frontmatterText: '', body: normalized };
  return { frontmatterText: match[1], body: normalized.slice(match[0].length) };
}

export function renderNotePreview(rawText) {
  const { frontmatterText, body } = splitFrontmatterSafe(rawText);
  return {
    frontmatterText,
    html: renderMarkdown(body || rawText),
  };
}
