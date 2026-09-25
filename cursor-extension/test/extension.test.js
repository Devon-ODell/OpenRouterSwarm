// Offline checks for the Flint Swarm extension: manifest/code consistency, helpers, the
// renderer's escaping, the webview's CSP and message flow, and one real bridge round trip.
// Run: node cursor-extension/test/extension.test.js
'use strict';
const assert = require('assert');
const Module = require('module');
const path = require('path');
const fs = require('fs');

const EXT = path.resolve(__dirname, '..');
const manifest = JSON.parse(fs.readFileSync(path.join(EXT, 'package.json'), 'utf8'));
const registered = {};
let provider = null;
const disposable = { dispose() {} };

class Range {
  constructor(a, b, c, d) { this.start = { line: a, character: b }; this.end = { line: c, character: d }; }
  contains(p) { return p.line >= this.start.line && p.line <= this.end.line; }
  get isEmpty() { return this.start.line === this.end.line && this.start.character === this.end.character; }
}
const vscode = {
  workspace: {
    getConfiguration: () => ({ get: (k) => (manifest.contributes.configuration.properties['flintSwarm.' + k] || {}).default }),
    getWorkspaceFolder: () => undefined, workspaceFolders: undefined,
  },
  window: {
    activeTextEditor: undefined,
    createStatusBarItem: () => ({ show() {}, dispose() {} }),
    registerWebviewViewProvider: (id, p) => { assert.strictEqual(id, 'flintSwarm.panel'); provider = p; return disposable; },
    onDidChangeActiveTextEditor: () => disposable,
    showWarningMessage: () => Promise.resolve(undefined),
  },
  commands: { registerCommand: (id, fn) => { registered[id] = fn; return disposable; }, executeCommand: () => Promise.resolve() },
  Uri: { joinPath: (base, ...parts) => ({ fsPath: path.join(base.fsPath, ...parts), toString() { return 'vscode-resource:' + this.fsPath; } }) },
  StatusBarAlignment: { Left: 1 }, ProgressLocation: { Notification: 15 }, ViewColumn: { One: 1 },
  SymbolKind: { 5: 'Class', 11: 'Function', Class: 5, Function: 11 }, Range, Position: class { constructor(l, c) { this.line = l; this.character = c; } },
};
const load = Module._load;
Module._load = function (request) { return request === 'vscode' ? vscode : load.apply(this, arguments); };
const ext = require(path.join(EXT, 'extension.js'));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('every contributed command is registered, and menus only use contributed commands', () => {
  ext.activate({ subscriptions: [], extensionUri: { fsPath: EXT } });
  const declared = manifest.contributes.commands.map((c) => c.command).sort();
  assert.deepStrictEqual(Object.keys(registered).sort(), declared);
  const used = [...Object.values(manifest.contributes.menus).flat(), ...manifest.contributes.keybindings].map((m) => m.command);
  used.forEach((c) => assert.ok(declared.includes(c), c));
  assert.ok(fs.existsSync(path.join(EXT, manifest.contributes.viewsContainers.activitybar[0].icon)));
});

test('JSON lines are parsed across chunk boundaries', () => {
  const got = [];
  let rest = ext._test.parseLines('', '{"a":1}\n{"b":', (o) => got.push(o));
  rest = ext._test.parseLines(rest, '2}\nnot json\n', (o) => got.push(o));
  assert.deepStrictEqual(got, [{ a: 1 }, { b: 2 }, { event: 'log', text: 'not json' }]);
  assert.strictEqual(rest, '');
});

test('the innermost symbol under the cursor is chosen', () => {
  const method = { name: 'm', kind: 11, range: new Range(5, 0, 9, 0), children: [] };
  const cls = { name: 'C', kind: 5, range: new Range(1, 0, 20, 0), children: [method] };
  assert.strictEqual(ext._test.innermost([cls], { line: 6 }), method);
  assert.strictEqual(ext._test.innermost([cls], { line: 2 }), cls);
  assert.strictEqual(ext._test.innermost([cls], { line: 30 }), null);
});

