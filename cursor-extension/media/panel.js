// Webview script for the Flint Swarm sidebar. Talks to extension.js by postMessage only.
// Two tabs: the swarm conversation, and the queue — where a task can be edited or dropped.
(function () {
  'use strict';
  const vscode = acquireVsCodeApi();
  const R = window.FlintRender;
  const saved = vscode.getState() || {};
  const threads = saved.threads || [];          // [{id, kind, question, where, repo, cards:{}, order:[], done}]
  let status = saved.status || null, info = saved.info || null;
  let landed = saved.landed || null, parked = saved.parked || null;
  let tab = saved.tab || 'ask';
  const open = new Set();                       // queue rows expanded to show their detail
  let editing = null;                           // {id, task} while a task is being rewritten
  let editError = null;

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const save = () => vscode.setState({ threads: threads.slice(-20), status, info, tab, landed, parked });
  // Cents are the unit that matters under a dollar: the first few questions cost fractions of one.
  const money = (n) => '$' + Number(n || 0).toFixed(Math.abs(Number(n) || 0) < 1 ? 4 : 2);

  /** "4m10s" for a duration in seconds. */
  function forHuman(seconds) { return idleFor(seconds); }

  /** A clock time from an epoch, for "back 20:00". */
  function atTime(epoch) {
    if (!epoch) return null;
    const d = new Date(epoch * 1000);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  }

  /** The first line: what the swarm is doing right now, in words. */
  function nowLine() {
    const now = status && status.now;
    if (!status) return '';
    if (!status.daemon_running) {
      return status.is_target
        ? '<span class="dim">○ Stopped.</span> Start it to work on this repository.'
        : '<span class="dim">○ No swarm is set up for this repository.</span>';
    }
    if (!now) return '<span class="ok">● Running</span> <span class="dim">· waiting for its first status line…</span>';
    if (now.run && ['Blocked', 'Draining', 'Waiting'].includes(now.run.state)) {
      const run = now.run;
      const blocker = (run.blockers || [])[0];
      return `<span class="warn">${R.esc(run.state)}</span> · ${R.esc(run.reason || '')}`
        + (run.state === 'Blocked' && blocker ? `<br>Owner: ${R.esc(blocker.owner || 'repository owner')} · Next: ${R.esc(blocker.recovery_action || '')}` : '');
    }
    if (now.run && now.run.improvement && now.run.improvement.phase === 'discovering') {
      return '<span class="ok">● Finding an improvement</span>';
    }
    const w = (now.workers || [])[0];
    if (!w) {
      if (now.idle && now.back) return `<span class="warn">◐ ${R.esc(now.idle)}</span> · first back ${R.esc(atTime(now.back))}`;
      if (now.idle) return `<span class="warn">◐ ${R.esc(now.idle)}</span>`;
      return '<span class="ok">● Running</span> <span class="dim">· between tasks</span>';
    }
    if (w.waiting) {
      return `<span class="warn">◐ ${R.esc(w.waiting)}</span>`
        + (now.back ? ` · back ${R.esc(atTime(now.back))}` : '');
    }
    const role = (w.role || 'working').replace(/^./, (ch) => ch.toUpperCase());
    const rounds = w.round ? ` · round ${w.round}/${w.rounds}` : '';
    return `<span class="ok">●</span> ${R.esc(role)} <b>"${R.esc(String(w.title).slice(0, 60))}"</b>`
      + (w.model ? ` · <span class="dim">${R.esc(w.model)}</span>` : '') + rounds
      + ` · ${forHuman(w.seconds)}`;
  }

  /** How the last 24 hours went. Red when few attempts accepted. */
  function healthLine() {
    const h = status && status.health;
    if (!h || !h.attempts) return '<span class="dim">No attempts in the last 24h.</span>';
    const rate = h.landed_rate == null ? '–' : `${Math.round(h.landed_rate * 100)}%`;
    const bits = [`${h.attempts} attempts · <b>${h.landed} accepted attempts</b> (${rate})`];
    if (h.failures && h.failures.length) {
      bits.push(h.failures.map(([k, n]) => `${R.esc(k)} ${n}`).join(' · '));
    }
    if (h.usd) {
      bits.push(`${money(h.usd)} spent`
        + (h.usd_per_landed ? ` · ${money(h.usd_per_landed)} per accepted attempt` : ''));
    }
    const cls = h.unhealthy ? 'err' : 'dim';
    return `<span class="${cls}">24h: ${bits.join(' — ')}</span>`
      + (h.unhealthy ? ' <span class="err">— few attempts accepted</span>' : '');
  }

  /** Which settings are in force. Red when the tuned file exists and was not loaded. */
  function configLine() {
    if (!status || !status.config_path) return '';
    const wrong = status.config_tuned_available && status.config_tuned_available !== status.config_path;
    const name = status.config_path.split('/').pop();
    return `<span class="${wrong ? 'err' : 'dim'}" title="${R.esc(status.config_path)}">`
      + `config: ${R.esc(name)} (${R.esc(status.config_kind || '?')})</span>`
      + (wrong ? ` <span class="err">— this repository has a tuned config that is NOT loaded</span>` : '');
  }

  function statusHtml() {
    if (!status && !info) return '<span class="dim">Loading swarm status…</span>';
    const bits = [];
    if (status) {
      const name = status.repo.split('/').pop();
      bits.push(nowLine());
      const run = (status.now && status.now.run) || status.run;
      if (run && run.id) {
        const left = run.deadline ? Math.max(0, run.deadline - Date.now() / 1000) : null;
        bits.push(`<span class="dim">Run ${R.esc(run.id.slice(0, 8))}`
          + (left == null ? ' · no shift deadline' : ` · ${forHuman(left)} remaining`)
          + (run.stop_reason ? ` · ${R.esc(run.stop_reason)}` : '') + '</span>');
        bits.push(`<span class="dim">Last tool/check progress: ${run.last_progress_at ? forHuman(Date.now() / 1000 - run.last_progress_at) + ' ago' : 'not recorded'}`
          + ` · Last accepted commit: ${run.last_accepted_commit ? R.esc(run.last_accepted_commit.slice(0, 8)) : 'none this run'}`
          + ` · Blocked: ${forHuman(run.blocked_duration || run.blocked_seconds || 0)}</span>`);
        const imp = run.improvement;
        if (imp && imp.enabled) {
          if (imp.selected) bits.push(`Improvement: ${R.esc(imp.selected.title)} · ${R.esc(imp.selected.benefit)}`);
          if (imp.next_scan) bits.push(`Next discovery: ${R.esc(atTime(imp.next_scan))}`);
        }
      }
      bits.push(healthLine());
      bits.push(`<b>${R.esc(name)}</b> · ${status.queue.length} queued · ${status.landed} closed as done (unverified)`
        + (status.trunk_ahead ? ` · trunk +${status.trunk_ahead}` : ''));
      bits.push(configLine());
      const b = status.budget;
      if (b) bits.push(`free requests <b>${b.spent_today}</b>/${b.usable} today, resets ${R.esc(b.resets_local)}`);
      const sp = status.spend;
      if (sp && sp.cap > 0) {          // a zero cap would draw an Infinity% meter
        const spent = sp.left != null && sp.left <= 0;
        bits.push(`<span class="meter" title="${money(sp.used)} of ${money(sp.cap)}">`
          + `<span class="meter-fill${spent ? ' full' : ''}" style="width:${Math.min(100, (sp.used / sp.cap) * 100).toFixed(1)}%"></span></span>`
          + ` $ spent this month (OpenRouter-reported) <b>${money(sp.used)}</b> of ${money(sp.cap)} cap`
          + (sp.shared ? ' this month' : ' today')
          + (sp.resets ? ` <span class="dim">· back ${R.esc(sp.resets)}</span>` : '')
          + (spent ? ' <span class="err">— spent</span>' : ''));
      }
      const w = status.wallet;
      if (w) {
        bits.push(`editor wallet: recorded <b>${money(w.spent)}</b> of ${money(w.cap)} cap` +
          (w.remaining <= 0 ? ' <span class="err">— spent</span>' : '') +
          ` <button class="icon" id="budget" title="Change the paid budget">✎</button>`);
      } else bits.push('<span class="dim">paid models off</span> <button class="icon" id="budget" title="Set a paid budget">✎</button>');
    }
    if (info && info.provider_usage) {
      const u = info.provider_usage;
      const amount = n => typeof n === 'number' && Number.isFinite(n) ? money(n) : 'unavailable';
      bits.push(u.available
        ? `OpenRouter key usage: <b>${amount(u.usage_daily)}</b> today · <b>${amount(u.usage_monthly)}</b> calendar month`
          + ` <span class="dim">(all activity on this key; checked ${R.esc(u.checked_at)})</span>`
        : '<span class="dim">OpenRouter key usage unavailable — local ledger is not verification</span>');
    }
    if (info && info.quota) bits.push(`OpenRouter free requests ${info.quota.used}/${info.quota.limit}`);
    // The corpus chunk count and the experiment arms are only worth a line while an experiment
    // is actually running; otherwise they are two numbers nobody is going to act on.
    const experimenting = info && info.mit_experiment && info.mit_experiment.enabled;
    if (experimenting && info.corpus) bits.push(`MIT corpus ${info.corpus.chunks.toLocaleString()} chunks`);
    if (experimenting && status && status.experiment) bits.push(`MIT experiment: ${status.experiment.on} with / ${status.experiment.off} without`);
    return bits.filter(Boolean).map((b) => `<div>${b}</div>`).join('');
  }

  /** "4m12s" — the same shape the activity tab prints, so the two agree. */
  function idleFor(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}m${String(s % 60).padStart(2, '0')}s`
      : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`;
  }

  function renderStatus() {
    $('status').innerHTML = statusHtml();
    const running = status && status.daemon_running;
    $('start').disabled = !!running;
    $('stop').disabled = !running;
    $('drain').disabled = !running;
    $('restart').disabled = !running;
    $('improveIdle').checked = !!(status && status.improvement && status.improvement.enabled);
    $('improveIdle').disabled = !status;
    const recent = (status && status.recent) || [];
    const age = status && status.stale_seconds;
    const quiet = running && age != null
      ? `<div class="quiet${age >= 600 ? ' warn' : ''}">last log write ${idleFor(age)} ago`
        + `<button class="icon" id="activity" title="Watch the activity log">log&nbsp;↗</button></div>`
      : '';
    $('recent').innerHTML = (recent.length
      ? recent.slice(-6).reverse().map((r) => `<div>${R.esc(r)}</div>`).join('') : '') + quiet;
    const n = status ? status.queue.length : 0;
    const badge = $('qcount');
    badge.textContent = n ? String(n) : '';
    badge.hidden = !n;
  }

  // ---------------------------------------------------------------- tabs

  function renderTab() {
    for (const name of ['ask', 'queue', 'landed']) {
      $('pane-' + name).hidden = tab !== name;
      const t = $('tab-' + name);
      t.classList.toggle('active', tab === name);
      t.setAttribute('aria-selected', tab === name ? 'true' : 'false');
    }
    if (tab === 'queue') { renderQueue(); renderParked(); }
    if (tab === 'landed') renderLanded();
  }

  function showTab(name) {
    if (tab === name) return;
    tab = name;
    renderTab();
    save();
    if (name === 'queue') vscode.postMessage({ type: 'refresh' });
    if (name === 'landed') vscode.postMessage({ type: 'landed', repo: status && status.repo });
  }

  // ---------------------------------------------------------------- landed

  function landedRow(cmt) {
    const bits = [cmt.model ? `<span class="dim">${R.esc(cmt.model)}</span>` : null,
      cmt.reward != null ? `reward <b>${Number(cmt.reward).toFixed(2)}</b>` : null,
      cmt.goal_item ? `goal item ${cmt.goal_item}` : null,
      cmt.usd ? money(cmt.usd) : null].filter(Boolean).join(' · ');
    const row = el('div', 'qrow');
    row.dataset.sha = cmt.sha;
    row.appendChild(el('div', 'qmain',
      `<span class="qtitle">${R.esc(cmt.subject)}</span>` +
      `<span class="qmeta"><code>${R.esc(cmt.short)}</code> · ${bits}</span>`));
    row.appendChild(el('div', 'qactions',
      `<button class="icon lshow" title="Open this commit as a diff">diff</button>`));
    return row;
  }

  function renderLanded() {
    const head = $('landedHead'), list = $('landedList');
    if (!landed) {
      head.innerHTML = '<span class="dim">Loading integrated commits…</span>';
      list.innerHTML = '';
      return;
    }
    head.innerHTML = `<div class="qhead"><span>${landed.length} commit(s) on <b>`
      + `${R.esc(String(status && status.trunk || 'trunk'))}</b></span></div>`;
    head.innerHTML += '<p class="dim">Integrated into swarm/trunk; this does not prove the full task is Done or deployed. Latest 20 commits.</p>';
    list.innerHTML = '';
    if (!landed.length) {
      list.innerHTML = '<p class="dim">No integrated commits found.</p>';
      return;
    }
    for (const cmt of landed) list.appendChild(landedRow(cmt));
  }

  // ---------------------------------------------------------------- queue

  function waitFor(t) {
    const secs = Math.round((t.not_before || 0) - Date.now() / 1000);
    if (secs <= 0) return null;
    return secs < 90 ? `retrying in ${secs}s` : `retrying in ${Math.round(secs / 60)}m`;
  }

  function queueMeta(t) {
    const bits = [R.esc(t.kind)];
    if (t.origin) bits.push(R.esc(t.origin));
    if (t.attempts) bits.push(`${t.attempts} attempt${t.attempts > 1 ? 's' : ''}`);
    // A harness failure cost the task no attempt, so say so rather than leaving it looking clean.
    if (t.harness_failures) bits.push(`<span class="warn">${t.harness_failures} harness failure${t.harness_failures > 1 ? 's' : ''}</span>`);
    if (t.failure_class) bits.push(`last failure: <b>${R.esc(t.failure_class)}</b>`);
    if (t.allow_test_changes) bits.push('<span class="tag">may change tests</span>');
    if (t.depends_on && t.depends_on.length) bits.push(`waits for ${t.depends_on.length}`);
    if (t.blocks && t.blocks.length) bits.push(`blocks ${t.blocks.length}`);
    const wait = waitFor(t);
    if (t.claimed) bits.push('<span class="ok">running now</span>');
    else if (wait) bits.push(`<span class="warn">${wait}</span>`);
    return bits.join(' · ');
  }

  function queueRow(t) {
    const row = el('div', 'qrow' + (t.claimed ? ' claimed' : ''));
    row.dataset.id = t.id;
    const prio = ['later', 'next', 'first'][t.priority] || 'next';
    row.appendChild(el('div', 'qmain',
      `<span class="prio p${t.priority}" title="priority ${t.priority} — ${prio}">P${t.priority}</span>` +
      `<span class="qtitle">${R.esc(t.title)}</span>` +
      `<span class="qmeta">${queueMeta(t)}</span>`));
    row.appendChild(el('div', 'qactions',
      (waitFor(t) ? `<button class="icon qretry" title="Try this task now, without waiting out the backoff">↻</button>` : '') +
      `<button class="icon qedit" title="Edit this task">✎</button>` +
      `<button class="icon qdel" title="Remove this task from the queue">✕</button>`));
    if (open.has(t.id)) {
      const parts = [];
      if (t.detail) parts.push(`<div class="md">${R.render(t.detail)}${t.detail_truncated ? '<p class="dim">…</p>' : ''}</div>`);
      if (t.acceptance && t.acceptance.length) {
        parts.push('<div class="accept"><b>Done when</b><ul>' +
          t.acceptance.map((a) => `<li>${R.esc(a)}</li>`).join('') + '</ul></div>');
      }
      if (t.note) parts.push(`<div class="note">last note: ${R.esc(String(t.note).slice(0, 400))}</div>`);
      if (t.blocks && t.blocks.length) {
        parts.push(`<div class="note">other tasks waiting on this one: ${t.blocks.map((b) => R.esc(b.title)).join(', ')}</div>`);
      }
      row.appendChild(el('div', 'qdetail', parts.join('') || '<span class="dim">No detail.</span>'));
    }
    return row;
  }

  function editForm(t) {
    const kinds = (status && status.kinds) || ['feature', 'bugfix', 'test', 'refactor'];
    const form = el('form', 'qform');
    form.dataset.id = t.id;
    form.innerHTML =
      `<input class="f-title" value="${R.esc(t.title)}" placeholder="Task title" aria-label="Task title">` +
      `<textarea class="f-detail" placeholder="What the swarm should do" aria-label="Detail">${R.esc(t.detail || '')}</textarea>` +
      `<textarea class="f-accept" placeholder="Done when… (one per line)" aria-label="Acceptance criteria">${R.esc((t.acceptance || []).join('\n'))}</textarea>` +
      `<div class="row">` +
      `<select class="f-kind" aria-label="Kind">${kinds.map((k) => `<option value="${R.esc(k)}"${k === t.kind ? ' selected' : ''}>${R.esc(k)}</option>`).join('')}</select>` +
      `<select class="f-prio" aria-label="Priority">${[0, 1, 2].map((v) => `<option value="${v}"${v === t.priority ? ' selected' : ''}>P${v} — ${['later', 'next', 'first'][v]}</option>`).join('')}</select>` +
      `<button type="submit">Save</button><button type="button" class="secondary f-cancel">Cancel</button></div>` +
      (editError ? `<div class="notice error">${R.esc(editError)}</div>` : '');
    return form;
  }

  function renderQueue() {
    const head = $('queueHead'), list = $('queueList');
    const tasks = (status && status.queue) || [];
    const max = (status && status.max_queue) || 20;
    head.innerHTML = status
      ? `<div class="qhead"><span>${tasks.length} of ${max} queued for <b>${R.esc(status.repo.split('/').pop())}</b></span>` +
        `<button id="qclear" class="secondary"${tasks.length ? '' : ' disabled'}>Clear all</button></div>`
      : '<span class="dim">Loading the queue…</span>';
    list.innerHTML = '';
    if (!tasks.length) {
      list.innerHTML = status
        ? '<p class="dim">Nothing queued. Select code and use <b>Queue as task</b>, or let the planner fill the queue while the swarm runs.</p>'
        : '';
      return;
    }
    const order = tasks.slice().sort((a, b) =>
      (b.claimed - a.claimed) || (b.priority - a.priority) || (a.created - b.created));
    for (const t of order) {
      list.appendChild(editing && editing.id === t.id ? editForm(editing.task) : queueRow(t));
    }
  }

  function renderParked() {
    const head = $('parkedHead'), list = $('parkedList');
    const rows = parked || [];
    head.innerHTML = rows.length
      ? `<div class="qhead"><span>${rows.length} split or parked — the swarm gave these up</span></div>` : '';
    list.innerHTML = '';
    for (const t of rows) {
      const row = el('div', 'qrow');
      row.dataset.id = t.id;
      const bits = [R.esc(t.status)];
      if (t.failure_class) bits.push(`<b>${R.esc(t.failure_class)}</b>`);
      if (t.stage) bits.push(R.esc(t.stage));
      if (t.why) bits.push(R.esc(String(t.why).slice(0, 120)));
      row.appendChild(el('div', 'qmain',
        `<span class="qtitle">${R.esc(t.title)}</span><span class="qmeta">${bits.join(' · ')}</span>`));
      row.appendChild(el('div', 'qactions',
        `<button class="icon prequeue" title="Put this task back in the queue, attempts cleared">↻</button>`
        + (t.handoff ? `<button class="icon pwhy" data-path="${R.esc(t.handoff)}" title="Open the handoff for this attempt">why</button>` : '')));
      list.appendChild(row);
    }
  }

  // ---------------------------------------------------------------- threads

  function cardHtml(c) {
    if (c.kind === 'hit') {
      const where = [c.hit.course, c.hit.lecture || c.hit.document, c.hit.page ? 'p. ' + c.hit.page : null].filter(Boolean).join(' · ');
      return `<div class="meta"><span class="tag">${R.esc(c.hit.kind)}</span> ${R.esc(where)}
        <a href="#" class="file" data-path="${R.esc(c.hit.path)}"${c.hit.page ? ` data-page="${c.hit.page}"` : ''}>open</a></div>
        <div class="md">${R.render(c.hit.text.slice(0, 900))}</div>`;
    }
    const m = R.esc(c.model || '');
    const paid = c.paid ? '<span class="tag paid" title="a paid model, charged to the extension budget">paid</span> ' : '';
    if (c.state === 'pending') return `<div class="meta">${paid}<span class="spin"></span> ${m}${c.role === 'synthesis' ? ' is checking and merging the answers…' : ' is reading your code…'}` +
      (c.progress ? ` <span class="dim">${R.esc(c.progress)}</span>` : '') + '</div>';
    if (c.state === 'error') return `<div class="meta err">${paid}${m}: ${R.esc(c.error || 'failed')}</div>`;
    const facts = [c.secs != null ? c.secs + 's' : null, c.requests ? c.requests + ' requests' : null,
      c.tool_calls ? c.tool_calls + ' tool calls' : null, c.study_calls ? `<b>${c.study_calls} MIT lookups</b>` : null,
      c.usd ? money(c.usd) : null].filter(Boolean).join(' · ');
    const vote = c.role === 'synthesis' ? '' :
      `<span class="vote">${c.voted ? (c.voted > 0 ? '👍 noted' : '👎 noted') :
        `<button class="icon" data-vote="1" data-model="${m}" title="Useful: this model gets picked more">👍</button><button class="icon" data-vote="0" data-model="${m}" title="Not useful">👎</button>`}</span>`;
    return `<div class="meta">${c.role === 'synthesis' ? '<span class="tag">merged</span> ' : ''}${paid}<b>${m}</b> ${facts}${vote}</div>
      <div class="md">${R.render(c.text)}</div>`;
  }

  function renderThread(t) {
    const node = el('article', 'thread');
    node.dataset.id = t.id;
    node.dataset.repo = t.repo || '';
    const head = t.kind === 'study' ? `📚 MIT lookup: ${R.esc(t.question)}` : R.esc(t.question);
    node.appendChild(el('div', 'q', `${head}${t.where ? ` <span class="where">${R.esc(t.where)}</span>` : ''}` +
      `<button class="icon close" title="Remove">✕</button>`));
    const ordered = t.order.map((k) => t.cards[k]);
    ordered.sort((a, b) => (b.role === 'synthesis') - (a.role === 'synthesis'));
    for (const c of ordered) node.appendChild(el('div', 'card' + (c.role === 'synthesis' ? ' synthesis' : '') + (c.state === 'error' ? ' failed' : ''), cardHtml(c)));
    if (t.note) node.appendChild(el('div', 'note', R.esc(t.note)));
    return node;
  }

  function renderThreads() {
    const box = $('threads');
    box.innerHTML = '';
    for (const t of threads.slice().reverse()) box.appendChild(renderThread(t));
    if (!threads.length) box.innerHTML = '<p class="dim">Select code (or just put the cursor in a function), then ask. Every answer comes from a different free model; one more model checks them against your code and merges them.</p>';
  }

  function thread(id) { return threads.find((t) => t.id === id); }

  function onAskEvent(t, ev) {
    const key = (ev.role === 'synthesis' ? 'synthesis:' : '') + (ev.model || ev.event);
    const put = (card) => { if (!t.cards[key]) t.order.push(key); t.cards[key] = Object.assign(t.cards[key] || {}, card); };
    if (ev.event === 'context') { t.where = ev.where || t.where; t.note = ev.study ? 'Models can search the MIT OpenCourseWare corpus.' : ''; }
    else if (ev.event === 'start') put({ model: ev.model, role: ev.role, state: 'pending', paid: !!ev.paid });
    else if (ev.event === 'progress') { if (t.cards[key] && t.cards[key].state === 'pending') t.cards[key].progress = ev.text; }
    else if (ev.event === 'answer' || ev.event === 'synthesis') put(Object.assign({}, ev, { state: 'done', role: ev.event === 'synthesis' ? 'synthesis' : ev.role }));
    else if (ev.event === 'rescue') {
      t.note = (ev.models || []).length
        ? `No free model answered — paying for ${ev.models.join(', ')} from the extension budget.`
        : `No free model answered, and no paid model could be used: ${ev.why || 'the budget is spent'}.`;
    } else if (ev.event === 'error') {
      if (ev.model) put({ model: ev.model, role: ev.role, state: 'error', error: ev.error, paid: !!ev.paid });
      else t.note = ev.error;
    } else if (ev.event === 'done') {
      t.done = true;
      t.note = `${ev.answered}/${ev.asked} models answered` + (ev.requests ? ` · ${ev.requests} free requests` : '')
        + (ev.usd ? ` · ${money(ev.usd)} of the paid budget` : '');
    }
  }

  // ---------------------------------------------------------------- messages

  window.addEventListener('message', (e) => {
    const msg = e.data;
    if (msg.type === 'status') {
      status = msg.data;
      // A task that was rewritten or removed elsewhere must not stay open in a stale form.
      if (editing && !status.queue.some((t) => t.id === editing.id)) editing = null;
      renderStatus();
      if (tab === 'queue') { renderQueue(); renderParked(); }
    } else if (msg.type === 'info') { info = msg.data; renderStatus(); }
    else if (msg.type === 'landed') { landed = msg.commits || []; save(); if (tab === 'landed') renderLanded(); }
    else if (msg.type === 'parked') { parked = msg.parked || []; save(); if (tab === 'queue') renderParked(); }
    else if (msg.type === 'tab') { tab = ['queue', 'landed'].includes(msg.tab) ? msg.tab : 'ask'; renderTab(); }
    else if (msg.type === 'queueTask') { editing = { id: msg.task.id, task: msg.task }; editError = null; showTab('queue'); renderQueue(); }
    else if (msg.type === 'queueSaved') { editing = null; editError = null; renderQueue(); }
    else if (msg.type === 'queueError') { editError = msg.error; renderQueue(); }
    else if (msg.type === 'askStart') { threads.push({ id: msg.id, kind: 'ask', question: msg.question, where: msg.where, repo: msg.repo, cards: {}, order: [] }); showTab('ask'); renderThreads(); }
    else if (msg.type === 'askEvent') { const t = thread(msg.id); if (t) { onAskEvent(t, msg.event); renderThreads(); } }
    else if (msg.type === 'study') {
      const t = { id: msg.id, kind: 'study', question: msg.query, cards: {}, order: [], done: true };
      (msg.hits || []).forEach((h, i) => { t.cards['h' + i] = { kind: 'hit', hit: h }; t.order.push('h' + i); });
      if (!(msg.hits || []).length) t.note = 'No matches in the MIT corpus.';
      threads.push(t); showTab('ask'); renderThreads();
    } else if (msg.type === 'notice') { $('notice').textContent = msg.text; $('notice').className = 'notice ' + (msg.level || ''); }
    else if (msg.type === 'prefill') { $('question').value = msg.question || ''; $('question').focus(); }
    save();
  });

  // ---------------------------------------------------------------- clicks

  function queued(id) { return ((status && status.queue) || []).find((t) => t.id === id); }

  document.addEventListener('click', (e) => {
    const tabBtn = e.target.closest('.tab');
    if (tabBtn) { showTab(tabBtn.dataset.tab); return; }
    if (e.target.closest('#board')) { vscode.postMessage({ type: 'board' }); return; }
    if (e.target.closest('#completed')) { vscode.postMessage({ type: 'completed' }); return; }
    if (e.target.closest('#activity')) { vscode.postMessage({ type: 'activity' }); return; }
    if (e.target.closest('#budget')) { vscode.postMessage({ type: 'budget' }); return; }
    if (e.target.closest('#qclear')) {
      vscode.postMessage({ type: 'queueClear', repo: status && status.repo });
      return;
    }
    const landedRowEl = e.target.closest('#landedList .qrow');
    if (landedRowEl) {
      vscode.postMessage({ type: 'showCommit', sha: landedRowEl.dataset.sha, repo: status && status.repo });
      return;
    }
    const parkedRowEl = e.target.closest('#parkedList .qrow');
    if (parkedRowEl) {
      const why = e.target.closest('.pwhy');
      if (why) { vscode.postMessage({ type: 'open', path: why.dataset.path, repo: status && status.repo }); return; }
      if (e.target.closest('.prequeue')) {
        vscode.postMessage({ type: 'queueRequeue', id: parkedRowEl.dataset.id, repo: status && status.repo });
        return;
      }
      return;
    }
    const row = e.target.closest('.qrow');
    if (row) {
      const id = row.dataset.id, t = queued(id);
      if (e.target.closest('.qretry')) {
        vscode.postMessage({ type: 'queueRetry', id, repo: status && status.repo });
        return;
      }
      if (e.target.closest('.qdel')) {
        vscode.postMessage({ type: 'queueRemove', id, repo: status && status.repo,
          title: t ? t.title : '', claimed: !!(t && t.claimed), blocks: (t && t.blocks) || [] });
        return;
      }
      if (e.target.closest('.qedit')) {
        editError = null;
        vscode.postMessage({ type: 'queueGet', id, repo: status && status.repo });
        return;
      }
      if (open.has(id)) open.delete(id); else open.add(id);
      renderQueue();
      return;
    }
    if (e.target.closest('.f-cancel')) { editing = null; editError = null; renderQueue(); return; }
    const a = e.target.closest('a.file');
    if (a) {
      e.preventDefault();
      const th = e.target.closest('.thread');
      vscode.postMessage({ type: 'open', path: a.dataset.path, line: a.dataset.line ? +a.dataset.line : null,
        page: a.dataset.page ? +a.dataset.page : null, repo: th ? th.dataset.repo : '' });
      return;
    }
    const v = e.target.closest('button[data-vote]');
    if (v) {
      const th = e.target.closest('.thread');
      const t = thread(th.dataset.id);
      const card = Object.values(t.cards).find((c) => c.model === v.dataset.model && c.role !== 'synthesis');
      if (card) card.voted = v.dataset.vote === '1' ? 1 : -1;
      vscode.postMessage({ type: 'vote', model: v.dataset.model, useful: v.dataset.vote === '1' });
      renderThreads(); save();
      return;
    }
    if (e.target.closest('button.close')) {
      const th = e.target.closest('.thread');
      const i = threads.findIndex((t) => t.id === th.dataset.id);
      if (i >= 0) threads.splice(i, 1);
      renderThreads(); save();
    }
  });

  document.addEventListener('submit', (e) => {
    const form = e.target.closest('.qform');
    if (!form) return;
    e.preventDefault();
    const val = (cls) => form.querySelector(cls).value;
    const title = val('.f-title').trim();
    if (!title) { form.querySelector('.f-title').focus(); return; }
    vscode.postMessage({ type: 'queueEdit', id: form.dataset.id, repo: status && status.repo,
      title, detail: val('.f-detail').trim(), kind: val('.f-kind'), priority: +val('.f-prio'),
      acceptance: val('.f-accept').split('\n').map((l) => l.trim()).filter(Boolean) });
  });

  $('askForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = $('question').value.trim();
    if (!q) { $('question').focus(); return; }
    vscode.postMessage({ type: 'ask', question: q, withCode: $('withCode').checked });
  });
  $('question').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); $('askForm').requestSubmit(); }
  });
  $('queue').addEventListener('click', () => vscode.postMessage({ type: 'queue', title: $('question').value.trim(), withCode: $('withCode').checked }));
  $('lookup').addEventListener('click', () => vscode.postMessage({ type: 'study', query: $('question').value.trim(), withCode: $('withCode').checked }));
  $('start').addEventListener('click', () => vscode.postMessage({ type: 'start' }));
  $('improveIdle').addEventListener('change', () => vscode.postMessage({ type: 'improvement', repo: status && status.repo, enabled: $('improveIdle').checked }));
  $('drain').addEventListener('click', () => vscode.postMessage({ type: 'stop', repo: status && status.repo, drain: true }));
  $('restart').addEventListener('click', () => vscode.postMessage({ type: 'restart', repo: status && status.repo }));
  // Name the repository the panel is showing: stop means this swarm, not every swarm on the machine.
  $('stop').addEventListener('click', () => vscode.postMessage({ type: 'stop', repo: status && status.repo }));
  $('report').addEventListener('click', () => vscode.postMessage({ type: 'report' }));

  renderStatus();
  renderThreads();
  renderTab();
  vscode.postMessage({ type: 'ready' });
})();
