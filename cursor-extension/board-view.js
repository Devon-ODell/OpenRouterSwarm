'use strict';
const crypto = require('crypto');
const { esc } = require('./media/render.js');
function html(data, completedOnly = false) {
  const nonce = crypto.randomBytes(16).toString('base64');
  const cards = data.cards.filter(c => !completedOnly || c.status === 'Done');
  const columns = completedOnly ? ['Done'] : data.columns;
  const card = c => `<article><h3>${esc(c.title)}</h3><p>${esc(c.packet || c.id)} · ${esc(c.origin || '')}</p>
    <p>${esc(c.reason || '')}</p>${c.commit ? `<p>Commit <code>${esc(c.commit)}</code></p>` : ''}
    ${c.review_summary ? `<p>${esc(c.review_summary)}</p>` : ''}
    ${c.commands ? `<p>Passing checks: ${esc(c.commands.join('; '))}</p>` : ''}
    ${c.files ? `<p>Changed: ${esc(c.files.join(', '))}</p>` : ''}
    ${c.evidence ? `<p>Evidence: <code>${esc(c.evidence)}</code></p>` : ''}
    ${c.criteria ? `<details><summary>Acceptance criteria</summary><ul>${c.criteria.map(x => `<li>${esc(x.text)}</li>`).join('')}</ul></details>` : ''}</article>`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style nonce="${nonce}">body{font:14px system-ui;color:var(--vscode-foreground);background:var(--vscode-editor-background);padding:18px}button{padding:8px;margin-right:8px;cursor:pointer}main{display:flex;gap:16px;overflow:auto}section{min-width:290px;flex:1}article{padding:14px;margin:12px 0;border:1px solid var(--vscode-panel-border,#777);border-radius:8px}h3{font-size:15px}p,code{overflow-wrap:anywhere}code{font-size:12px}</style></head><body>
  <h1>${completedOnly ? 'Verified completed' : 'Swarm scrum board'}</h1><p>${esc(data.repo)}</p>
  <button id="refresh">Refresh evidence</button><button id="toggle">${completedOnly ? 'Show scrum board' : 'Verified completed'}</button>
  <p>All recorded task history and the committed roadmap; these are not shift totals.</p><p>Snapshot: ${esc(new Date(data.as_of * 1000).toLocaleString())}. ${esc(data.notice)}</p>
  <p>“Landed” means integrated into swarm/trunk. Deployment is separate.</p>
  ${data.stale_workers.length ? '<p>Worker status still references closed tasks; those tasks are not counted as In Progress.</p>' : ''}
  <main>${columns.map(s => `<section><h2>${esc(s)} (${cards.filter(c => c.status === s).length})</h2>${cards.filter(c => c.status === s).map(card).join('') || '<p>No tasks in this column.</p>'}</section>`).join('')}</main>
  <script nonce="${nonce}">const vscode=acquireVsCodeApi();document.getElementById('refresh').onclick=()=>vscode.postMessage({type:'refresh'});document.getElementById('toggle').onclick=()=>vscode.postMessage({type:'toggle'});</script></body></html>`;
}
module.exports = { html };
