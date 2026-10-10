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
  { label: 'Ask something else…', custom: true },
];

let panel = null;
let statusItem = null;
let lastEditor = null;
let lastInfo = 0;
let extensionContext = null;
let secretApiKey = null;
let secretReady = Promise.resolve();
let modelProvidersCache = [];
const running = new Set();
const SECRET_KEY = 'flintSwarm.openRouterApiKey';
const SKIN_KEY = 'flintSwarm.skin';
const UPDATE_CLICKS_KEY = 'flintSwarm.updateClicks';
const MAX_UPDATE_CLICKS = 999999999;
let updateInstallPromise = null;

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

// ---------------------------------------------------------------- self-update click counter

/** Render the persistent click count as three base-1000 fields: 1000 -> 0.1.0. */
function updateVersion(count) {
  const safe = Math.max(0, Math.min(MAX_UPDATE_CLICKS, Math.trunc(Number(count) || 0)));
  return `${Math.floor(safe / 1000000)}.${Math.floor(safe / 1000) % 1000}.${safe % 1000}`;
}

function updateClicks(context) {
  const raw = context.globalState.get(UPDATE_CLICKS_KEY, 0);
  return Math.max(0, Math.min(MAX_UPDATE_CLICKS, Math.trunc(Number(raw) || 0)));
}

/** Run the existing installer as a child so success is known before Cursor reloads. */
function runExtensionInstaller(root) {
  return new Promise((resolve, reject) => {
    const script = path.join(root, 'cursor-extension', 'install.sh');
    if (!fs.existsSync(script)) {
      reject(new Error(`installer not found: ${script}`));
      return;
    }
    const child = cp.spawn(script, [], {
      cwd: root,
      env: { ...process.env, FLINT_REQUIRE_INSTALL: '1' },
    });
    running.add(child);
    let stdout = '', stderr = '', settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      running.delete(child);
      if (error) reject(error); else resolve(value);
    };
    child.stdout.on('data', (data) => { stdout = (stdout + data.toString('utf8')).slice(-30000); });
    child.stderr.on('data', (data) => { stderr = (stderr + data.toString('utf8')).slice(-30000); });
    child.on('error', (error) => finish(error));
    child.on('close', (code) => {
      if (code === 0) finish(null, { stdout, stderr });
      else finish(new Error(`install.sh exited ${code}: ${tail(stderr || stdout, 8)}`));
    });
  });
}

/** Count the click first, then install and reload. The count deliberately survives failure. */
function installExtensionUpdate(context, post, installer = runExtensionInstaller,
  restart = () => vscode.commands.executeCommand('workbench.action.reloadWindow')) {
  if (updateInstallPromise) return updateInstallPromise;
  updateInstallPromise = (async () => {
    const count = Math.min(MAX_UPDATE_CLICKS, updateClicks(context) + 1);
    await context.globalState.update(UPDATE_CLICKS_KEY, count);
    const version = updateVersion(count);
    post({ type: 'updateVersion', count, version, busy: true });
    try {
      const root = flintRoot();
      if (!root) throw new Error('cannot find the Flint checkout containing cursor-extension/install.sh');
      await installer(root);
      post({ type: 'updateVersion', count, version, busy: false, installed: true });
      void vscode.window.showInformationMessage(`Flint update ${version} installed. Restarting this Cursor window…`);
      await restart();
      return true;
    } catch (error) {
      const message = error && error.message ? error.message : String(error);
      post({ type: 'updateVersion', count, version, busy: false, error: message });
      vscode.window.showErrorMessage(`Flint update ${version} failed: ${message}`);
      return false;
    }
  })().finally(() => { updateInstallPromise = null; });
  return updateInstallPromise;
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
      { cwd: root, env: Object.assign({}, process.env,
        secretApiKey ? { PYTHONUNBUFFERED: '1', OPENROUTER_API_KEY: secretApiKey } : { PYTHONUNBUFFERED: '1' }) });
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

/** The provider the user last picked for asks, remembered per workspace. */
function provState() { return vscode.workspace.getConfiguration('flintSwarm').get('providerState') || {}; }

/** Persist the provider/model pick across restarts, so a local setup does not need re-picking. */
function saveProvState(backend, model, customUrl) {
  const c = cfg();
  const prev = provState();
  const target = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length
    ? vscode.ConfigurationTarget.Workspace : vscode.ConfigurationTarget.Global;
  return c.update('providerState', { ...prev, backend, model,
    ...(customUrl != null ? { customUrl: String(customUrl).trim() } : {}) }, target);
}

/** The last chosen backend, or 'openrouter' when nothing was picked. */
function chosenBackend() { return (provState().backend || 'openrouter'); }

/** All backends the picker offers, locals first, all sourced from the bridge registry. */
function providerChoices() {
  return [
    { id: 'openrouter', label: '$(cloud) OpenRouter (free models)', detail: 'the swarm\'s normal cloud models', local: false },
    { id: 'ollama', label: '$(server) Ollama', detail: 'local · http://localhost:11434', local: true },
    { id: 'lm-studio', label: '$(server) LM Studio', detail: 'local · http://localhost:1234', local: true },
    { id: 'mlx', label: '$(server) MLX', detail: 'local · http://localhost:8080 (Apple Silicon)', local: true },
    { id: 'llamacpp', label: '$(server) llama.cpp', detail: 'local · http://localhost:8080', local: true },
    { id: '__custom__', label: '$(edit) Custom endpoint…', detail: 'any OpenAI-compatible URL (e.g. LM Studio on a remote box)', local: true },
  ];
}

