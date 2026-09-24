// Minimal, escape-first Markdown renderer for model answers. Model output is untrusted:
// everything is HTML-escaped before any markup is added, links are never followed as URLs,
// and file references (src/app.py:42, /abs/path/notes.md#page-3) become open-in-editor links.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.FlintRender = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Absolute paths to text files (MIT corpus pages contain spaces), or relative paths with a line.
  var ABS = /(\/(?:Users|home|private|tmp|var)\/[^\n`'"<>()\[\]]*?\.(?:md|py|js|ts|tsx|jsx|txt|json|go|rs|java|rb|c|h|cpp|html|css|sh))(#page-(\d+))?/g;
  var REL = /(^|[\s(\[`'"])((?:[\w.\-]+\/)*[\w.\-]+\.[A-Za-z][A-Za-z0-9]{0,7}):(\d+)(?:-(\d+))?/g;

  function linkFiles(html) {
    // Runs on escaped text outside tags; paths never contain escaped characters we produce.
    html = html.replace(ABS, function (m, p, anchor, page) {
      return '<a href="#" class="file" data-path="' + p + '"' + (page ? ' data-page="' + page + '"' : '') + '>' + m + '</a>';
    });
    return html.replace(REL, function (m, pre, p, line, end) {
      return pre + '<a href="#" class="file" data-path="' + p + '" data-line="' + line + '">' + p + ':' + line + (end ? '-' + end : '') + '</a>';
    });
  }

  function inline(text) {
    var codes = [];
    var s = esc(text).replace(/`([^`\n]+)`/g, function (m, c) {
      codes.push(c);
      return '\u0000' + (codes.length - 1) + '\u0000';
    });
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
         .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>')
         .replace(/\[([^\]\n]+)\]\(([^)\n]+)\)/g, function (m, label, target) {
           // Keep the target visible as text; file targets become editor links below.
           return label + ' (' + target + ')';
         });
    s = linkFiles(s);
    return s.replace(/\u0000(\d+)\u0000/g, function (m, i) {
      return '<code>' + linkFiles(codes[+i]) + '</code>';
    });
  }

  function render(md) {
    var lines = String(md == null ? '' : md).replace(/\r\n?/g, '\n').split('\n');
    var out = [], para = [], list = null, i = 0;
    function flushPara() {
      if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; }
    }
    function flushList() {
      if (list) { out.push('<' + list.tag + '>' + list.items.map(function (it) { return '<li>' + inline(it) + '</li>'; }).join('') + '</' + list.tag + '>'); list = null; }
    }
    while (i < lines.length) {
      var line = lines[i];
      var fence = line.match(/^\s*(```|~~~)\s*([\w+#.\-]*)\s*$/);
      if (fence) {
        flushPara(); flushList();
        var body = [], j = i + 1;
        while (j < lines.length && !/^\s*(```|~~~)\s*$/.test(lines[j])) { body.push(lines[j]); j++; }
        out.push('<pre><code' + (fence[2] ? ' data-lang="' + esc(fence[2]) + '"' : '') + '>' + esc(body.join('\n')) + '</code></pre>');
        i = j + 1;
        continue;
      }
      var h = line.match(/^(#{1,6})\s+(.*)$/);
      var bullet = line.match(/^\s*[-*+]\s+(.*)$/);
      var numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (h) {
        flushPara(); flushList();
        var level = Math.min(6, h[1].length + 2);
        out.push('<h' + level + '>' + inline(h[2]) + '</h' + level + '>');
      } else if (bullet || numbered) {
        flushPara();
        var tag = bullet ? 'ul' : 'ol';
        if (!list || list.tag !== tag) { flushList(); list = { tag: tag, items: [] }; }
        list.items.push((bullet || numbered)[1]);
      } else if (/^\s*\|.*\|\s*$/.test(line)) {
        flushPara(); flushList();
        var rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(lines[i]); i++; }
        out.push('<pre class="table">' + esc(rows.join('\n')) + '</pre>');
        continue;
      } else if (/^\s*>\s?/.test(line)) {
        flushPara(); flushList();
        out.push('<blockquote>' + inline(line.replace(/^\s*>\s?/, '')) + '</blockquote>');
      } else if (!line.trim()) {
        flushPara(); flushList();
      } else {
        if (list && /^\s{2,}\S/.test(line)) { list.items[list.items.length - 1] += ' ' + line.trim(); }
        else { flushList(); para.push(line); }
      }
      i++;
    }
    flushPara(); flushList();
    return out.join('\n');
  }

  return { render: render, esc: esc, inline: inline };
});
