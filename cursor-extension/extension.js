// Flint Swarm for Cursor and VS Code: point the flint swarm at the code you are working on.
// Everything goes through swarm/bridge.py (JSON over stdout); this file never calls a model itself.
'use strict';
const vscode = require('vscode');
const cp = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

const PRESETS = [
  { label: 'Find bugs in this code', detail: 'Edge cases, wrong assumptions, failing inputs' },
  { label: 'Explain what this code does and why', detail: 'Walk through the logic with file:line references' },
  { label: 'How could this be simpler or faster?', detail: 'Concrete rewrites, with complexity where it matters' },
  { label: 'What tests should this have?', detail: 'Test cases with inputs and expected outputs' },
  { label: 'Which MIT technique applies here?', detail: 'Algorithms, probability, ML or finance methods from the course notes' },
  { label: 'Ask something else…', custom: true },
];

let panel = null;
let statusItem = null;
let lastEditor = null;
let lastInfo = 0;
const running = new Set();

// ---------------------------------------------------------------- locating flint

function cfg() { return vscode.workspace.getConfiguration('flintSwarm'); }

function expandHome(p) { return p ? p.replace(/^~(?=$|\/)/, os.homedir()) : p; }

function flintRoot() {
  const candidates = [expandHome((cfg().get('flintPath') || '').trim()), process.env.FLINT_ROOT];
  try { candidates.push(fs.readFileSync(path.join(__dirname, 'flint-root.txt'), 'utf8').trim()); } catch (_) { /* not packaged */ }
  candidates.push(path.resolve(__dirname, '..'), path.join(os.homedir(), 'Desktop', 'LingAI-Trader'));
  return candidates.find((c) => c && fs.existsSync(path.join(c, 'swarm', 'bridge.py'))) || null;
}

function pythonFor(root) {
  const configured = expandHome((cfg().get('python') || '').trim());
  if (configured) return configured;
  const venv = path.join(root, '.venv', 'bin', 'python');
  return fs.existsSync(venv) ? venv : 'python3';
}

// ---------------------------------------------------------------- bridge

/** Feed stdout text; calls onObject for each complete JSON line; returns the unfinished tail. */
function parseLines(buffer, chunk, onObject) {
  const text = buffer + chunk;
  const lines = text.split('\n');
  const rest = lines.pop();
  for (const line of lines) {
    if (!line.trim()) continue;
    try { onObject(JSON.parse(line)); } catch (_) { onObject({ event: 'log', text: line }); }
  }
  return rest;
}

function runBridge(args, opts = {}) {
  const root = flintRoot();
  if (!root) {
    return Promise.reject(new Error('Cannot find your flint checkout (a folder with swarm/bridge.py). Set "Flint Swarm: Flint Path" in Settings.'));
  }
  return new Promise((resolve, reject) => {
    const child = cp.spawn(pythonFor(root), [path.join(root, 'swarm', 'bridge.py'), ...args],
      { cwd: root, env: Object.assign({}, process.env, { PYTHONUNBUFFERED: '1' }) });
    running.add(child);
    const events = [];
    let buf = '', stderr = '';
    const onObject = (obj) => { events.push(obj); if (opts.onEvent) opts.onEvent(obj); };
    child.stdout.on('data', (d) => { buf = parseLines(buf, d.toString('utf8'), onObject); });
    child.stderr.on('data', (d) => { stderr = (stderr + d.toString('utf8')).slice(-20000); });
    child.on('error', (e) => { running.delete(child); reject(e); });
    child.on('close', (code) => {
      running.delete(child);
      if (buf.trim()) parseLines(buf, '\n', onObject);
      resolve({ code, events, stderr });
    });
    if (opts.token) opts.token.onCancellationRequested(() => child.kill('SIGTERM'));
  });
}

/** The bridge's last object, whether it reports success or failure. */
async function bridgeLast(args) {
  const res = await runBridge(args);
  const last = res.events.filter((e) => e.event !== 'log').pop();
  if (!last) throw new Error(tail(res.stderr) || `bridge exited with code ${res.code}`);
  return last;
}

async function bridgeJson(args) {
  const last = await bridgeLast(args);
  if (last.ok === false || (last.event === 'error' && last.error)) throw new Error(last.error);
  return last;
}