/** Pick a backend, then (for locals) fetch its /models list and pick a model. Never raises. */
async function pickProviderModel() {
  const current = chosenBackend();
  let backend = await vscode.window.showQuickPick(providerChoices().map((p) => ({
    ...p, description: p.id === current ? '$(check) selected' : '', sortText: p.local ? '0' : '1',
  })), {
    placeHolder: 'Which backend should answer?',
    title: 'Flint Swarm · choose a provider',
  });
  if (!backend) return null;
  if (backend.id === '__custom__') {
    const url = await vscode.window.showInputBox({
      prompt: 'Custom OpenAI-compatible endpoint http(s)://host:port',
      placeHolder: 'http://localhost:1234/v1',
      validateInput: (v) => (v && /^https?:\/\//.test(v)) ? null : 'needs to start with http:// or https://',
    });
    if (!url) return null;
    backend = { id: 'custom:' + url, label: url, local: true, baseUrl: url };
  }
  let baseUrl = backend.baseUrl;
  if (backend.local && !baseUrl) {
    // Probe this provider's /models — the bridge lists them without needing ollama etc.
    try {
      const info = await bridgeJson(['local-models']);
      const prov = (info.providers || []).find((p) => p.id === backend.id);
      if (prov && prov.running) {
        baseUrl = prov.baseUrl;
      } else {
        const pick = await vscode.window.showQuickPick(
          [{ label: 'Retry', description: prov ? prov.label : '' }, { label: 'Back' }],
          { placeHolder: `${prov ? prov.label : backend.label} is not running. Make sure the server is up, then retry.` });
        if (!pick || pick.label !== 'Retry') return null;
        return pickProviderModel();
      }
    } catch (e) {
      vscode.window.showWarningMessage(`Could not reach ${backend.label}: ${e.message}`);
      return null;
    }
  }
  return { backend: backend.id, label: backend.label, local: backend.local, baseUrl };
}

/** For a chosen local backend, fetch its model list from the bridge and pick one. */
async function pickLocalModel(backend) {
  let list;
  try {
    const info = await bridgeJson(['local-models']);
    const prov = (info.providers || []).find((p) => p.id === backend.backend);
    list = prov ? prov.models : [];
  } catch (e) { list = []; }
  if (!list.length) {
    vscode.window.showWarningMessage(`${backend.label}: no models reported. Is the server running?`);
    return null;
  }
  const chosen = await vscode.window.showQuickPick(list.map((m) => ({
    label: m.name || m.id, description: m.context ? `${m.context.toLocaleString()} ctx` : '',
  })), { placeHolder: `${backend.label}: which model?` });
  return chosen ? list.find((m) => (m.name || m.id) === chosen.label) : null;
}

/** Bridge routing flags for a provider selected by pickProviderModel(). */
function providerArgs(prov) {
  const args = [];
  const backendId = prov.backend.startsWith('custom:') ? null : prov.backend;
  // The picker probes named local providers and returns the endpoint that actually
  // answered. Preserve it: it may not be the registry's default port.
  const baseUrl = prov.local ? (prov.baseUrl || (prov.backend.startsWith('custom:') ? prov.backend.slice(7) : null)) : null;
  if (backendId && backendId !== 'openrouter') args.push('--provider', backendId);
  if (baseUrl) args.push('--base-url', baseUrl);
  return args;
}

/** Resolve the compact provider/model controls in the sidebar without opening another picker. */
async function providerFromPanel(choice) {
  const backend = (choice && choice.backend) || 'openrouter';
  if (backend === 'openrouter') return { backend, label: 'OpenRouter', local: false, baseUrl: null };
  if (backend === '__custom__') {
    const url = String(choice.customUrl || '').trim().replace(/\/$/, '');
    if (!/^https?:\/\//.test(url)) throw new Error('The custom model endpoint must start with http:// or https://.');
    return { backend: 'custom:' + url, label: url, local: true, baseUrl: url };
  }
  const known = providerChoices().find((p) => p.id === backend && p.local);
  if (!known) throw new Error(`Unknown model provider: ${backend}`);
  const info = await bridgeJson(['local-models']);
  const found = (info.providers || []).find((p) => p.id === backend);
  if (!found || !found.running) throw new Error(`${known.label.replace(/^\$\([^)]*\)\s*/, '')} is not running.`);
  return { backend, label: found.label || known.label, local: true, baseUrl: found.baseUrl };
}

async function ask(question, ctx, choice = null) {
  const repo = (ctx && ctx.repo) || repoFor(currentEditor() && currentEditor().document.uri);
  if (!repo) { vscode.window.showWarningMessage('Open a folder or file first; the swarm answers about a repository.'); return; }
  // The provider/model picker is the Phase 1 headline: a backend dropdown (local servers
  // first), then — for a local backend — the server's own model list from /models, then a
  // custom endpoint prompt. The pick is persisted so a local setup stays put between sessions.
  let prov;
  try { prov = choice ? await providerFromPanel(choice) : await pickProviderModel(); }
  catch (e) { vscode.window.showErrorMessage(`Flint Swarm: ${e.message}`); return; }
  if (!prov) return;
  let model = null;
  if (choice) {
    model = choice.model && choice.model !== '__auto__' ? choice.model : null;
    if (prov.local && !model) { vscode.window.showWarningMessage('Choose a model from the local provider before asking.'); return; }
  } else if (prov.local) {
    const picked = await pickLocalModel(prov);
    if (!picked) return;
    model = picked.id || picked.name;
  } else {
    model = (provState().model || null);   // the last cloud model asked with, if any
  }
  await saveProvState(prov.backend.startsWith('custom:') ? '__custom__' : prov.backend, model,
    choice && choice.customUrl);
  const id = crypto.randomBytes(6).toString('hex');
  panel.post({ type: 'askStart', id, question, where: where(ctx), repo, provider: prov.label });
  await panel.reveal();
  const c = cfg();
  const args = ['ask', '--repo', repo, '--question', question, '--models', String(c.get('models')),
    '--steps', String(c.get('stepsPerModel')), '--timeout', String(c.get('timeoutSeconds') || 300),
    '--paid', String(c.get('paidFallback') || 'off')];
  args.push(...providerArgs(prov));
  if (model) args.push('--model', model);
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
  const withCtx = (extra) => (ctx
    ? withTempFile(ctx.text, (f) => bridgeJson([...args, ...extra, '--file', ctx.file,
      '--start', String(ctx.start), '--end', String(ctx.end), '--selection-file', f]))
    : bridgeJson([...args, ...extra]));
  // Say what will be queued before it is queued. The same scope check the worker runs before
  // it spends anything runs here too, so a request with nothing to aim at is caught while the
  // developer is still looking at it — not an hour later, as a parked task.
  const pre = await withCtx(['--preview']).catch(() => null);
  if (pre) {
    if (pre.duplicate) { vscode.window.showWarningMessage(`"${title}" has already been queued or tried.`); return; }
    if (pre.queue_full) { vscode.window.showWarningMessage(`The queue is full (${pre.queued}/${pre.max_queue}).`); return; }
    const lines = [`${pre.task.kind} · P${pre.task.priority} · verified with \`${pre.test_cmd || 'the configured checks'}\``];
    if (pre.where) lines.push(`about ${pre.where}`);
    lines.push('', 'Done when:', ...(pre.task.acceptance || []).map((x) => `  • ${x}`));
    if (pre.scope_gap) lines.push('', `⚠ ${pre.scope_gap}`);
    const buttons = pre.scope_gap ? ['Queue anyway', 'Edit'] : ['Queue', 'Edit'];
    const act = await vscode.window.showInformationMessage(
      `Queue "${title}"?`, { modal: true, detail: lines.join('\n') }, ...buttons);
    if (act === 'Edit') { await queueTask(title, ctx); return; }
    if (!act) return;
  }
  try {
    const res = await withCtx([]);
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

const PAID_LABEL = {
  off: 'Free models only.',
  auto: 'Paid models only when no free model answered.',
  always: 'Paid models from the start — the budget goes quickest this way.',
};

/** Flip paid requests on or off, globally and visibly. */
async function togglePaid(to) {
  const c = cfg();
  const now = c.get('paidFallback') || 'off';
  const next = to || (now === 'off' ? 'auto' : 'off');
  const target = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length
    ? vscode.ConfigurationTarget.Workspace : vscode.ConfigurationTarget.Global;
  await c.update('paidFallback', next, target);
  vscode.window.setStatusBarMessage(
    next === 'off' ? '$(circle-slash) Swarm: paid requests OFF' : `$(credit-card) Swarm: paid requests ${next}`, 5000);
  if (next !== 'off') {
    const w = await bridgeJson(['wallet']).catch(() => null);
    if (w && w.wallet && w.wallet.remaining <= 0) {
      vscode.window.showWarningMessage('Paid requests are on, but the editor wallet has nothing left. Set a budget first.');
    }
  }
  refreshStatus();
  return next;
}

async function cmdTogglePaid() {
  const now = cfg().get('paidFallback') || 'off';
  const pick = await vscode.window.showQuickPick(
    ['off', 'auto', 'always'].map((v) => ({ label: v === now ? `$(check) ${v}` : v, value: v, description: PAID_LABEL[v] })),
    { placeHolder: `Paid requests are currently "${now}"` });
  if (pick) await togglePaid(pick.value);
}

const QUOTA_MODE_ORDER = ['shutdown', 'wait', 'paid'];
const QUOTA_MODE_LABEL = {
  shutdown: 'Stop safely after the current task.',
  wait: 'Stay open and continue after the daily reset.',
  paid: 'Continue with a configured paid model and spending cap.',
};

/** Flip the repo daemon's quota-exhaustion failsafe, via the bridge (it lives in that
 * repo's own tuned config, not an extension setting). */
async function setQuotaMode(repo, mode) {
  const res = await bridgeJson(['quota-mode', '--repo', repo, '--set', mode]);
  if (res && res.ok === false) {
    vscode.window.showWarningMessage(`Flint swarm: ${res.error}`);
    return null;
  }
  const words = { shutdown: 'stop safely', wait: 'wait for reset', paid: 'continue with paid fallback' };
  vscode.window.setStatusBarMessage(`$(sync) Swarm at free limit: ${words[mode] || mode}`, 5000);
  lastInfo = 0;
  refreshStatus();
  return mode;
}

async function cmdSetQuotaMode() {
  const repo = repoFor(currentEditor() && currentEditor().document.uri);
  if (!repo) {
    vscode.window.showWarningMessage('Flint swarm: open a file in the target repository first.');
    return;
  }
  const s = await bridgeJson(['status', '--repo', repo]).catch(() => null);
  const now = (s && s.quota_mode) || 'shutdown';
  const pick = await vscode.window.showQuickPick(
    QUOTA_MODE_ORDER.map((v) => ({ label: v === now ? `$(check) ${v}` : v, value: v, description: QUOTA_MODE_LABEL[v] })),
    { placeHolder: `On quota exhaustion, this repo is currently set to "${now}"` });
  if (pick) await setQuotaMode(repo, pick.value);
}

/** What happened on a task: the attempt bundles the daemon recorded, newest first. */
async function showEvidence(m) {
  const repo = panelRepo(m);
  if (!repo || !m.id) return;
  let res;
  try {
    res = await bridgeJson(['evidence', '--repo', repo, '--id', m.id]);
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
    return;
  }
  const attempts = res.attempts || [];
  if (!attempts.length) {
    vscode.window.showInformationMessage('No attempt has been recorded for that task yet.');
    return;
  }
  const withHandoff = attempts.filter((a) => a.handoff);
  const target = withHandoff.length === 1 ? withHandoff[0] : await vscode.window.showQuickPick(
    attempts.map((a) => ({
      label: a.attempt,
      description: [a.phase, a.commit && a.commit.slice(0, 8), a.seconds && `${Math.round(a.seconds)}s`]
        .filter(Boolean).join(' · '),
      detail: (a.note || '').slice(0, 160),
      attempt: a,
    })), { placeHolder: 'Which attempt?' }).then((p) => p && p.attempt);
  if (!target) return;
  const file = target.handoff || (target.files.find((f) => f.name === 'attempt.json') || {}).path;
  if (!file) { vscode.window.showInformationMessage(`Evidence is in ${target.dir}`); return; }
  const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
  if (file.endsWith('.md')) {
    await vscode.window.showTextDocument(doc, { preview: true });
    vscode.commands.executeCommand('markdown.showPreview', doc.uri).then(undefined, () => {});
  } else {
    await vscode.window.showTextDocument(doc, { preview: true });
  }
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

let swarmTerminal = null;
let daemonRunning = false;      // paces the refresh loop: 10s while it runs, 60s when it does not

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

/** The repository a swarm command should act on: the one the caller named, else the one the
 *  open file belongs to, else the one the swarm is configured for. The last fallback matters for
 *  Stop: wanting to stop a run is not a reason to have a file from that repo open, and without
 *  it the button refused from the command palette and targeted the wrong repository when the
 *  open file happened to belong to a different one. */
async function swarmRepo(repo) {
  if (repo) return repo;
  const open = repoFor(currentEditor() && currentEditor().document.uri);
  try {
    const info = await bridgeJson(['info']);
    if (info.configured_repo) {
      if (!open || open === info.configured_repo) return info.configured_repo;
      return open;                       // an explicit file elsewhere wins over the default
    }
  } catch (e) { /* fall through to whatever the editor knows */ }
  return open;
}

async function stopGrind(repo, drain) {
  repo = await swarmRepo(repo);
  if (!repo) { vscode.window.showWarningMessage('No swarm repository: open a file in one, or run `swarm grind <repo>` once to configure it.'); return; }
  const name = path.basename(repo);
  if (drain === undefined) {
    // Stopping now kills the turn in flight and loses model work already paid for, so the
    // gentler option is offered first.
    const ok = await vscode.window.showWarningMessage(
      `Stop the swarm on ${name}? Swarms on other repositories keep running.`,
      { modal: true }, 'Finish this task first', 'Stop now');
    if (ok !== 'Stop now' && ok !== 'Finish this task first') return;
    drain = ok === 'Finish this task first';
  }
  try {
    const res = await bridgeJson(drain ? ['stop', '--repo', repo, '--drain'] : ['stop', '--repo', repo]);
    if (res.ok === false) vscode.window.showErrorMessage(`Flint swarm: ${res.error}`);
    else if (drain && (res.draining || []).length) vscode.window.showInformationMessage(`The swarm on ${name} will stop after the task in flight.`);
    else if ((res.stopped || []).length) vscode.window.showInformationMessage(`Stopped the swarm on ${name}.`);
    else vscode.window.showInformationMessage(`No swarm is running on ${name}.`);
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
  setTimeout(refreshStatus, 3000);
}

/** Drain, wait for the daemon to go, then start again on the same config. */
async function restartGrind(repo) {
  repo = repo || repoFor(currentEditor() && currentEditor().document.uri);
  if (!repo) { vscode.window.showWarningMessage('Open a file in a Git repository first.'); return; }
  const name = path.basename(repo);
  const ok = await vscode.window.showWarningMessage(
    `Restart the swarm on ${name}? It finishes the task in flight first, then starts again on the same config.`,
    { modal: true }, 'Restart');
  if (ok !== 'Restart') return;
  try {
    const res = await bridgeJson(['stop', '--repo', repo, '--drain']);
    if (res.ok === false) { vscode.window.showErrorMessage(`Flint swarm: ${res.error}`); return; }
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Waiting for ${name} to finish its task…` },
      async () => {
        // A task can take minutes. Give it ten, then say so rather than starting a second daemon.
        for (let i = 0; i < 120; i++) {
          const s = await bridgeJson(['status', '--repo', repo]);
          if (!s.daemon_running) return;
          await new Promise((r) => setTimeout(r, 5000));
        }
        throw new Error('it is still working after 10 minutes; start it yourself when it stops');
      });
    const { command } = await bridgeJson(['grind-cmd', '--repo', repo, '--goal', 'GOAL.md']);
    if (swarmTerminal && swarmTerminal.exitStatus === undefined) swarmTerminal.dispose();
    swarmTerminal = vscode.window.createTerminal({ name: `Flint Swarm: ${name}`, cwd: flintRoot() });
    swarmTerminal.show(true);
    swarmTerminal.sendText(command);
    setTimeout(refreshStatus, 8000);
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
}

// One read-only board tab per repository; never changes task or daemon state.
const boards = new Map();
async function showBoard(completedOnly = false) {
  const repo = repoFor(currentEditor() && currentEditor().document.uri);
  const root = flintRoot();
  if (!repo || !root) return;
  if (boards.has(repo)) {
    const current = boards.get(repo);
    current.completedOnly = completedOnly;
    current.view.reveal();
    await current.refresh();
    return;
  }
  const view = vscode.window.createWebviewPanel('flintSwarm.board', 'Swarm scrum board',
    vscode.ViewColumn.One, { enableScripts: true, localResourceRoots: [] });
  const state = { view, completedOnly, busy: false, closed: false };
  state.refresh = async () => {
    if (state.busy || state.closed) return;
    state.busy = true;
    try {
      const data = await new Promise((resolve, reject) => {
        cp.execFile(pythonFor(root), [path.join(root, 'swarm', 'board.py'), '--repo', repo],
          { timeout: 60000, maxBuffer: 16 * 1024 * 1024 }, (error, stdout) => {
            if (error) return reject(error);
            try { resolve(JSON.parse(stdout)); } catch (e) { reject(e); }
          });
      });
      if (!state.closed) {
        view.title = state.completedOnly ? 'Verified completed' : 'Swarm scrum board';
        view.webview.html = require('./board-view').html(data, state.completedOnly);
      }
    } catch (e) {
      if (!state.closed) vscode.window.showErrorMessage(`Swarm board could not refresh: ${e.message}. Any displayed snapshot is stale.`);
    } finally { state.busy = false; }
  };
  boards.set(repo, state);
  view.onDidDispose(() => { state.closed = true; boards.delete(repo); });
  view.webview.onDidReceiveMessage(async (m) => {
    if (m.type === 'toggle') state.completedOnly = !state.completedOnly;
    if (m.type === 'refresh' || m.type === 'toggle') await state.refresh();
  });
  await state.refresh();
}

async function showLanded(repo) {
  repo = repo || repoFor(currentEditor() && currentEditor().document.uri);
  if (!repo) return;
  try {
    panel.post({ type: 'landed', ...(await bridgeJson(['landed', '--repo', repo, '--limit', '20'])) });
  } catch (e) {
    panel.post({ type: 'notice', text: e.message, level: 'error' });
  }
}

/** `git show <sha>` for a landed commit, in a read-only editor. */
async function showCommit(m) {
  if (!m.repo || !m.sha) return;
  try {
    const stdout = await new Promise((resolve, reject) => {
      cp.execFile('git', ['-C', m.repo, 'show', '--stat', '--patch', m.sha],
        { maxBuffer: 8 * 1024 * 1024 }, (err, out) => err ? reject(err) : resolve(out));
    });
    const doc = await vscode.workspace.openTextDocument({ content: stdout, language: 'diff' });
    await vscode.window.showTextDocument(doc, { preview: true });
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: could not show ${m.sha.slice(0, 8)}: ${e.message}`);
  }
}

async function queueRetry(m) {
  try {
    const res = await bridgeJson(['queue-retry', '--repo', m.repo, '--id', m.id]);
    if (res.ok === false) vscode.window.showErrorMessage(`Flint swarm: ${res.error}`);
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
  await refreshStatus();
}

async function queueRequeue(m) {
  try {
    const res = await bridgeJson(['queue-requeue', '--repo', m.repo, '--id', m.id]);
    if (res.ok === false) vscode.window.showErrorMessage(`Flint swarm: ${res.error}`);
    else vscode.window.showInformationMessage(`Back in the queue: ${res.task.title}`);
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
  await refreshStatus();
}

async function dismissParked(m) {
  try {
    const args = ['parked-dismiss', '--repo', m.repo];
    args.push(...(m.id ? ['--id', m.id] : ['--all']));
    const res = await bridgeJson(args);
    if (res.ok === false) vscode.window.showErrorMessage(`Flint swarm: ${res.error}`);
    else vscode.window.showInformationMessage(`Dismissed ${res.dismissed} abandoned task(s). Logs and patches are preserved.`);
  } catch (e) {
    vscode.window.showErrorMessage(`Flint swarm: ${e.message}`);
  }
  await refreshStatus();
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

// ---------------------------------------------------------------- activity HUD

// A read-only terminal tab that tails the daemon's own log. The sidebar already shows the
// last few journal lines, but the journal only records task boundaries — a hung turn and a
// quiet one look the same there. What gives a hang away is elapsed time with no new output,
// so this prints how long the log has been silent on every poll, whether or not it grew.
const IDLE_WARN_S = 600;        // ten minutes of silence is worth a colour

/** "4m12s" — a duration short enough to read at a glance in a status line. */
function idleFor(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m${String(s % 60).padStart(2, '0')}s`
    : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`;
}

let activityTerminal = null;

async function showActivity(repo, fetch) {
  const ask = fetch || bridgeJson;          // a seam, so the poll loop is testable offline
  repo = repo || repoFor(currentEditor() && currentEditor().document.uri);
  if (!repo) { vscode.window.showWarningMessage('Open a file in a Git repository first.'); return; }
  if (activityTerminal && activityTerminal.exitStatus === undefined) {
    activityTerminal.show(false);     // one tab per window; reuse the open one
    return;
  }
  const writer = new vscode.EventEmitter();
  const write = (s) => writer.fire(s.replace(/\n/g, '\r\n'));
  let timer = null, offset = 0, day = null, quiet = 0, started = false;

  const poll = async () => {
    let a;
    try {
      a = await ask(['activity', '--repo', repo, '--offset', String(offset)]);
    } catch (e) {
      write(`\x1b[31m${e.message}\x1b[0m\n`);
      return;
    }
    if (a.day !== day) { day = a.day; offset = 0; quiet = 0; started = false; }
    if (!a.exists) {
      if (!started) { write('\x1b[2mNo log for today yet — the swarm has not run.\x1b[0m\n'); started = true; }
      return;
    }
    if (!started) { write(`\x1b[2m${a.path}\x1b[0m\n`); started = true; }
    offset = a.offset;
    for (const line of a.lines) write(line + '\n');
    // The heartbeat is the point: it proves the tab is live even when the log is not.
    const stale = a.stale_seconds == null ? 0 : a.stale_seconds;
    if (a.lines.length) quiet = 0; else quiet++;
    const colour = stale >= IDLE_WARN_S ? '31' : '2';
    const state = a.daemon_running ? 'running' : '\x1b[33mno daemon\x1b[0m';
    if (a.lines.length || quiet % 5 === 1) {
      write(`\x1b[${colour}m── quiet ${idleFor(stale)} · ${state}\x1b[${colour}m ──\x1b[0m\n`);
    }
  };

  const pty = {
    onDidWrite: writer.event,
    open: () => {
      write('\x1b[1mFlint Swarm — activity\x1b[0m  \x1b[2m(read-only; close the tab to stop)\x1b[0m\n');
      poll();
      timer = setInterval(poll, 3000);
    },
    close: () => { if (timer) clearInterval(timer); timer = null; writer.dispose(); },
  };
  activityTerminal = vscode.window.createTerminal({ name: 'Flint Swarm: activity', pty });
  activityTerminal.show(false);
}

// ---------------------------------------------------------------- status

// The final-touch gate: on a fresh Cursor start, if no OpenRouter key is configured and
// the last-chosen backend is the cloud one, ask for the key once per session instead of
// letting the first ask die at the bridge with a cryptic "OPENROUTER_API_KEY is not set".
// A local-only setup (Ollama, LM Studio, …) never triggers this: local asks need no key.
let keyPrompted = false;

function validApiKey(key) { return /^sk-(or-)?[A-Za-z0-9_-]+$/.test(String(key || '').trim()); }

/** Keep the key in Cursor/VS Code SecretStorage. It is exposed only to bridge child processes. */
async function storeApiKey(key) {
  key = String(key || '').trim();
  if (!validApiKey(key)) throw new Error('An OpenRouter key starts with sk-or-.');
  if (extensionContext && extensionContext.secrets && extensionContext.secrets.store) {
    await extensionContext.secrets.store(SECRET_KEY, key);
    secretApiKey = key;
    return { ok: true, storage: 'Cursor encrypted secret storage' };
  }
  // Compatibility for older hosts and the offline test shim.
  const res = await bridgeJson(['set-key', '--key', key]);
  if (!res || !res.ok) throw new Error((res && res.error) || 'could not save the key');
  return { ok: true, storage: res.path || 'the flint .env' };
}

async function clearStoredApiKey() {
  if (extensionContext && extensionContext.secrets && extensionContext.secrets.delete) {
    await extensionContext.secrets.delete(SECRET_KEY);
  }
  secretApiKey = null;
}

/** Ask for an OpenRouter API key and store it in the editor's encrypted keychain. */
async function promptForApiKey(message) {
  if (keyPrompted || !flintRoot()) return false;
  keyPrompted = true;
  const key = await vscode.window.showInputBox({
    prompt: message || 'OpenRouter API key (sk-or-...) — required for cloud asks and the swarm',
    password: true,
    placeHolder: 'sk-or-…',
    ignoreFocusOut: true,
    validateInput: (v) => (v && /^sk-(or-)?[A-Za-z0-9_-]+$/.test(v.trim())) ? null : 'an OpenRouter key starts with sk-or-',
  });
  if (!key) return false;
  try {
    const res = await storeApiKey(key);
    vscode.window.showInformationMessage('OpenRouter API key saved in ' + res.storage + '.');
    return true;
  } catch (e) {
    vscode.window.showErrorMessage('Flint Swarm: ' + e.message);
  }
  return false;
}

async function maybePromptForApiKey() {
  if (keyPrompted) return;
  await secretReady;
  if (secretApiKey) return;
  // Only cloud asks need the key; a local-only pick (ollama, lm-studio, …) is fine without it.
  if (chosenBackend() !== 'openrouter') return;
  let info = null;
  try { info = await bridgeJson(['info']); } catch { /* no bridge, no prompt */ return; }
  if (info && info.api_key) return;               // already configured
  await promptForApiKey();
}

const SETTING_KEYS = ['models', 'stepsPerModel', 'synthesize', 'useStudy', 'timeoutSeconds',
  'paidFallback', 'flintPath', 'python'];

async function settingsData(probeLocals = false) {
  const c = cfg();
  const values = Object.fromEntries(SETTING_KEYS.map((k) => [k, c.get(k)]));
  let providers = [];
  if (probeLocals) {
    const local = await bridgeJson(['local-models']).catch(() => ({ providers: [] }));
    modelProvidersCache = local.providers || [];
  }
  providers = modelProvidersCache;
  return {
    values,
    providerState: provState(),
    providers,
    keyConfigured: !!secretApiKey,
    keyStorage: secretApiKey ? 'Cursor encrypted secret storage' : null,
    project: path.basename(repoFor(currentEditor() && currentEditor().document.uri) || 'this workspace'),
  };
}

async function sendSettings(probeLocals = false) {
  panel.post({ type: 'settings', data: await settingsData(probeLocals) });
}

async function saveProjectSettings(values) {
  const c = cfg();
  const target = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length
    ? vscode.ConfigurationTarget.Workspace : vscode.ConfigurationTarget.Global;
  const clean = {
    models: Math.max(1, Math.min(6, Number(values.models) || 3)),
    stepsPerModel: Math.max(2, Math.min(20, Number(values.stepsPerModel) || 8)),
    timeoutSeconds: Math.max(60, Math.min(1800, Number(values.timeoutSeconds) || 300)),
    synthesize: !!values.synthesize,
    useStudy: !!values.useStudy,
    paidFallback: ['off', 'auto', 'always'].includes(values.paidFallback) ? values.paidFallback : 'off',
    flintPath: String(values.flintPath || '').trim(),
    python: String(values.python || '').trim(),
  };
  for (const [key, value] of Object.entries(clean)) {
    await c.update(key, value, target);
  }
  if (values.backend) await saveProvState(values.backend, values.model === '__auto__' ? null : values.model, values.customUrl);
  let quotaWarning = '';
  if (values.quotaChanged && ['shutdown', 'wait', 'paid'].includes(values.quotaMode)) {
    const repo = panelRepo({});
    if (repo) {
      try {
        await setQuotaMode(repo, values.quotaMode);
      } catch (e) {
        quotaWarning = `Workspace settings were saved, but the background-swarm policy was not changed: ${e.message}`;
      }
    }
  }
  panel.post(quotaWarning
    ? { type: 'settingsWarning', text: quotaWarning }
    : { type: 'settingsSaved', text: `Saved for ${path.basename(panelRepo({}) || 'this project')}.` });
  await sendSettings(false);
  lastInfo = 0;
  refreshStatus();
}

let refreshing = null;
function refreshStatus() {
  if (refreshing) return refreshing;
  const repo = repoFor(currentEditor() && currentEditor().document.uri);
  refreshing = (async () => {
    try {
      if (repo) {
        const s = await bridgeJson(['status', '--repo', repo]);
        // A $0 wallet and "paid requests are switched off" look identical in a meter, and
        // only one of them is a decision somebody made. The panel shows the setting itself.
        s.paid_fallback = cfg().get('paidFallback') || 'off';
        // The provider/model the user last picked for asks rides along with status so the
        // panel header can show it ("asking with Ollama · qwen2.5-coder:7b").
        s.ask_provider = provState().backend || 'openrouter';
        s.ask_model = provState().model || null;
        panel.post({ type: 'status', data: s });
        daemonRunning = !!s.daemon_running;
        try {
          panel.post({ type: 'parked', ...(await bridgeJson(['parked', '--repo', repo, '--limit', '20'])) });
        } catch { /* the queue tab still works without it */ }
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
      // Fresh-start gate: ask for an OpenRouter key once, when none is set and the
      // last-chosen backend needs one. Runs after the first status/info round so the
      // prompt appears on its own, not in the middle of the panel's first paint.
      await maybePromptForApiKey();
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
    // Whichever container the host gave us; focusing the absent one is a harmless no-op.
    for (const id of ['flintSwarm.panelSecondary', 'flintSwarm.panel']) {
      try { await vscode.commands.executeCommand(`${id}.focus`); } catch { /* not this one */ }
      if (this.view) break;
    }
    if (this.view) this.view.show(true);
  }

  html(webview, media) {
    const nonce = crypto.randomBytes(16).toString('base64');
    const uri = (f) => webview.asWebviewUri(vscode.Uri.joinPath(media, f));
    return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'nonce-${nonce}'; img-src ${webview.cspSource} data: https:; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="stylesheet" href="${uri('panel.css')}"></head>
<body>
<div id="skinBackdrop" aria-hidden="true"></div>
<div id="skinScanlines" aria-hidden="true"></div>
<header class="app-header">
  <div id="skinHeaderLayer" aria-hidden="true"></div>
  <div class="brand"><span class="brand-mark" aria-hidden="true">✦</span><span>Flint</span><span id="runState" class="status-dot" title="Swarm status"></span></div>
  <div class="header-actions"><button id="updateVersion" class="update-button" type="button" title="0 total update clicks — install this checkout and restart Cursor">Update · 0.0.0</button><button class="icon tab-jump" data-tab="skin" title="Dress it up" aria-label="Customize appearance">🎨</button><button class="icon tab-jump" data-tab="help" title="Setup guide" aria-label="Setup guide">?</button><button class="icon tab-jump" data-tab="settings" title="Settings" aria-label="Settings">⚙</button></div>
</header>
<div id="skinMarquee" class="marquee" hidden><span id="skinMarqueeText"></span></div>
<details id="statusDeck" class="status-deck">
  <summary><span>Project &amp; swarm status</span><span class="chevron">⌄</span></summary>
  <div id="status"></div>
  <div class="row run-controls"><button id="start">Start swarm</button><button id="stop" class="secondary" title="Kill the turn in flight">Stop now</button><button id="drain" class="secondary" title="Finish the task in flight, then stop">Stop after task</button><button id="restart" class="secondary" title="Drain, then start again on the same config">Restart</button><button id="report" class="secondary">Report</button></div>
</details>
<nav id="tabs" role="tablist" aria-label="Flint sections">
  <button class="tab" id="tab-ask" data-tab="ask" role="tab" aria-selected="true">Chat</button>
  <button class="tab" id="tab-queue" data-tab="queue" role="tab" aria-selected="false">Queue<span id="qcount" class="badge" hidden></span></button>
  <button class="tab" id="tab-landed" data-tab="landed" role="tab" aria-selected="false">Changes</button>
</nav>
<div id="notice" class="notice"></div>
<section id="pane-ask" role="tabpanel">
  <section id="threads"></section>
  <form id="askForm" class="composer">
    <div id="skinComposerLayer" aria-hidden="true"></div>
    <textarea id="question" rows="2" placeholder="Ask Flint to explain, review, or improve your code…"></textarea>
    <div class="context-row"><label class="context-pill"><input type="checkbox" id="withCode" checked><span aria-hidden="true">＋</span> Current selection</label></div>
    <div class="composer-footer">
      <div class="model-controls"><select id="providerSelect" aria-label="Model provider" title="Model provider"><option value="openrouter">OpenRouter</option></select><select id="modelSelect" aria-label="Model" title="Model"><option value="__auto__">Auto</option></select></div>
      <button type="submit" id="send" class="send" title="Send (Ctrl/⌘+Enter)" aria-label="Send">↑</button>
    </div>
    <input id="customEndpoint" class="custom-endpoint" type="url" placeholder="http://localhost:1234/v1" aria-label="Custom OpenAI-compatible endpoint" hidden>
    <input id="customModel" class="custom-endpoint" type="text" placeholder="Model id reported by that endpoint" aria-label="Custom endpoint model id" hidden>
    <div class="composer-tools"><button id="queue" type="button" class="tool-button">Queue as task</button></div>
  </form>
</section>
<section id="pane-queue" role="tabpanel" hidden>
  <div id="queueHead"></div>
  <div id="queueList"></div>
  <div class="row"><button id="board" class="secondary">Scrum board ↗</button><button id="completed" class="secondary">Verified completed ↗</button></div>
<div id="parkedHead"></div>
  <div id="parkedList"></div>
</section>
<section id="pane-landed" role="tabpanel" hidden>
  <div id="landedHead"></div>
  <div id="landedList"></div>
</section>
<section id="pane-settings" class="page" role="tabpanel" hidden>
  <div class="page-heading"><span class="eyebrow">PROJECT PREFERENCES</span><h2>Settings</h2><p>Your model and answer choices apply to this workspace. The background-swarm policy belongs to this repository.</p></div>
  <form id="settingsForm">
    <section class="settings-card"><h3>Appearance</h3><p class="setting-help">One sane, serious font control. For actual customization — colors, pictures, nonsense — see the 🎨 tab.</p>
      <label class="field"><span>Font</span><select id="skinFont">
        <option value="">Helvetica (default)</option>
        <option value="var(--vscode-font-family)">Match Cursor's editor font</option>
        <option value="Arial, sans-serif">Arial</option>
        <option value="Georgia, serif">Georgia</option>
        <option value="'Courier New', monospace">Courier New — hacker mode</option>
        <option value="'Comic Sans MS', cursive">Comic Sans MS — maximum trust</option>
        <option value="Papyrus, fantasy">Papyrus — ancient scrolls, modern swarm</option>
        <option value="Impact, sans-serif">Impact — meme lord</option>
        <option value="'Brush Script MT', cursive">Brush Script MT — wedding invitation</option>
        <option value="'Trebuchet MS', sans-serif">Trebuchet MS — early-internet clean</option>
      </select></label>
      <label class="field"><span>…or type any font family</span><input id="skinFontCustom" type="text" placeholder="e.g. 'Segoe Print', system-ui, Arial Black"></label>
    </section>
    <section class="settings-card"><h3>Model &amp; provider</h3><p class="setting-help">Choose where answers run. “Auto” lets Flint route across the configured free model pool.</p>
      <label class="field"><span>Provider</span><select id="settingProvider"><option value="openrouter">OpenRouter cloud</option><option value="ollama">Ollama</option><option value="lm-studio">LM Studio</option><option value="mlx">MLX</option><option value="llamacpp">llama.cpp</option><option value="__custom__">Custom endpoint</option></select></label>
      <label class="field" id="settingModelField"><span>Default model</span><select id="settingModel"><option value="__auto__">Auto — best available</option></select></label>
      <div class="custom-setting" id="customSetting" hidden><label class="field"><span>Custom endpoint</span><input id="settingCustomEndpoint" type="url" placeholder="http://localhost:1234/v1"></label><label class="field"><span>Model id</span><input id="settingCustomModel" type="text" placeholder="e.g. qwen2.5-coder"></label></div>
    </section>
    <section class="settings-card"><div class="setting-title"><div><h3>OpenRouter key</h3><p class="setting-help">Stored in Cursor’s encrypted SecretStorage and passed only to the local bridge process.</p></div><span id="keyStatus" class="state-pill">Not set</span></div>
      <label class="field"><span>API key</span><input id="apiKey" type="password" autocomplete="off" placeholder="sk-or-…"></label>
      <div class="row"><button id="saveKey" type="button">Save key securely</button><button id="clearKey" type="button" class="secondary">Forget saved key</button></div>
    </section>
    <details class="settings-card cost-card advanced-card"><summary><h3>Cost &amp; fallback<span id="costSummaryPill" class="state-pill">Free only</span></h3></summary><p class="setting-help">These are separate on purpose: one controls questions you ask in this panel; the other controls the unattended background swarm. Both are free-only by default — nothing here spends a cent until you change it.</p>
      <label class="policy-row"><span><b>Questions in this panel</b><small>What to do if free models cannot answer.</small></span><select id="settingPaid"><option value="off">Free only</option><option value="auto">Use paid only as rescue</option><option value="always">Use paid first</option></select></label>
      <label class="policy-row"><span><b>Background swarm at free limit</b><small>What the daemon should do after free requests run out.</small></span><select id="settingQuota"><option value="shutdown">Stop safely</option><option value="wait">Wait for daily reset</option><option value="paid">Continue with paid fallback</option></select></label>
      <div class="budget-row"><div><b>Paid-answer cap</b><small id="budgetSummary">Loading budget…</small></div><button id="changeBudget" type="button" class="secondary">Change cap</button></div>
      <div id="costWarning" class="cost-note"></div>
    </details>
    <section class="settings-card"><h3>Answer behavior</h3>
      <div class="field-grid"><label class="field"><span>Parallel models</span><input id="settingModels" type="number" min="1" max="6"></label><label class="field"><span>Tool rounds per model</span><input id="settingSteps" type="number" min="2" max="20"></label><label class="field"><span>Timeout (seconds)</span><input id="settingTimeout" type="number" min="60" max="1800"></label></div>
      <label class="switch-row"><span><b>Merge the answers</b><small>Ask one more model to check and synthesize the result.</small></span><input id="settingSynthesize" type="checkbox"></label>
      <label class="switch-row"><span><b>MIT study tools</b><small>Let models search the local OpenCourseWare corpus.</small></span><input id="settingStudy" type="checkbox"></label>
    </section>
    <section class="settings-card"><h3>Local installation</h3><label class="field"><span>Flint folder</span><input id="settingPath" type="text" placeholder="Auto-detect"></label><label class="field"><span>Python interpreter</span><input id="settingPython" type="text" placeholder=".venv/bin/python"></label></section>
    <div class="settings-save"><span id="settingsFeedback" class="dim"></span><button type="submit">Save project settings</button></div>
  </form>
</section>
<section id="pane-skin" class="page" role="tabpanel" hidden>
  <div class="page-heading"><span class="eyebrow">ZERO RESTRAINT, MAXIMUM VIBES</span><h2>Make it yours</h2><p>Early-2010s winamp-skin energy. Nobody else has to look at this panel. Go nuts.</p></div>
  <section class="settings-card"><h3>One-click vibes</h3><p class="setting-help">Each one sets everything below at once. You can still fiddle after.</p>
    <div id="skinPresets" class="skin-presets"></div>
  </section>
  <section class="settings-card"><h3>Background</h3><p class="setting-help">A picture from your machine, a link to one, or just a flat color. Blur it until nobody can tell what it is.</p>
    <div class="row"><button id="skinBgUpload" type="button">Upload a picture</button><button id="skinBgClear" type="button" class="secondary">Clear background</button></div>
    <label class="field"><span>…or paste an image URL</span>
      <div class="row"><input id="skinBgUrl" type="url" class="grow" placeholder="https://example.com/your-cat.png"><button id="skinBgUrlApply" type="button" class="secondary">Use it</button></div>
    </label>
    <div class="field-grid">
      <label class="field"><span>Fit</span><select id="skinBgFit"><option value="cover">Cover (crop to fill)</option><option value="contain">Contain (letterboxed)</option><option value="tile">Tile (repeat, chaotic)</option><option value="100% 100%">Stretch (ignore the aspect ratio, as God intended in 2009)</option></select></label>
      <label class="field"><span>Darken <span id="skinBgDarkVal"></span></span><input id="skinBgDark" type="range" min="0" max="90" step="5"></label>
    </div>
    <label class="field"><span>Blur <span id="skinBgBlurVal"></span></span><input id="skinBgBlur" type="range" min="0" max="20" step="1"></label>
  </section>
  <section class="settings-card"><h3>Header image</h3><p class="setting-help">The bar at the very top, behind the Flint logo. Same deal: picture, link, or skip it.</p>
    <div class="row"><button id="skinHeaderUpload" type="button">Upload a picture</button><button id="skinHeaderClear" type="button" class="secondary">Clear</button></div>
    <label class="field"><span>…or paste an image URL</span>
      <div class="row"><input id="skinHeaderUrl" type="url" class="grow" placeholder="https://example.com/banner.png"><button id="skinHeaderUrlApply" type="button" class="secondary">Use it</button></div>
    </label>
    <div class="field-grid">
      <label class="field"><span>Fit</span><select id="skinHeaderFit"><option value="cover">Cover</option><option value="contain">Contain</option><option value="tile">Tile</option><option value="100% 100%">Stretch</option></select></label>
      <label class="field"><span>Darken <span id="skinHeaderDarkVal"></span></span><input id="skinHeaderDark" type="range" min="0" max="90" step="5"></label>
    </div>
    <label class="field"><span>Blur <span id="skinHeaderBlurVal"></span></span><input id="skinHeaderBlur" type="range" min="0" max="20" step="1"></label>
  </section>
  <section class="settings-card"><h3>Chat composer background</h3><p class="setting-help">The box you actually type your prompt into. Put whatever's funniest behind your own cursor.</p>
    <div class="row"><button id="skinComposerUpload" type="button">Upload a picture</button><button id="skinComposerClear" type="button" class="secondary">Clear</button></div>
    <label class="field"><span>…or paste an image URL</span>
      <div class="row"><input id="skinComposerUrl" type="url" class="grow" placeholder="https://example.com/vibes.png"><button id="skinComposerUrlApply" type="button" class="secondary">Use it</button></div>
    </label>
    <div class="field-grid">
      <label class="field"><span>Fit</span><select id="skinComposerFit"><option value="cover">Cover</option><option value="contain">Contain</option><option value="tile">Tile</option><option value="100% 100%">Stretch</option></select></label>
      <label class="field"><span>Darken <span id="skinComposerDarkVal"></span></span><input id="skinComposerDark" type="range" min="0" max="90" step="5"></label>
    </div>
    <label class="field"><span>Blur <span id="skinComposerBlurVal"></span></span><input id="skinComposerBlur" type="range" min="0" max="20" step="1"></label>
  </section>
  <section class="settings-card"><h3>Colors</h3><p class="setting-help">Whatever you want. We will pick readable button text for you so it still works.</p>
    <div class="color-grid">
      <label class="color-field"><span>Background</span><input id="skinBg" type="color"></label>
      <label class="color-field"><span>Panels &amp; cards</span><input id="skinSurface" type="color"></label>
      <label class="color-field"><span>Text</span><input id="skinText" type="color"></label>
      <label class="color-field"><span>Accent / buttons</span><input id="skinAccent" type="color"></label>
      <label class="color-field"><span>Borders</span><input id="skinBorder" type="color"></label>
    </div>
  </section>
  <section class="settings-card"><h3>Shape</h3><p class="setting-help">Font lives in Settings now, with a sane default — this tab is just for colors, pictures, and nonsense.</p>
    <label class="field"><span>Corner roundness: brutalist ↔ bubbly <span id="skinRadiusVal"></span></span><input id="skinRadius" type="range" min="0" max="24" step="1"></label>
  </section>
  <section class="settings-card"><h3>Extras</h3><p class="setting-help">Within reason. Mostly.</p>
    <label class="switch-row"><span><b>Neon glow</b><small>Buttons and the active tab get a soft glow in your accent color.</small></span><input id="skinGlow" type="checkbox"></label>
    <label class="switch-row"><span><b>CRT scanlines</b><small>A faint scanline overlay, because the swarm is basically a terminal anyway.</small></span><input id="skinCrt" type="checkbox"></label>
    <label class="switch-row"><span><b>Scrolling marquee banner</b><small>Under the header, like it's 2006.</small></span><input id="skinMarqueeToggle" type="checkbox"></label>
    <div class="field" id="skinMarqueeTextField" hidden><span>Marquee text</span><input id="skinMarqueeTextInput" type="text" maxlength="200" placeholder="✦ WELCOME TO MY SWARM ✦"></div>
    <label class="switch-row"><span><b>🎉 Confetti when work lands</b><small>Fires once when a new commit lands on trunk while you have the Changes tab open.</small></span><input id="skinConfetti" type="checkbox"></label>
    <div class="row"><button id="skinConfettiTest" type="button" class="secondary">Test confetti</button></div>
  </section>
  <div class="settings-save"><span id="skinFeedback" class="dim"></span><button id="skinChaos" type="button" class="secondary">🎲 I'm feeling chaotic</button><button id="skinResetBtn" type="button">Reset to default</button></div>
</section>
<section id="pane-help" class="page guide" role="tabpanel" hidden>
  <div class="page-heading"><span class="eyebrow">ZERO TO WORKING</span><h2>Put Flint on any laptop</h2><p>No jargon, no cloud lock-in, and no mystery spending. You own the checkout, the key, and every commit.</p></div>
  <div class="guide-callout"><b>The short version</b><span>Install four ordinary tools, clone the repository, run one setup command, paste one API key, and reload Cursor.</span></div>
  <ol class="steps">
    <li><span class="step-number">1</span><div><h3>Install the basics</h3><p>Install <b>Cursor</b>, <b>Git</b>, <b>Python 3</b>, and the current <b>Node.js LTS</b>. On Windows, install <b>WSL with Ubuntu</b> and do the terminal steps inside Ubuntu. These are tools on your laptop—not subscriptions to Flint.</p></div></li>
    <li><span class="step-number">2</span><div><h3>Get an OpenRouter key</h3><p>Create your own OpenRouter account, make an API key, and copy it. A key is a password for models. Do not email it, paste it into source code, or commit it to Git.</p></div></li>
    <li><span class="step-number">3</span><div><h3>Download and install Flint</h3><p>Open a terminal, then run these commands one line at a time:</p><pre><code>git clone https://github.com/Devon-ODell/OpenRouterSwarm.git&#10;cd OpenRouterSwarm&#10;./setup.sh</code></pre><p>Run <code>./setup.sh</code> from inside the <code>OpenRouterSwarm</code> folder. The setup creates an isolated Python environment, runs the checks, packages the extension, and installs it into Cursor when Cursor’s command-line tool is available. If Flint is already set up and you only want to reinstall this extension, run <code>./cursor-extension/install.sh</code> from that same folder.</p></div></li>
    <li><span class="step-number">4</span><div><h3>Restart Cursor</h3><p>Fully quit Cursor and open it again, then look for Flint in the secondary sidebar. On macOS, quitting means <b>Cursor → Quit Cursor</b> or <b>Cmd+Q</b>, not merely closing the window. Open this Settings page and save your OpenRouter key.</p></div></li>
    <li><span class="step-number">5</span><div><h3>Use it on a project</h3><p>Open a Git project in Cursor. Put the cursor inside a function—or select exact lines—then ask a question below. Use <b>Queue as task</b> when you want tested work prepared on <code>swarm/trunk</code>. Flint never merges that branch into your main branch unless you do it.</p></div></li>
  </ol>
  <section class="settings-card"><h3>Moving to another laptop</h3><p>Repeat steps 1–4 on the new machine. Clone your project separately, open it in Cursor, and choose its project settings here. Keys are deliberately not copied with Git; save the key once on each laptop.</p></section>
  <section class="settings-card"><h3>What the buttons mean</h3><dl><dt>Ask</dt><dd>Read-only advice about the code in front of you.</dd><dt>Queue as task</dt><dd>Tested implementation work in a separate worktree.</dd><dt>Start swarm</dt><dd>Begin taking queued work until you stop it.</dd><dt>Changes</dt><dd>Commits that passed tests and adversarial review.</dd></dl></section>
  <section class="settings-card"><h3>Your control, plainly stated</h3><p>Cloud requests go only to the provider you choose. Local providers keep prompts on your own machine. Paid fallback is off unless you enable it, and its cap is visible. The daemon works in its own Git worktrees; you decide whether accepted commits ever reach <code>main</code>.</p></section>
</section>
<script nonce="${nonce}">window.__NONCE__=${JSON.stringify(nonce)};</script>
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
      sendSettings(true).catch((e) => this.post({ type: 'settingsError', text: e.message }));
      this.post({ type: 'skin', skin: this.context.globalState.get(SKIN_KEY, null) });
      const count = updateClicks(this.context);
      this.post({ type: 'updateVersion', count, version: updateVersion(count), busy: false });
    } else if (m.type === 'ask') {
      await ask(m.question, m.withCode ? await codeContext(currentEditor()) : null,
        { backend: m.backend, model: m.model, customUrl: m.customUrl });
    } else if (m.type === 'queue') {
      await queueTask(m.title, m.withCode ? await codeContext(currentEditor()) : null);
    } else if (m.type === 'vote') {
      bridgeJson(['vote', '--model', m.model, '--useful', m.useful ? '1' : '0']).catch((e) => vscode.window.showErrorMessage(e.message));
    } else if (m.type === 'open') {
      await openFile(m);
    } else if (m.type === 'start') {
      await startGrind();
    } else if (m.type === 'stop') {
      await stopGrind(m.repo, m.drain);
    } else if (m.type === 'restart') {
      await restartGrind(m.repo);
    } else if (m.type === 'landed') {
      await showLanded(m.repo);
    } else if (m.type === 'queueRetry') {
      await queueRetry(m);
    } else if (m.type === 'queueRequeue') {
      await queueRequeue(m);
    } else if (m.type === 'board' || m.type === 'completed') {
      await showBoard(m.type === 'completed');
    } else if (m.type === 'parkedDismiss') {
      await dismissParked(m);
    } else if (m.type === 'showCommit') {
      await showCommit(m);
    } else if (m.type === 'report') {
      await showReport();
    } else if (m.type === 'togglePaid') {
      await togglePaid(m.to);
    } else if (m.type === 'evidence') {
      await showEvidence(m);
    } else if (m.type === 'queueGet') {
      await queueGet(m);
    } else if (m.type === 'queueEdit') {
      await queueEdit(m);
    } else if (m.type === 'queueRemove') {
      await queueRemove(m);
    } else if (m.type === 'queueClear') {
      await clearQueue(m);
    } else if (m.type === 'activity') {
      await showActivity();
    } else if (m.type === 'budget') {
      await setBudget();
    } else if (m.type === 'setApiKey') {
      keyPrompted = false;
      await promptForApiKey('OpenRouter API key (sk-or-...) — stored in Cursor encrypted SecretStorage');
      lastInfo = 0;
      await refreshStatus();
    } else if (m.type === 'settingsSave') {
      try { await saveProjectSettings(m.values || {}); }
      catch (e) { panel.post({ type: 'settingsError', text: e.message }); }
    } else if (m.type === 'settingsKeySave') {
      try {
        const res = await storeApiKey(m.key);
        panel.post({ type: 'keySaved', storage: res.storage });
        lastInfo = 0;
        await refreshStatus();
      } catch (e) { panel.post({ type: 'settingsError', text: e.message }); }
    } else if (m.type === 'settingsKeyClear') {
      await clearStoredApiKey();
      panel.post({ type: 'keyCleared' });
      lastInfo = 0;
      await refreshStatus();
    } else if (m.type === 'settingsRefresh') {
      await sendSettings(true);
    } else if (m.type === 'refresh') {
      await refreshStatus();
    } else if (m.type === 'skinSave') {
      await this.context.globalState.update(SKIN_KEY, m.skin || null);
    } else if (m.type === 'skinReset') {
      await this.context.globalState.update(SKIN_KEY, undefined);
      this.post({ type: 'skin', skin: null });
    } else if (m.type === 'updateInstall') {
      await installExtensionUpdate(this.context, (message) => this.post(message));
    } else if (m.type === 'skinPickImage') {
      await this.pickSkinImage(m.target);
    }
  }

  // target says which layer this picture is for ('bgImage', 'headerImage', 'composerImage');
  // echoed straight back so the webview routes the result without having to track "what did I
  // last ask for" itself.
  async pickSkinImage(target) {
    try {
      const picked = await vscode.window.showOpenDialog({
        canSelectMany: false, openLabel: 'Use as background',
        filters: { Images: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'] },
      });
      if (!picked || !picked[0]) return;
      const file = picked[0].fsPath;
      const stat = await fs.promises.stat(file);
      // The whole thing round-trips through webview postMessage and globalState; a multi-megabyte
      // photo makes both sluggish, so cap it rather than silently stalling the panel.
      if (stat.size > 6 * 1024 * 1024) {
        this.post({ type: 'skinImageError', target, text: 'That image is over 6MB — pick something smaller, or a more reasonably-sized meme.' });
        return;
      }
      const ext = path.extname(file).slice(1).toLowerCase();
      const mime = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif',
        webp: 'image/webp', bmp: 'image/bmp', svg: 'image/svg+xml' }[ext] || 'application/octet-stream';
      const data = await fs.promises.readFile(file);
      this.post({ type: 'skinImageResult', target, dataUri: `data:${mime};base64,${data.toString('base64')}` });
    } catch (e) {
      this.post({ type: 'skinImageError', target, text: e.message });
    }
  }
}

// ---------------------------------------------------------------- lifecycle

// The secondary sidebar arrived in VS Code 1.106. Claude Code and Codex both contribute
// there and fall back to the activity bar below that version, keyed off a context flag they
// set themselves; Flint Swarm does the same so all three sit in the same sidebar.
function supportsSecondarySidebar(version) {
  const [major, minor] = String(version || '').split('.').map(Number);
  return (major || 0) > 1 || ((major || 0) === 1 && (minor || 0) >= 106);
}

function activate(context) {
  extensionContext = context;
  if (context.globalState && context.globalState.setKeysForSync) {
    context.globalState.setKeysForSync([UPDATE_CLICKS_KEY]);
  }
  if (context.secrets && context.secrets.get) {
    secretReady = Promise.resolve(context.secrets.get(SECRET_KEY))
      .then((key) => { secretApiKey = key || null; }).catch(() => {});
  }
  panel = new SwarmPanel(context);
  if (!supportsSecondarySidebar(vscode.version)) {
    vscode.commands.executeCommand('setContext', 'flintSwarm:doesNotSupportSecondarySidebar', true);
  }
  lastEditor = vscode.window.activeTextEditor || null;
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusItem.text = '$(organization) Swarm';
  statusItem.tooltip = 'Flint swarm: ask about the code you are working on';
  statusItem.command = 'flintSwarm.showPanel';
  statusItem.show();
  const reg = (id, fn) => context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  context.subscriptions.push(
    statusItem,
    // Only one of these two views exists at a time, decided by the context key above;
    // registering both means the panel works either way without a reload.
    vscode.window.registerWebviewViewProvider('flintSwarm.panel', panel, { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.window.registerWebviewViewProvider('flintSwarm.panelSecondary', panel, { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.window.onDidChangeActiveTextEditor((e) => { if (e && e.document.uri.scheme === 'file') lastEditor = e; }),
  );
  reg('flintSwarm.askSelection', cmdAsk);
  reg('flintSwarm.queueTask', cmdQueue);
  reg('flintSwarm.startGrind', () => startGrind());
  reg('flintSwarm.stopGrind', () => stopGrind());
  reg('flintSwarm.showPanel', () => panel.reveal());
  reg('flintSwarm.togglePaid', cmdTogglePaid);
  reg('flintSwarm.setQuotaMode', cmdSetQuotaMode);
  reg('flintSwarm.showEvidence', async () => {
    const repo = repoFor(currentEditor() && currentEditor().document.uri);
    const id = await vscode.window.showInputBox({ prompt: 'Swarm task id', placeHolder: 't1a2b3c4d5e6' });
    if (id) await showEvidence({ repo, id: id.trim() });
  });
  reg('flintSwarm.showActivity', () => showActivity());
  reg('flintSwarm.showReport', showReport);
  reg('flintSwarm.showQueue', showQueue);
  reg('flintSwarm.clearQueue', () => clearQueue());
  reg('flintSwarm.setBudget', setBudget);
  reg('flintSwarm.refresh', () => { lastInfo = 0; return refreshStatus(); });
  reg('flintSwarm.setApiKey', async () => {
    keyPrompted = false;   // the palette command may re-prompt even after a fresh-start skip
    await promptForApiKey('OpenRouter API key (sk-or-...) — stored in Cursor encrypted SecretStorage');
    lastInfo = 0;
    return refreshStatus();
  });
  reg('flintSwarm.showSettings', async () => {
    await panel.reveal();
    panel.post({ type: 'tab', tab: 'settings' });
    return sendSettings(true);
  });
  reg('flintSwarm.showHelp', async () => {
    await panel.reveal();
    panel.post({ type: 'tab', tab: 'help' });
  });
  reg('flintSwarm.showCustomize', async () => {
    await panel.reveal();
    panel.post({ type: 'tab', tab: 'skin' });
  });
  // While a daemon runs there is something new to show every few seconds — the round it is on,
  // the model, how long the turn has taken. When nothing runs, once a minute is plenty.
  let timer = null;
  const pace = () => {
    const every = daemonRunning ? 10 * 1000 : 60 * 1000;
    if (timer && timer.every === every) return;
    if (timer) clearInterval(timer.id);
    const id = setInterval(() => {
      if (panel.view && panel.view.visible) refreshStatus();
      pace();
    }, every);
    timer = { id, every };
  };
  pace();
  context.subscriptions.push({ dispose: () => timer && clearInterval(timer.id) });
  if (!flintRoot()) {
    vscode.window.showWarningMessage('Flint Swarm cannot find your flint checkout. Set "Flint Swarm: Flint Path" in Settings.', 'Open Settings')
      .then((a) => { if (a) vscode.commands.executeCommand('workbench.action.openSettings', 'flintSwarm.flintPath'); });
  }
}

function deactivate() {
  for (const child of running) child.kill('SIGTERM');
}

module.exports = { activate, deactivate,
  _test: { parseLines, innermost, flintRoot, where, bridgeJson, bridgeLast, PRESETS,
    idleFor, supportsSecondarySidebar, showActivity, providerChoices, provState, chosenBackend,
    providerArgs, promptForApiKey, maybePromptForApiKey, updateVersion,
    updateClicks, installExtensionUpdate, MAX_UPDATE_CLICKS, UPDATE_CLICKS_KEY } };