test('the flint checkout is found next to the extension', () => {
  assert.strictEqual(ext._test.flintRoot(), path.resolve(EXT, '..'));
});

test('the renderer escapes model output and links file references', () => {
  const R = require(path.join(EXT, 'media', 'render.js'));
  const html = R.render('<img src=x onerror=alert(1)> see `app.py:12` and [x](javascript:alert(1))');
  assert.ok(!/<img/i.test(html) && html.includes('&lt;img'));
  assert.ok(!html.includes('href="javascript'));
  assert.ok(html.includes('data-path="app.py" data-line="12"'));
  assert.ok(R.render('```\n<b>\n```').includes('<pre><code>&lt;b&gt;</code></pre>'));
});

test('the webview has a nonce CSP and no inline handlers', () => {
  const posted = [];
  const view = { visible: true, show() {}, onDidChangeVisibility: () => disposable,
    webview: { options: null, cspSource: 'vscode-resource:', asWebviewUri: (u) => u, postMessage: (m) => posted.push(m), onDidReceiveMessage: () => disposable } };
  provider.resolveWebviewView(view);
  const html = view.webview.html;
  const nonce = html.match(/'nonce-([^']+)'/)[1];
  assert.strictEqual(html.split(`nonce="${nonce}"`).length - 1, 2);
  assert.ok(html.includes("default-src 'none'"));
  assert.ok(!/\son\w+=/.test(html));
  provider.post({ type: 'status', data: {} });
  assert.strictEqual(posted.length, 0, 'messages wait for the ready handshake');
});

test('the panel script renders a streamed ask with the merged answer first', () => {
  const els = {};
  const make = (tag) => {
    const e = { tag, children: [], dataset: {}, className: '', textContent: '', value: '', checked: true,
      disabled: false, hidden: false, attrs: {},
      appendChild(c) { this.children.push(c); }, addEventListener() {}, focus() {}, requestSubmit() {},
      setAttribute(k, v) { this.attrs[k] = v; },
      classList: { toggle() {}, add() {}, remove() {} },
      text() { return this.innerHTML + this.children.map((c) => c.text()).join(''); } };
    let html = '';   // like the DOM: assigning innerHTML replaces the children
    Object.defineProperty(e, 'innerHTML', { get: () => html, set: (v) => { html = v; e.children = []; } });
    return e;
  };
  const handlers = {};
  global.document = { getElementById: (id) => (els[id] = els[id] || make(id)), createElement: make,
    addEventListener: (t, fn) => { handlers['doc:' + t] = fn; } };
  global.window = { FlintRender: require(path.join(EXT, 'media', 'render.js')), addEventListener: (t, fn) => { handlers[t] = fn; } };
  const sent = [];
  global.acquireVsCodeApi = () => ({ postMessage: (m) => sent.push(m), getState: () => null, setState() {} });
  const origBox = els.threads;
  require(path.join(EXT, 'media', 'panel.js'));
  assert.deepStrictEqual(sent, [{ type: 'ready' }]);
  const send = (data) => handlers.message({ data });
  send({ type: 'askStart', id: 'q1', question: 'Find bugs', where: 'app.py:1-9', repo: '/r' });
  send({ type: 'askEvent', id: 'q1', event: { event: 'start', model: 'a:free' } });
  send({ type: 'askEvent', id: 'q1', event: { event: 'progress', model: 'a:free', text: 'tool: study' } });
  assert.ok(els.threads.text().includes('tool: study'));
  send({ type: 'askEvent', id: 'q1', event: { event: 'answer', model: 'a:free', text: 'Bug at `app.py:3`', secs: 9, study_calls: 2 } });
  send({ type: 'askEvent', id: 'q1', event: { event: 'start', model: 'b:free', role: 'synthesis' } });
  send({ type: 'askEvent', id: 'q1', event: { event: 'synthesis', model: 'b:free', role: 'synthesis', text: 'Merged: **one bug**' } });
  send({ type: 'askEvent', id: 'q1', event: { event: 'done', answered: 1, asked: 1, requests: 5 } });
  const out = els.threads.text();
  assert.ok(out.indexOf('Merged:') < out.indexOf('Bug at'), 'merged answer comes first');
  assert.ok(out.includes('2 MIT lookups'));
  assert.ok(out.includes('data-vote="1" data-model="a:free"'));
  assert.ok(out.includes('1/1 models answered'));
  send({ type: 'study', id: 's1', query: 'dijkstra', hits: [{ kind: 'card', course: '6.006', lecture: 'L13', page: null, path: '/x/cards.md', text: 'relax edges' }] });
  assert.ok(els.threads.text().includes('MIT lookup: dijkstra'));
  void origBox;
  panelHarness = { els, handlers, sent, send };
});