function tail(text, n = 4) { return (text || '').trim().split('\n').slice(-n).join('\n'); }

// ---------------------------------------------------------------- what the user points at

function repoFor(uri) {
  const folder = uri && vscode.workspace.getWorkspaceFolder(uri);
  const dir = uri && uri.scheme === 'file' ? path.dirname(uri.fsPath) : folder && folder.uri.fsPath;
  if (dir) {
    try {
      return cp.execFileSync('git', ['-C', dir, 'rev-parse', '--show-toplevel'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
    } catch (_) { /* not a git repository */ }
  }
  if (folder) return folder.uri.fsPath;
  const first = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
  return first ? first.uri.fsPath : null;
}

function currentEditor() {
  const e = vscode.window.activeTextEditor;
  return e && e.document.uri.scheme === 'file' ? e : lastEditor;
}

function innermost(symbols, pos) {
  for (const s of symbols || []) {
    const range = s.range || (s.location && s.location.range);
    if (range && range.contains(pos)) return innermost(s.children, pos) || s;
  }
  return null;
}

/** The selection, else the function/class under the cursor, else ±40 lines around it. */
async function codeContext(editor) {
  if (!editor) return null;
  const doc = editor.document;
  let range = editor.selection;
  let how = 'selection';
  if (range.isEmpty) {
    how = 'lines around the cursor';
    try {
      const symbols = await vscode.commands.executeCommand('vscode.executeDocumentSymbolProvider', doc.uri);
      const s = innermost(symbols, range.active);
      if (s && (s.range || s.location)) { range = s.range || s.location.range; how = `the ${vscode.SymbolKind[s.kind] ? vscode.SymbolKind[s.kind].toLowerCase() : 'symbol'} ${s.name}`; }
    } catch (_) { /* no symbol provider for this language */ }
    if (range.isEmpty) {
      const line = range.active.line;
      const end = Math.min(doc.lineCount - 1, line + 40);
      range = new vscode.Range(Math.max(0, line - 40), 0, end, doc.lineAt(end).text.length);
    }
  }
  const start = range.start.line + 1;
  const end = range.end.line + 1 - (range.end.character === 0 && range.end.line > range.start.line ? 1 : 0);
  return { file: doc.uri.fsPath, start, end, text: doc.getText(range), how, repo: repoFor(doc.uri) };
}

function withTempFile(text, fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flint-swarm-'));
  const file = path.join(dir, 'selection.txt');
  fs.writeFileSync(file, text, 'utf8');
  return Promise.resolve().then(() => fn(file)).finally(() => fs.rmSync(dir, { recursive: true, force: true }));
}

function where(ctx) {
  return ctx ? `${path.relative(ctx.repo || path.dirname(ctx.file), ctx.file)}:${ctx.start}-${ctx.end}` : null;
}

// ---------------------------------------------------------------- actions

async function ask(question, ctx) {
  const repo = (ctx && ctx.repo) || repoFor(currentEditor() && currentEditor().document.uri);
  if (!repo) { vscode.window.showWarningMessage('Open a folder or file first; the swarm answers about a repository.'); return; }
  const id = crypto.randomBytes(6).toString('hex');
  panel.post({ type: 'askStart', id, question, where: where(ctx), repo });
  await panel.reveal();
  const c = cfg();
  const args = ['ask', '--repo', repo, '--question', question, '--models', String(c.get('models')),
    '--steps', String(c.get('stepsPerModel')), '--timeout', String(c.get('timeoutSeconds') || 300),
    '--paid', String(c.get('paidFallback') || 'auto')];
  if (!c.get('synthesize')) args.push('--no-synthesis');
  if (!c.get('useStudy')) args.push('--no-study');
  const go = (selectionFile) => vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'Flint swarm', cancellable: true },
    async (progress, token) => {
      progress.report({ message: 'models are reading your code…' });
      let answered = 0;
      const res = await runBridge(selectionFile ? [...args, '--file', ctx.file, '--start', String(ctx.start),
        '--end', String(ctx.end), '--selection-file', selectionFile] : args, {
        token,
        onEvent: (ev) => {
          if (ev.event === 'log') return;
          panel.post({ type: 'askEvent', id, event: ev });
          if (ev.event === 'answer') progress.report({ message: `${++answered} answered (${ev.model})` });
          if (ev.event === 'start' && ev.role === 'synthesis') progress.report({ message: `merging with ${ev.model}…` });
          if (ev.event === 'rescue' && ev.models && ev.models.length) {
            progress.report({ message: `no free model answered — paying for ${ev.models[0]}…` });
          }
        },
      });
      if (!res.events.some((e) => e.event === 'done')) {
        const why = token.isCancellationRequested ? 'cancelled' : (tail(res.stderr) || `bridge exited with code ${res.code}`);
        panel.post({ type: 'askEvent', id, event: { event: 'error', error: why } });
      }
      refreshStatus();
    });
  try {
    await (ctx ? withTempFile(ctx.text, go) : go(null));
  } catch (e) {
    panel.post({ type: 'askEvent', id, event: { event: 'error', error: e.message } });
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
}

async function pickQuestion() {
  const choice = await vscode.window.showQuickPick(PRESETS, { placeHolder: 'What should the swarm look at?' });
  if (!choice) return null;
  if (!choice.custom) return choice.label;
  return vscode.window.showInputBox({ prompt: 'Your question for the swarm', placeHolder: 'e.g. Why does this return None for an empty list?' });
}

async function cmdAsk() {
  const ctx = await codeContext(currentEditor());
  const question = await pickQuestion();
  if (question) await ask(question, ctx);
}

async function queueTask(title, ctx) {
  const repo = (ctx && ctx.repo) || repoFor(currentEditor() && currentEditor().document.uri);
  if (!repo) { vscode.window.showWarningMessage('Open a file in a Git repository first.'); return; }
  title = title || await vscode.window.showInputBox({ prompt: 'Task title for the swarm', placeHolder: 'e.g. Handle empty input in parse_orders' });
  if (!title) return;
  const detail = await vscode.window.showInputBox({ prompt: 'What must be true when it is done? (acceptance, optional)', placeHolder: 'e.g. parse_orders([]) returns [] and a test proves it' });
  if (detail === undefined) return;
  const args = ['task', '--repo', repo, '--title', title, '--detail', detail || title];
  try {
    const res = ctx ? await withTempFile(ctx.text, (f) => bridgeJson([...args, '--file', ctx.file, '--start', String(ctx.start), '--end', String(ctx.end), '--selection-file', f]))
      : await bridgeJson(args);
    if (res.duplicate_or_full) { vscode.window.showWarningMessage('Not queued: that title was already tried, or the queue is full.'); return; }
    const hint = res.daemon_running ? 'The running swarm will pick it up next.'
      : res.is_target ? 'Start the swarm to work on it.' : 'The swarm is set up for another repository; start it here to work on this task.';
    const act = await vscode.window.showInformationMessage(`Queued "${title}". ${hint}`, ...(res.daemon_running ? [] : ['Start swarm']));
    if (act === 'Start swarm') await startGrind(repo);
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
  refreshStatus();
}

async function cmdQueue() {
  await queueTask(null, await codeContext(currentEditor()));
}

// ---------------------------------------------------------------- the queue

/** The repository the panel is showing, which is not always the active editor's. */
function panelRepo(msg) {
  return (msg && msg.repo) || repoFor(currentEditor() && currentEditor().document.uri);
}

async function queueGet(msg) {
  const repo = panelRepo(msg);
  if (!repo) return;
  try {
    const res = await bridgeJson(['queue-get', '--repo', repo, '--id', msg.id]);
    panel.post({ type: 'queueTask', task: res.task, kinds: res.kinds });
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
    refreshStatus();
  }
}

async function queueEdit(msg) {
  const repo = panelRepo(msg);
  if (!repo) return;
  const args = ['queue-edit', '--repo', repo, '--id', msg.id];
  if (msg.title != null) args.push('--title', msg.title);
  if (msg.detail != null) args.push('--detail', msg.detail);
  if (msg.kind) args.push('--kind', msg.kind);
  if (msg.priority != null) args.push('--priority', String(msg.priority));
  for (const line of msg.acceptance || []) args.push('--acceptance', line);
  try {
    const res = await bridgeJson(args);
    panel.post({ type: 'queueSaved', task: res.task });
    vscode.window.setStatusBarMessage(`$(check) Swarm task updated: ${res.task.title}`, 4000);
  } catch (e) {
    panel.post({ type: 'queueError', id: msg.id, error: e.message });
  }
  refreshStatus();
}

async function queueRemove(msg) {
  const repo = panelRepo(msg);
  if (!repo) return;
  const blocks = msg.blocks || [];
  // A quick ✕ should stay quick. Only the two cases that lose more than one task ask first.
  if (msg.claimed || blocks.length) {
    const what = msg.claimed
      ? `A worker is running "${msg.title}" right now. Remove it anyway? The attempt in progress is abandoned; work already committed stays on the swarm branch.`
      : `"${msg.title}" is needed by ${blocks.length} other queued task(s): ${blocks.map((b) => b.title).join(', ')}. They can never run without it, so they are removed too.`;
    const ok = await vscode.window.showWarningMessage(what, { modal: true }, 'Remove');
    if (ok !== 'Remove') return;
  }
  const res = await bridgeLast(['queue-remove', '--repo', repo, '--id', msg.id,
    ...(blocks.length ? ['--cascade'] : [])]).catch((e) => ({ ok: false, error: e.message }));
  if (res.ok === false) {
    if ((res.needs_cascade || []).length) {   // it gained a dependent since the panel last looked
      return queueRemove(Object.assign({}, msg, { blocks: res.needs_cascade }));
    }
    vscode.window.showErrorMessage(`Flint swarm: ${res.error}`);
  } else {
    const names = (res.removed || []).map((r) => r.title);
    vscode.window.setStatusBarMessage(`$(trash) Removed from the swarm queue: ${names.join(', ')}`, 4000);
  }
  refreshStatus();
}

async function clearQueue(msg) {
  const repo = panelRepo(msg);
  if (!repo) { vscode.window.showWarningMessage('Open a file in a Git repository first.'); return; }
  const s = await bridgeJson(['status', '--repo', repo]).catch(() => null);
  const n = s ? s.queue.length : 0;
  if (!n) { vscode.window.showInformationMessage('The swarm queue is already empty.'); return; }
  const claimed = s.queue.filter((t) => t.claimed).length;
  const ok = await vscode.window.showWarningMessage(
    `Remove all ${n - claimed} waiting task(s) from the swarm queue for ${path.basename(repo)}?` +
    (claimed ? ` The ${claimed} task(s) being worked on right now are kept.` : ' This cannot be undone.'),
    { modal: true }, 'Clear the queue');
  if (ok !== 'Clear the queue') return;
  try {
    const res = await bridgeJson(['queue-clear', '--repo', repo]);
    vscode.window.showInformationMessage(`Removed ${res.removed.length} task(s) from the queue.`);
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
  refreshStatus();
}

async function showQueue() {
  await panel.reveal();
  panel.post({ type: 'tab', tab: 'queue' });
  return refreshStatus();
}

// ---------------------------------------------------------------- paid budget

/** What OpenRouter says about the credits behind the key, so the dialog can say whether the
 *  budget is actually spendable. */
function creditNote(account) {
  if (!account) return '';
  if (account.is_free_tier) return ' Warning: this API key has no purchased credits, so paid models will be refused.';
  const left = account.limit_remaining;
  return left != null ? ` The key has $${left.toFixed(2)} left.`
    : ` Spent on the account today: $${(account.usage_daily || 0).toFixed(4)}.`;
}

async function setBudget() {
  let current = null, account = null;
  try {
    const res = await bridgeJson(['wallet', '--account']);
    current = res.wallet;
    account = res.account;
  } catch (_) { /* reported below */ }
  const answer = await vscode.window.showInputBox({
    title: 'Paid model budget for this extension',
    prompt: (current
      ? `Dollars the extension may spend in total. $${current.spent.toFixed(4)} of $${current.cap.toFixed(2)} used so far. 0 turns paid models off.`
      : 'Dollars the extension may spend in total. 0 turns paid models off.') + creditNote(account),
    value: current ? String(current.cap) : '5',
    validateInput: (v) => (/^\d+(\.\d{1,2})?$/.test(v.trim()) ? null : 'A dollar amount, e.g. 5 or 2.50'),
  });
  if (answer === undefined) return;
  try {
    const res = await bridgeJson(['wallet', '--cap', String(parseFloat(answer)), '--enable']);
    const w = res.wallet;
    const act = w.spent > 0 ? await vscode.window.showInformationMessage(
      `Budget set to $${w.cap.toFixed(2)}. $${w.spent.toFixed(4)} of it is already spent.`, 'Reset to $0 spent') : null;
    if (act) await bridgeJson(['wallet', '--reset']);
    else if (w.spent === 0) vscode.window.showInformationMessage(`Budget set to $${w.cap.toFixed(2)}, nothing spent yet.`);
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
  lastInfo = 0;
  refreshStatus();
}

async function study(query) {
  if (!query) return;
  try {
    const res = await bridgeJson(['study', '--query', query.slice(0, 400), '-k', '6']);
    panel.post({ type: 'study', id: crypto.randomBytes(6).toString('hex'), query: query.slice(0, 120), hits: res.hits });
    await panel.reveal();
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
}

async function cmdStudy() {
  const editor = currentEditor();
  const selected = editor && !editor.selection.isEmpty ? editor.document.getText(editor.selection) : '';
  const guess = selected.replace(/[^A-Za-z0-9_]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 200);
  const query = await vscode.window.showInputBox({ prompt: 'Search MIT OpenCourseWare notes (technical terms work best)', value: guess,
    placeHolder: 'e.g. dijkstra priority queue, monte carlo variance, gradient clipping' });
  await study(query);
}

let swarmTerminal = null;

async function startGrind(repo) {
  repo = repo || repoFor(currentEditor() && currentEditor().document.uri);
  if (!repo) { vscode.window.showWarningMessage('Open a file in a Git repository first.'); return; }
  const name = path.basename(repo);
  const hasGoal = fs.existsSync(path.join(repo, 'GOAL.md'));
  const goal = await vscode.window.showInputBox({
    prompt: `What should ${name} become? ${hasGoal ? 'Leave empty to use GOAL.md.' : 'Leave empty to let the planner infer it from the README and code.'}`,
    placeHolder: 'e.g. A fast, well-tested order book simulator with a CLI' });
  if (goal === undefined) return;
  const testCmd = await vscode.window.showInputBox({ prompt: 'Test command that must pass for work to land (leave empty to auto-detect)', placeHolder: 'e.g. python -m pytest -q' });
  if (testCmd === undefined) return;
  const ok = await vscode.window.showWarningMessage(
    `Start the swarm on ${name}? It commits only to its own branch (swarm/trunk), never your checkout, uses free models only, and runs until you stop it.`,
    { modal: true }, 'Start');
  if (ok !== 'Start') return;
  const args = ['grind-cmd', '--repo', repo];
  if (goal) args.push('--goal', goal);
  if (testCmd) args.push('--test-cmd', testCmd);
  try {
    const { command } = await bridgeJson(args);
    if (swarmTerminal && swarmTerminal.exitStatus === undefined) swarmTerminal.dispose();
    swarmTerminal = vscode.window.createTerminal({ name: `Flint Swarm: ${name}`, cwd: flintRoot() });
    swarmTerminal.show(true);
    swarmTerminal.sendText(command);
    setTimeout(refreshStatus, 8000);
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
}

async function stopGrind() {
  const ok = await vscode.window.showWarningMessage('Stop the swarm? Work in progress is kept on its branch.', { modal: true }, 'Stop');
  if (ok !== 'Stop') return;
  try {
    const res = await bridgeJson(['stop']);
    vscode.window.showInformationMessage(res.stopped.length ? `Sent stop to ${res.stopped.length} swarm process(es).` : 'No running swarm found.');
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
  setTimeout(refreshStatus, 3000);
}

async function showReport() {
  const repo = repoFor(currentEditor() && currentEditor().document.uri);
  if (!repo) return;
  try {
    const res = await bridgeJson(['report', '--repo', repo]);
    const doc = await vscode.workspace.openTextDocument({ content: res.markdown, language: 'markdown' });
    await vscode.window.showTextDocument(doc, { preview: true });
    vscode.commands.executeCommand('markdown.showPreview', doc.uri).then(undefined, () => {});
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
}

async function openFile(msg) {
  let file = msg.path;
  if (!path.isAbsolute(file)) file = path.join(msg.repo || repoFor(currentEditor() && currentEditor().document.uri) || '', file);
  if (!fs.existsSync(file)) { vscode.window.showWarningMessage(`File not found: ${file}`); return; }
  const doc = await vscode.workspace.openTextDocument(file);
  let line = msg.line ? msg.line - 1 : 0;
  if (msg.page) {
    const at = doc.getText().split('\n').findIndex((l) => new RegExp(`^#+\\s.*\\bPage ${msg.page}\\b`).test(l) || l.includes(`id="page-${msg.page}"`));
    if (at >= 0) line = at;
  }
  const pos = new vscode.Position(Math.min(line, doc.lineCount - 1), 0);
  await vscode.window.showTextDocument(doc, { selection: new vscode.Range(pos, pos), preview: true, viewColumn: vscode.ViewColumn.One });
}

// ---------------------------------------------------------------- status

let refreshing = null;
function refreshStatus() {
  if (refreshing) return refreshing;
  const repo = repoFor(currentEditor() && currentEditor().document.uri);
  refreshing = (async () => {
    try {
      if (repo) {
        const s = await bridgeJson(['status', '--repo', repo]);
        panel.post({ type: 'status', data: s });
        statusItem.text = s.daemon_running ? `$(sync~spin) Swarm · ${s.queue.length} queued` : '$(organization) Swarm';
        statusItem.tooltip = (s.daemon_running ? `Flint swarm running on ${s.repo}`
          : 'Flint swarm: ask about the code you are working on')
          + (s.wallet ? `\nPaid budget: $${s.wallet.spent.toFixed(4)} of $${s.wallet.cap.toFixed(2)} used`
            : '\nPaid models off: free models only');
      }
      if (Date.now() - lastInfo > 5 * 60 * 1000) {
        lastInfo = Date.now();
        panel.post({ type: 'info', data: await bridgeJson(['info', '--quota']) });
      }
    } catch (e) {
      panel.post({ type: 'notice', text: e.message, level: 'error' });
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

// ---------------------------------------------------------------- webview

class SwarmPanel {
  constructor(context) { this.context = context; this.view = null; this.ready = false; this.queue = []; }

  resolveWebviewView(view) {
    this.view = view;
    this.ready = false;   // messages wait until panel.js says it is listening
    const media = vscode.Uri.joinPath(this.context.extensionUri, 'media');
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.html = this.html(view.webview, media);
    view.webview.onDidReceiveMessage((m) => this.onMessage(m));
    view.onDidChangeVisibility(() => { if (view.visible) refreshStatus(); });
  }

  post(msg) {
    if (this.view && this.ready) this.view.webview.postMessage(msg);
    else this.queue.push(msg);
  }

  async reveal() {
    await vscode.commands.executeCommand('flintSwarm.panel.focus');
    if (this.view) this.view.show(true);
  }

  html(webview, media) {
    const nonce = crypto.randomBytes(16).toString('base64');
    const uri = (f) => webview.asWebviewUri(vscode.Uri.joinPath(media, f));
    return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="stylesheet" href="${uri('panel.css')}"></head>
<body>
<div id="status"></div>
<div class="row"><button id="start" class="secondary">Start swarm</button><button id="stop" class="secondary">Stop</button><button id="report" class="secondary">Report</button></div>
<nav id="tabs" role="tablist">
  <button class="tab" id="tab-ask" data-tab="ask" role="tab" aria-selected="true">Swarm</button>
  <button class="tab" id="tab-queue" data-tab="queue" role="tab" aria-selected="false">Queue<span id="qcount" class="badge" hidden></span></button>
</nav>
<div id="notice" class="notice"></div>
<section id="pane-ask" role="tabpanel">
  <div id="recent"></div>
  <form id="askForm">
    <textarea id="question" placeholder="Ask the swarm about the code you're on… (⌘/Ctrl+Enter)"></textarea>
    <label><input type="checkbox" id="withCode" checked> Include my selection (or the function under the cursor)</label>
    <div class="row"><button type="submit">Ask the swarm</button><button id="queue" type="button" class="secondary">Queue as task</button><button id="lookup" type="button" class="secondary">MIT lookup</button></div>
  </form>
  <section id="threads"></section>
</section>
<section id="pane-queue" role="tabpanel" hidden>
  <div id="queueHead"></div>
  <div id="queueList"></div>
</section>
<script nonce="${nonce}" src="${uri('render.js')}"></script>
<script nonce="${nonce}" src="${uri('panel.js')}"></script>
</body></html>`;
  }

  async onMessage(m) {
    if (m.type === 'ready') {
      this.ready = true;
      for (const q of this.queue.splice(0)) this.view.webview.postMessage(q);
      lastInfo = 0;
      refreshStatus();
    } else if (m.type === 'ask') {
      await ask(m.question, m.withCode ? await codeContext(currentEditor()) : null);
    } else if (m.type === 'queue') {
      await queueTask(m.title, m.withCode ? await codeContext(currentEditor()) : null);
    } else if (m.type === 'study') {
      let q = m.query;
      if (!q && m.withCode) {
        const ctx = await codeContext(currentEditor());
        q = ctx && ctx.text.replace(/[^A-Za-z0-9_]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 200);
      }
      if (q) await study(q); else await cmdStudy();
    } else if (m.type === 'vote') {
      bridgeJson(['vote', '--model', m.model, '--useful', m.useful ? '1' : '0']).catch((e) => vscode.window.showErrorMessage(e.message));
    } else if (m.type === 'open') {
      await openFile(m);
    } else if (m.type === 'start') {
      await startGrind();
    } else if (m.type === 'stop') {
      await stopGrind();
    } else if (m.type === 'report') {
      await showReport();
    } else if (m.type === 'queueGet') {
      await queueGet(m);
    } else if (m.type === 'queueEdit') {
      await queueEdit(m);
    } else if (m.type === 'queueRemove') {
      await queueRemove(m);
    } else if (m.type === 'queueClear') {
      await clearQueue(m);
    } else if (m.type === 'budget') {
      await setBudget();
    } else if (m.type === 'refresh') {
      await refreshStatus();
    }
  }
}

// ---------------------------------------------------------------- lifecycle

function activate(context) {
  panel = new SwarmPanel(context);
  lastEditor = vscode.window.activeTextEditor || null;
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusItem.text = '$(organization) Swarm';
  statusItem.tooltip = 'Flint swarm: ask about the code you are working on';
  statusItem.command = 'flintSwarm.panel.focus';
  statusItem.show();
  const reg = (id, fn) => context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  context.subscriptions.push(
    statusItem,
    vscode.window.registerWebviewViewProvider('flintSwarm.panel', panel, { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.window.onDidChangeActiveTextEditor((e) => { if (e && e.document.uri.scheme === 'file') lastEditor = e; }),
  );
  reg('flintSwarm.askSelection', cmdAsk);
  reg('flintSwarm.queueTask', cmdQueue);
  reg('flintSwarm.studySelection', cmdStudy);
  reg('flintSwarm.startGrind', () => startGrind());
  reg('flintSwarm.stopGrind', stopGrind);
  reg('flintSwarm.showReport', showReport);
  reg('flintSwarm.showQueue', showQueue);
  reg('flintSwarm.clearQueue', () => clearQueue());
  reg('flintSwarm.setBudget', setBudget);
  reg('flintSwarm.refresh', () => { lastInfo = 0; return refreshStatus(); });
  const timer = setInterval(() => { if (panel.view && panel.view.visible) refreshStatus(); }, 60 * 1000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
  if (!flintRoot()) {
    vscode.window.showWarningMessage('Flint Swarm cannot find your flint checkout. Set "Flint Swarm: Flint Path" in Settings.', 'Open Settings')
      .then((a) => { if (a) vscode.commands.executeCommand('workbench.action.openSettings', 'flintSwarm.flintPath'); });
  }
}

function deactivate() {
  for (const child of running) child.kill('SIGTERM');
}

module.exports = { activate, deactivate,
  _test: { parseLines, innermost, flintRoot, where, bridgeJson, bridgeLast, PRESETS } };
