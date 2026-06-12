/**
 * markdown.js
 * ───────────
 * Minimal, dependency-free Markdown → HTML renderer for agent answers.
 * Escapes HTML first to avoid injection, then applies a safe subset:
 * code fences, inline code, bold, italic, headings, hr, lists, paragraphs.
 */

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

export function renderMarkdown(src) {
  if (!src) return '';
  let text = String(src);

  // Extract fenced code blocks first so their contents are not further parsed.
  const codeBlocks = [];
  text = text.replace(/```(?:[\w-]+)?\n?([\s\S]*?)```/g, (_, code) => {
    codeBlocks.push(code.replace(/\n$/, ''));
    return `\u0000CODE${codeBlocks.length - 1}\u0000`;
  });

  text = escapeHtml(text);

  // Inline code
  text = text.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
  // Bold then italic
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');

  // Block-level: split into lines and assemble lists / headings / paragraphs.
  const lines = text.split('\n');
  const out = [];
  let listType = null; // 'ul' | 'ol'
  let para = [];

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.join('<br>')}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (listType) {
      out.push(`</${listType}>`);
      listType = null;
    }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const trimmed = line.trim();

    if (trimmed === '') {
      flushPara();
      flushList();
      continue;
    }
    if (/^###\s+/.test(trimmed)) {
      flushPara();
      flushList();
      out.push(`<h3>${trimmed.replace(/^###\s+/, '')}</h3>`);
      continue;
    }
    if (/^---$/.test(trimmed)) {
      flushPara();
      flushList();
      out.push('<hr>');
      continue;
    }
    const ol = trimmed.match(/^\d+\.\s+(.*)$/);
    const ul = trimmed.match(/^[-*]\s+(.*)$/);
    if (ol) {
      flushPara();
      if (listType !== 'ol') {
        flushList();
        out.push('<ol>');
        listType = 'ol';
      }
      out.push(`<li>${ol[1]}</li>`);
      continue;
    }
    if (ul) {
      flushPara();
      if (listType !== 'ul') {
        flushList();
        out.push('<ul>');
        listType = 'ul';
      }
      out.push(`<li>${ul[1]}</li>`);
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();

  let html = out.join('');

  // Restore code blocks.
  html = html.replace(/\u0000CODE(\d+)\u0000/g, (_, i) => `<pre><code>${escapeHtml(codeBlocks[+i])}</code></pre>`);

  return html;
}