// The queue tab is driven entirely by the status message; these run against the same loaded
// panel.js, so they see exactly what the sidebar would render.
let panelHarness = null;

const QUEUE_STATUS = {
  repo: '/r/demo', is_target: true, daemon_running: true, landed: 2, max_queue: 20,
  kinds: ['feature', 'bugfix', 'test', 'refactor'], recent: [], budget: null, experiment: null,
  wallet: { cap: 5, spent: 0.1234, remaining: 4.8766, calls: 3 },
  queue: [
    { id: 't1', title: 'Handle empty input', kind: 'bugfix', priority: 2, attempts: 1, claimed: false,
      origin: 'cursor', not_before: 0, created: 10, acceptance: ['parse([]) returns []'], depends_on: [],
      blocks: [{ id: 't3', title: 'Write tests' }], detail: 'the detail', detail_truncated: false },
    { id: 't2', title: 'Verbose flag', kind: 'feature', priority: 1, attempts: 0, claimed: true,
      origin: 'planner', not_before: 0, created: 11, acceptance: [], depends_on: [], blocks: [], detail: '' },
    { id: 't3', title: 'Write tests', kind: 'test', priority: 0, attempts: 0, claimed: false,
      origin: 'planner', not_before: 0, created: 12, acceptance: [], depends_on: ['t1'], blocks: [], detail: '' },
  ],
};

/** A click whose target only has to answer closest(), which is all the handler asks it. */
const hit = (map) => ({ target: { closest: (sel) => map[sel] || null } });

test('the queue tab lists every task with its state and a remove control', () => {
  const { els, send } = panelHarness;
  send({ type: 'status', data: QUEUE_STATUS });
  send({ type: 'tab', tab: 'queue' });
  const out = els.queueList.text();
  assert.ok(out.includes('Handle empty input') && out.includes('Verbose flag') && out.includes('Write tests'));
  assert.ok(out.includes('class="icon qdel"') && out.includes('class="icon qedit"'), 'each row has ✕ and ✎');
  assert.ok(out.includes('running now'), 'the claimed task says a worker has it');
  assert.ok(out.includes('blocks 1') && out.includes('waits for 1'), 'dependencies are visible');
  assert.ok(els.queueHead.text().includes('3 of 20 queued'));
  assert.strictEqual(els.qcount.textContent, '3');
  assert.ok(els.status.text().includes('$0.1234'), 'the paid budget is on show');
  // the claimed task is listed first, then by priority
  assert.ok(out.indexOf('Verbose flag') < out.indexOf('Handle empty input'));
  assert.ok(out.indexOf('Handle empty input') < out.indexOf('Write tests'));
});

test('✕ asks to remove that task and names the tasks that depend on it', () => {
  const { handlers, sent } = panelHarness;
  sent.length = 0;
  handlers['doc:click'](hit({ '.qdel': {}, '.qrow': { dataset: { id: 't1' } } }));
  assert.deepStrictEqual(sent, [{ type: 'queueRemove', id: 't1', repo: '/r/demo', title: 'Handle empty input',
    claimed: false, blocks: [{ id: 't3', title: 'Write tests' }] }]);
  sent.length = 0;
  handlers['doc:click'](hit({ '.qdel': {}, '.qrow': { dataset: { id: 't2' } } }));
  assert.strictEqual(sent[0].claimed, true, 'a task being worked on is flagged, so the extension confirms');
});

test('✎ fetches the whole task, and saving posts the rewritten fields', () => {
  const { els, handlers, sent, send } = panelHarness;
  sent.length = 0;
  handlers['doc:click'](hit({ '.qedit': {}, '.qrow': { dataset: { id: 't1' } } }));
  assert.deepStrictEqual(sent, [{ type: 'queueGet', id: 't1', repo: '/r/demo' }]);
  send({ type: 'queueTask', task: { id: 't1', title: 'Handle empty input', kind: 'bugfix', priority: 2,
    detail: 'the whole detail', acceptance: ['parse([]) returns []', 'a test proves it'] } });
  const form = els.queueList.text();
  assert.ok(form.includes('the whole detail'), 'the form gets the full detail, not the trimmed one');
  assert.ok(form.includes('parse([]) returns []\na test proves it'), 'acceptance is one per line');
  assert.ok(form.includes('<option value="bugfix" selected>') && form.includes('<option value="2" selected>'));

  sent.length = 0;
  const field = (v) => ({ value: v });
  handlers['doc:submit']({ preventDefault() {}, target: { closest: () => ({
    dataset: { id: 't1' },
    querySelector: (cls) => ({ '.f-title': field(' Handle empty input '), '.f-detail': field('rewritten'),
      '.f-accept': field('first\n\n  second  '), '.f-kind': field('test'), '.f-prio': field('0') }[cls]),
  }) } });
  assert.deepStrictEqual(sent, [{ type: 'queueEdit', id: 't1', repo: '/r/demo', title: 'Handle empty input',
    detail: 'rewritten', kind: 'test', priority: 0, acceptance: ['first', 'second'] }]);
  send({ type: 'queueError', id: 't1', error: 'another task already has that title' });
  assert.ok(els.queueList.text().includes('another task already has that title'), 'a refused edit is shown in the form');
  send({ type: 'queueSaved', task: { id: 't1' } });
  assert.ok(els.queueList.text().includes('class="icon qdel"'), 'saving closes the form');
});

test('Clear all and the budget pencil reach the extension', () => {
  const { handlers, sent } = panelHarness;
  sent.length = 0;
  handlers['doc:click'](hit({ '#qclear': {} }));
  handlers['doc:click'](hit({ '#budget': {} }));
  assert.deepStrictEqual(sent, [{ type: 'queueClear', repo: '/r/demo' }, { type: 'budget' }]);
});

test('the row controls are revealed by hover and by keyboard focus', () => {
  const css = fs.readFileSync(path.join(EXT, 'media', 'panel.css'), 'utf8');
  assert.ok(/\.qactions\s*{[^}]*visibility:\s*hidden/.test(css));
  assert.ok(css.includes('.qrow:hover .qactions') && css.includes('.qrow:focus-within .qactions'));
});

test('a real bridge call returns MIT hits with absolute paths', async () => {
  const db = (process.env.FLINT_CORPUS_DB || path.join(require('os').homedir(), '.flint', 'corpus.db'));
  if (!fs.existsSync(db)) { console.log('skip  (no study corpus at ' + db + ')'); return; }
  const res = await ext._test.bridgeJson(['study', '--query', 'dijkstra priority queue', '-k', '2']);
  assert.ok(res.hits.length >= 1);
  assert.ok(path.isAbsolute(res.hits[0].path) && fs.existsSync(res.hits[0].path), res.hits[0].path);
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log('ok  ', name); } catch (e) { failed++; console.log('FAIL', name, '\n    ', e && e.stack || e); }
  }
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
