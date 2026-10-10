// Webview script for the Flint Swarm sidebar. Talks to extension.js by postMessage only.
// Codex-shaped conversation surface plus queue, change history, project settings and setup guide.
(function () {
  'use strict';
  const vscode = acquireVsCodeApi();
  const R = window.FlintRender;
  const saved = vscode.getState() || {};
  const threads = saved.threads || [];          // [{id, kind, question, where, repo, cards:{}, order:[], done}]
  let status = saved.status || null, info = saved.info || null;
  let landed = saved.landed || null, parked = saved.parked || null;
  let settings = saved.settings || null;
  let skin = saved.skin || null;                // custom look: colors, background, fonts, fx
  let updateState = saved.updateState || { count: 0, version: '0.0.0', busy: false };
  let tab = saved.tab || 'ask';
  const open = new Set();                       // queue rows expanded to show their detail
  let editing = null;                           // {id, task} while a task is being rewritten
  let editError = null;

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const save = () => vscode.setState({ threads: threads.slice(-20), status, info, tab, landed, parked, settings, skin, updateState });
  // Cents are the unit that matters under a dollar: the first few questions cost fractions of one.
  const money = (n) => '$' + Number(n || 0).toFixed(Math.abs(Number(n) || 0) < 1 ? 4 : 2);

  function renderUpdateVersion(message) {
    updateState = Object.assign({}, updateState, message);
    const button = $('updateVersion');
    button.disabled = !!updateState.busy;
    button.textContent = updateState.busy
      ? `Updating ${updateState.version}…`
      : `Update · ${updateState.version}`;
    button.title = `${Number(updateState.count || 0).toLocaleString()} total update clicks — install this checkout and restart Cursor`;
    if (updateState.error) {
      $('notice').textContent = `Update ${updateState.version} failed: ${updateState.error}`;
      $('notice').className = 'notice error';
    }
  }

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
      return `<span class="warn">${R.esc(now.run.state)}</span> · ${R.esc(now.run.reason || '')}`;
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

  /** How the last 24 hours went. Red when almost nothing is landing. */
  function healthLine() {
    const h = status && status.health;
    if (!h || !h.attempts) return '<span class="dim">No attempts in the last 24h.</span>';
    const rate = h.landed_rate == null ? '–' : `${Math.round(h.landed_rate * 100)}%`;
    const bits = [`${h.attempts} attempts · <b>${h.landed} landed</b> (${rate})`];
    if (h.failures && h.failures.length) {
      bits.push(h.failures.map(([k, n]) => `${R.esc(k)} ${n}`).join(' · '));
    }
    if (h.usd) {
      bits.push(`${money(h.usd)} spent`
        + (h.usd_per_landed ? ` · ${money(h.usd_per_landed)} per landed commit` : ''));
    }
    const cls = h.unhealthy ? 'err' : 'dim';
    return `<span class="${cls}">24h: ${bits.join(' — ')}</span>`
      + (h.unhealthy ? ' <span class="err">— almost nothing is landing</span>' : '');
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
        bits.push(`<span class="dim">Run ${R.esc(run.id.slice(0, 8))} · ${R.esc(run.state || '')}`
          + (run.deadline ? ` · ${forHuman(Math.max(0, run.deadline - Date.now() / 1000))} remaining` : '') + '</span>');
        bits.push(`<span class="dim">Last tool/check progress: ${run.last_progress_at ? forHuman(Date.now() / 1000 - run.last_progress_at) + ' ago' : 'not recorded'}`
          + ` · Last accepted commit: ${run.last_accepted_commit ? R.esc(run.last_accepted_commit.slice(0, 8)) : 'none this run'}</span>`);
      }
      bits.push(healthLine());
      bits.push(`<b>${R.esc(name)}</b> · ${status.queue.length} queued · ${status.landed} landed`
        + (status.trunk_ahead ? ` · trunk +${status.trunk_ahead}` : ''));
      bits.push(configLine());
      const b = status.budget;
      if (b) bits.push(`free requests <b>${b.spent_today}</b>/${b.usable} today, resets ${R.esc(b.resets_local)}`);
      const sp = status.spend;
      if (sp && sp.cap > 0) {          // a zero cap would draw an Infinity% meter
        const spent = sp.left != null && sp.left <= 0;
        bits.push(`<span class="meter" title="${money(sp.used)} of ${money(sp.cap)}">`
          + `<span class="meter-fill${spent ? ' full' : ''}" style="width:${Math.min(100, (sp.used / sp.cap) * 100).toFixed(1)}%"></span></span>`
          + ` background budget <b>${money(sp.used)} / ${money(sp.cap)}</b>`
          + (sp.shared ? ' this month' : ' today')
          + (sp.resets ? ` <span class="dim">· back ${R.esc(sp.resets)}</span>` : '')
          + (spent ? ' <span class="err">— spent</span>' : ''));
      }
      const paid = status.paid_fallback || 'off';
      const quota = status.quota_mode || 'shutdown';
      const paidWord = { off: 'questions stay free', auto: 'paid rescue for questions',
        always: 'questions use paid first' }[paid] || paid;
      const quotaWord = { shutdown: 'swarm stops at the free limit', wait: 'swarm waits for reset',
        paid: 'swarm may continue on paid' }[quota] || quota;
      const w = status.wallet;
      const capWord = w ? `${money(w.spent)} / ${money(w.cap)} paid-answer cap` : 'no paid-answer cap';
      bits.push(`<span class="cost-line"><span class="cost-shield" aria-hidden="true">◇</span><b>Cost safety</b> · `
        + `${R.esc(paidWord)} · ${R.esc(quotaWord)} · ${R.esc(capWord)}`
        + ` <button class="inline-link" id="costSettings" title="Open cost and fallback settings">Manage</button></span>`);
      // The provider/model the developer's ask will use (local picks stay off OpenRouter).
      const prov = status.ask_provider || 'openrouter';
      const provLabel = prov === 'openrouter' ? 'OpenRouter' : prov;
      bits.push(`<span class="dim">ask with <b>${R.esc(provLabel)}</b>`
        + (status.ask_model ? ` · ${R.esc(status.ask_model)}` : '') + '</span>');
      // The key gate from the fresh-start prompt: cloud asks need a key, local ones do not.
      if (prov === 'openrouter' && info && info.api_key === false) {
        bits.push(`<span class="err">no OpenRouter API key set</span> `
          + `<button class="icon" id="setkey" title="Set the OpenRouter API key in Cursor encrypted SecretStorage">🔑</button>`);
      }
      const held = status.roadmap && status.roadmap.roots;
      if (held && held.length) {
        const rows = status.roadmap.blocked || {};
        bits.push(`<span class="err">roadmap held on ${held.length} packet(s)</span>: `
          + held.slice(0, 3).map((id) => `<b>${R.esc(id)}</b> — ${R.esc((rows[id] || {}).next_action || '')}`).join('; '));
      }
      const idle = status.idle;
      if (idle && idle.standing_down) bits.push(`<span class="dim">spare-time work paused: ${R.esc(idle.standing_down)}</span>`);
      else if (idle && idle.active) bits.push('<span class="dim">one spare-time improvement is in the queue</span>');
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
    const n = status ? status.queue.length : 0;
    const badge = $('qcount');
    badge.textContent = n ? String(n) : '';
    badge.hidden = !n;
    const dot = $('runState');
    if (dot) {
      dot.className = 'status-dot ' + (running ? 'running' : 'stopped');
      dot.setAttribute('title', running ? 'Swarm running' : 'Swarm stopped');
    }
  }

  // ---------------------------------------------------------------- model controls + settings

  function modelRows(provider) {
    if (provider === 'openrouter') return ((info && info.models) || []).map((id) => ({ id, name: id }));
    const p = ((settings && settings.providers) || []).find((x) => x.id === provider);
    return (p && p.models) || [];
  }

  function fillModels(select, provider, selected) {
    if (!select) return;
    const rows = modelRows(provider);
    const auto = provider === 'openrouter' ? '<option value="__auto__">Auto · best available</option>' : '';
    select.innerHTML = auto + rows.map((m) => `<option value="${R.esc(m.id)}">${R.esc(m.name || m.id)}</option>`).join('');
    select.value = selected && rows.some((m) => m.id === selected) ? selected : (provider === 'openrouter' ? '__auto__' : (rows[0] && rows[0].id) || '');
  }

  function renderModelControls() {
    const state = (settings && settings.providerState) || {};
    const current = state.backend || (status && status.ask_provider) || 'openrouter';
    const providers = [
      { id: 'openrouter', label: 'OpenRouter' },
      ...((settings && settings.providers) || []).map((p) => ({ id: p.id, label: p.label + (p.running ? '' : ' · offline') })),
      { id: '__custom__', label: 'Custom endpoint' },
    ];
    const options = providers.filter((p, i, all) => all.findIndex((x) => x.id === p.id) === i)
      .map((p) => `<option value="${R.esc(p.id)}">${R.esc(p.label)}</option>`).join('');
    for (const id of ['providerSelect', 'settingProvider']) {
      const node = $(id);
      if (!node) continue;
      node.innerHTML = options;
      node.value = current;
    }
    fillModels($('modelSelect'), current, state.model);
    fillModels($('settingModel'), current, state.model);
    const custom = current === '__custom__';
    $('modelSelect').hidden = custom;
    $('settingModelField').hidden = custom;
    $('customEndpoint').hidden = !custom;
    $('customModel').hidden = !custom;
    $('customSetting').hidden = !custom;
    if (state.customUrl) {
      $('customEndpoint').value = state.customUrl;
      $('settingCustomEndpoint').value = state.customUrl;
    }
    if (custom && state.model) {
      $('customModel').value = state.model;
      $('settingCustomModel').value = state.model;
    }
  }

  const PAID_PILL = { off: 'Free only', auto: 'Paid rescue', always: 'Paid first' };
  function renderCostSettings() {
    const configured = (settings && settings.keyConfigured) || !!(info && info.api_key);
    const wallet = status && status.wallet;
    const paidOn = $('settingPaid').value !== 'off' || $('settingQuota').value === 'paid';
    const pill = $('costSummaryPill');
    pill.textContent = PAID_PILL[$('settingPaid').value] || 'Free only';
    pill.classList.toggle('good', !paidOn);
    $('budgetSummary').textContent = wallet
      ? `${money(wallet.remaining)} left of ${money(wallet.cap)} · ${money(wallet.spent)} used`
      : 'No paid-answer cap is configured.';
    const needsPaidKey = ($('settingPaid').value !== 'off' || $('settingQuota').value === 'paid') && !configured;
    const daemonMissing = $('settingQuota').value === 'paid' && status && status.daemon_paid_configured === false;
    const capSpent = $('settingPaid').value !== 'off' && wallet && wallet.remaining <= 0;
    const warning = needsPaidKey ? 'Paid fallback needs an OpenRouter key.'
      : daemonMissing ? 'Background paid fallback still needs a paid model and spending cap in this repository’s swarm config.'
        : capSpent ? 'The paid-answer cap is exhausted. Change or reset it before using paid rescue.' : '';
    $('costWarning').textContent = warning;
    $('costWarning').className = 'cost-note' + (warning ? ' warn' : '');
  }

  function renderSettings() {
    if (!settings) return;
    const v = settings.values || {};
    $('settingModels').value = v.models == null ? 3 : v.models;
    $('settingSteps').value = v.stepsPerModel == null ? 8 : v.stepsPerModel;
    $('settingTimeout').value = v.timeoutSeconds == null ? 300 : v.timeoutSeconds;
    $('settingPaid').value = v.paidFallback || 'off';
    $('settingQuota').value = (status && status.quota_mode) || 'shutdown';
    $('settingSynthesize').checked = v.synthesize !== false;
    $('settingStudy').checked = v.useStudy !== false;
    $('settingPath').value = v.flintPath || '';
    $('settingPython').value = v.python || '';
    const configured = settings.keyConfigured || !!(info && info.api_key);
    $('keyStatus').textContent = configured ? 'Configured' : 'Not set';
    $('keyStatus').className = 'state-pill ' + (configured ? 'good' : '');
    $('clearKey').disabled = !settings.keyConfigured;
    renderCostSettings();
    renderModelControls();
  }

  // ---------------------------------------------------------------- customization ("dress it up")

  const SKIN_DEFAULTS = { bg: '#1e1e1e', surface: '#262626', text: '#cccccc', accent: '#6b8afd',
    border: '#7f7f7f66', radius: 8, font: '', fontCustom: '',
    bgImage: null, bgFit: 'cover', bgDarkness: 55, bgBlur: 0,
    headerImage: null, headerFit: 'cover', headerDarkness: 45, headerBlur: 0,
    composerImage: null, composerFit: 'cover', composerDarkness: 45, composerBlur: 0,
    glow: false, crt: false, marquee: false, marqueeText: '', confettiOnLand: false };

  const SKIN_PRESETS = [
    { id: 'winamp', cls: 'sw-winamp', name: 'Winamp Llama', desc: 'Two decibels above tasteful.',
      skin: { bg: '#140d26', surface: '#1f1640', text: '#e6e6ff', accent: '#2de2c2', border: '#3a2d66',
        radius: 3, font: "'Courier New', monospace", glow: true, crt: false, marquee: false, bgImage: null } },
    { id: 'vapor', cls: 'sw-vapor', name: 'Vaporwave Sunset', desc: 'Lo-fi beats to grind a swarm to.',
      skin: { bg: '#1a0f2e', surface: '#2a1850', text: '#ffe3fb', accent: '#ff6ec7', border: '#52317f',
        radius: 14, font: "'Trebuchet MS', sans-serif", glow: true, crt: false, marquee: false,
        bgImage: 'linear-gradient(160deg,#2a0a4a 0%,#5a1a6e 40%,#ff6ec7 75%,#ffd37a 100%)', bgFit: 'cover', bgDarkness: 35, bgBlur: 0 } },
    { id: 'fine', cls: 'sw-fine', name: 'This Is Fine', desc: "Everything's under control.",
      skin: { bg: '#2b1308', surface: '#3d1c0d', text: '#ffe7d6', accent: '#ff8a3d', border: '#6b3317',
        radius: 18, font: "'Comic Sans MS', cursive", glow: false, crt: false, marquee: false, bgImage: null } },
    { id: 'crt', cls: 'sw-crt', name: 'CRT Terminal', desc: 'Green text, tall tales, zero chill.',
      skin: { bg: '#021402', surface: '#041f04', text: '#7cff9b', accent: '#33ff66', border: '#0f5c22',
        radius: 2, font: "'Courier New', monospace", glow: true, crt: true, marquee: false, bgImage: null } },
    { id: 'comic', cls: 'sw-comic', name: 'Comic Sans Chaos', desc: 'Legally distinct from a ransom note.',
      skin: { bg: '#fff176', surface: '#ffe066', text: '#2b2400', accent: '#ff5c5c', border: '#d4b400',
        radius: 20, font: "'Comic Sans MS', cursive", glow: false, crt: false, marquee: false, confettiOnLand: true, bgImage: null } },
    { id: 'myspace', cls: 'sw-myspace', name: 'MySpace 2008', desc: 'Top 8 friends not included.',
      skin: { bg: '#120014', surface: '#28002b', text: '#ffd6f5', accent: '#ff1f8f', border: '#5c0050',
        radius: 10, font: 'Papyrus, fantasy', glow: true, crt: false, marquee: true,
        marqueeText: '✦ THANKS FOR VISITING MY SWARM ✦ PLEASE SIGN THE GUESTBOOK ✦ xoxo ✦', bgImage: null } },
  ];

  function clamp(n, lo, hi, dflt) { n = Number(n); return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : dflt; }

  /** Black or white, whichever reads better on this hex color — so a wild accent choice never eats its own button label. */
  function contrastOf(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return '#ffffff';
    const n = parseInt(m[1], 16);
    const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    return lum > 0.6 ? '#1a1a1a' : '#ffffff';
  }

  // A single nonce'd <style> tag carries every dynamic visual change. The CSP has no
  // 'unsafe-inline' for style-src, so a plain element.style write would be silently dropped;
  // nonce sources only cover <style>/<link> elements, never the style="" attribute or CSSOM writes.
  function skinStyleEl() {
    let s = document.getElementById('skinStyleTag');
    if (!s) {
      s = document.createElement('style');
      s.id = 'skinStyleTag';
      s.nonce = window.__NONCE__ || '';
      document.head.appendChild(s);
    }
    return s;
  }

  // For the one free-text field (a pasted URL): strip characters that could break out of the
  // url("...") wrapper or the declaration around it. Applied once, at the point of entry — not
  // to the already-assembled bgImage value, which by then is a complete, trusted CSS value
  // (either this, a preset's literal gradient() string, or an upload's base64 data: URI).
  function sanitizeCssText(v) { return String(v == null ? '' : v).replace(/["');{}]/g, ''); }

  /** One image/color/fit/darkness/blur layer, shared by the full backdrop, header, and composer. */
  function layerCss(selector, image, fit, darkness, blur, dfltDark) {
    if (!image) return `${selector}{opacity:0;}`;
    const dark = clamp(darkness, 0, 90, dfltDark) / 100;
    const b = clamp(blur, 0, 20, 0);
    const f = fit || 'cover';
    const fitCss = f === 'tile' ? 'background-repeat:repeat;background-size:auto;'
      : `background-repeat:no-repeat;background-size:${f};`;
    return `${selector}{opacity:1;filter:blur(${b}px);background-position:center;` +
      `background-image:linear-gradient(rgba(0,0,0,${dark}),rgba(0,0,0,${dark})),${image};${fitCss}}`;
  }

  function applySkin(s) {
    s = s || {};
    const root = [];
    if (s.bg) root.push(`--vscode-sideBar-background:${s.bg};`);
    if (s.surface) root.push(`--surface:${s.surface};--surface-raised:${s.surface};`);
    if (s.text) root.push(`--vscode-foreground:${s.text};--vscode-descriptionForeground:color-mix(in srgb, ${s.text} 65%, transparent);`);
    if (s.accent) {
      const onAccent = contrastOf(s.accent);
      root.push(`--accent:${s.accent};--vscode-focusBorder:${s.accent};--vscode-button-background:${s.accent};` +
        `--vscode-textLink-foreground:${s.accent};--vscode-badge-background:${s.accent};` +
        `--vscode-button-foreground:${onAccent};--vscode-badge-foreground:${onAccent};`);
    }
    if (s.border) root.push(`--hairline:${s.border};--edge:${s.border};`);
    if (s.radius != null) {
      const r = clamp(s.radius, 0, 24, 8);
      root.push(`--r-sm:${Math.round(r * .3)}px;--r-md:${Math.round(r * .45)}px;--r-lg:${Math.round(r * .6)}px;--r-xl:${Math.round(r)}px;`);
    }
    const css = root.length ? [`:root{${root.join('')}}`] : [];
    // A typed font always wins over the preset list — that's the actual "no whitelist" freedom;
    // the select is just a quick-pick. Sanitized because, unlike the select, this is free text.
    const fontValue = s.fontCustom ? sanitizeCssText(s.fontCustom) : s.font;
    if (fontValue) css.push(`body{font-family:${fontValue};}`);
    css.push(layerCss('#skinBackdrop', s.bgImage, s.bgFit, s.bgDarkness, s.bgBlur, 55));
    css.push(layerCss('#skinHeaderLayer', s.headerImage, s.headerFit, s.headerDarkness, s.headerBlur, 45));
    css.push(layerCss('#skinComposerLayer', s.composerImage, s.composerFit, s.composerDarkness, s.composerBlur, 45));
    skinStyleEl().textContent = css.join('\n');
    document.body.classList.toggle('fx-glow', !!s.glow);
    document.body.classList.toggle('fx-crt', !!s.crt);
    document.body.classList.toggle('fx-marquee', !!s.marquee);
    $('skinMarquee').hidden = !s.marquee;
    $('skinMarqueeText').textContent = s.marqueeText || '✦ YOUR SWARM, YOUR RULES ✦ MADE WITH VIBES AND DUCT TAPE ✦';
  }

  let skinSaveTimer = null;
  function scheduleSkinSave() {
    clearTimeout(skinSaveTimer);
    skinSaveTimer = setTimeout(() => vscode.postMessage({ type: 'skinSave', skin }), 400);
  }

  function readSkinForm() {
    const s = {
      bg: $('skinBg').value, surface: $('skinSurface').value, text: $('skinText').value,
      accent: $('skinAccent').value, border: $('skinBorder').value, radius: +$('skinRadius').value,
      font: $('skinFont').value, fontCustom: $('skinFontCustom').value.trim(),
      bgFit: $('skinBgFit').value, bgDarkness: +$('skinBgDark').value, bgBlur: +$('skinBgBlur').value,
      headerFit: $('skinHeaderFit').value, headerDarkness: +$('skinHeaderDark').value, headerBlur: +$('skinHeaderBlur').value,
      composerFit: $('skinComposerFit').value, composerDarkness: +$('skinComposerDark').value, composerBlur: +$('skinComposerBlur').value,
      glow: $('skinGlow').checked, crt: $('skinCrt').checked,
      marquee: $('skinMarqueeToggle').checked, marqueeText: $('skinMarqueeTextInput').value,
      confettiOnLand: $('skinConfetti').checked,
      // Images aren't form fields with a visible current value — they're set by upload/URL/clear
      // and just carried forward here so a color/slider tweak doesn't accidentally drop them.
      bgImage: skin && skin.bgImage || null,
      headerImage: skin && skin.headerImage || null,
      composerImage: skin && skin.composerImage || null,
    };
    return s;
  }

  function fillSkinForm(s) {
    s = Object.assign({}, SKIN_DEFAULTS, s || {});
    $('skinBg').value = s.bg; $('skinSurface').value = s.surface; $('skinText').value = s.text;
    $('skinAccent').value = s.accent; $('skinBorder').value = s.border;
    $('skinRadius').value = s.radius; $('skinRadiusVal').textContent = s.radius;
    $('skinFont').value = s.font || ''; $('skinFontCustom').value = s.fontCustom || '';
    $('skinBgFit').value = s.bgFit; $('skinBgDark').value = s.bgDarkness; $('skinBgDarkVal').textContent = s.bgDarkness + '%';
    $('skinBgBlur').value = s.bgBlur; $('skinBgBlurVal').textContent = s.bgBlur + 'px';
    $('skinHeaderFit').value = s.headerFit; $('skinHeaderDark').value = s.headerDarkness; $('skinHeaderDarkVal').textContent = s.headerDarkness + '%';
    $('skinHeaderBlur').value = s.headerBlur; $('skinHeaderBlurVal').textContent = s.headerBlur + 'px';
    $('skinComposerFit').value = s.composerFit; $('skinComposerDark').value = s.composerDarkness; $('skinComposerDarkVal').textContent = s.composerDarkness + '%';
    $('skinComposerBlur').value = s.composerBlur; $('skinComposerBlurVal').textContent = s.composerBlur + 'px';
    $('skinGlow').checked = !!s.glow; $('skinCrt').checked = !!s.crt;
    $('skinMarqueeToggle').checked = !!s.marquee; $('skinMarqueeTextField').hidden = !s.marquee;
    $('skinMarqueeTextInput').value = s.marqueeText || '';
    $('skinConfetti').checked = !!s.confettiOnLand;
  }

  function renderSkinPane() {
    const box = $('skinPresets');
    if (!box.childElementCount) {
      box.innerHTML = SKIN_PRESETS.map((p) =>
        `<button type="button" class="skin-swatch ${p.cls}" data-preset="${p.id}">` +
        `<span class="sw-preview" aria-hidden="true"></span><span class="sw-name">${R.esc(p.name)}</span>` +
        `<span class="sw-desc">${R.esc(p.desc)}</span></button>`).join('');
    }
    fillSkinForm(skin);
  }

  function burstConfetti() {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const canvas = document.createElement('canvas');
    canvas.className = 'confetti-canvas';
    canvas.width = innerWidth; canvas.height = innerHeight;
    document.body.appendChild(canvas);
    const ctx = canvas.getContext('2d');
    const colors = ['#ff6ec7', '#6f42ff', '#3fd6c0', '#ffd23f', '#ff5c5c', '#4dd4ff'];
    const pieces = Array.from({ length: 70 }, () => ({
      x: Math.random() * canvas.width, y: -20 - Math.random() * canvas.height * 0.3,
      r: 3 + Math.random() * 4, c: colors[Math.floor(Math.random() * colors.length)],
      vy: 2 + Math.random() * 3, vx: -1 + Math.random() * 2, rot: Math.random() * Math.PI, vr: -.2 + Math.random() * .4,
    }));
    let frames = 0;
    function tick() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (const p of pieces) {
        p.x += p.vx; p.y += p.vy; p.rot += p.vr;
        ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot);
        ctx.fillStyle = p.c; ctx.fillRect(-p.r, -p.r, p.r * 2, p.r * 2); ctx.restore();
      }
      frames++;
      if (frames < 110) requestAnimationFrame(tick); else canvas.remove();
    }
    requestAnimationFrame(tick);
  }

  // ---------------------------------------------------------------- tabs

  function renderTab() {
    for (const name of ['ask', 'queue', 'landed', 'settings', 'help', 'skin']) {
      $('pane-' + name).hidden = tab !== name;
      const t = $('tab-' + name);
      if (t) {
        t.classList.toggle('active', tab === name);
        t.setAttribute('aria-selected', tab === name ? 'true' : 'false');
      }
    }
    if (tab === 'queue') { renderQueue(); renderParked(); }
    if (tab === 'landed') renderLanded();
    if (tab === 'settings') { renderSettings(); vscode.postMessage({ type: 'settingsRefresh' }); }
    if (tab === 'skin') renderSkinPane();
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
      head.innerHTML = '<span class="dim">Loading what landed…</span>';
      list.innerHTML = '';
      return;
    }
    head.innerHTML = `<div class="qhead"><span>${landed.length} commit(s) on <b>`
      + `${R.esc(String(status && status.trunk || 'trunk'))}</b></span></div>`;
    list.innerHTML = '';
    if (!landed.length) {
      list.innerHTML = '<p class="dim">Nothing has landed yet.</p>';
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
    if (t.scope_gap) bits.push('<span class="warn" title="' + R.esc(t.scope_gap) + '">no workable scope</span>');
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
      (t.evidence ? `<button class="icon qevidence" title="What happened on the last attempt">🔍</button>` : '') +
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
      if (t.scope_gap) parts.push(`<div class="note warn">This will be parked rather than worked: ${R.esc(t.scope_gap)}</div>`);
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
      ? `<div class="qhead"><span>${status && status.parked != null ? status.parked : rows.length} split or parked</span>`
        + '<button id="pclear" class="secondary" title="Dismiss all abandoned tasks, including those not shown. Logs and patches stay available.">Clear all abandoned</button></div>' : '';
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
        + '<button class="icon pdismiss" title="Dismiss this abandoned task; preserve its logs and patches" aria-label="Dismiss abandoned task">×</button>'
        + (t.handoff ? `<button class="icon pwhy" data-path="${R.esc(t.handoff)}" title="Open the handoff for this attempt">why</button>` : '')));
      list.appendChild(row);
    }
  }

  // ---------------------------------------------------------------- threads

  function cardHtml(c) {
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
    node.appendChild(el('div', 'q', `${R.esc(t.question)}${t.where ? ` <span class="where">${R.esc(t.where)}</span>` : ''}` +
      `<button class="icon close" title="Remove">✕</button>`));
    const ordered = t.order.map((k) => t.cards[k]);
    const synthesis = ordered.find((c) => c.role === 'synthesis' && c.state === 'done');
    const others = ordered.filter((c) => c.role !== 'synthesis');
    if (!t.done) {
      // One quiet line instead of a stack of per-model pending cards — the free-model
      // consensus is an implementation detail, not something to watch happen live.
      node.appendChild(el('div', 'thinking-line', 'thinking'));
    } else if (synthesis) {
      node.appendChild(el('div', 'card synthesis', cardHtml(synthesis)));
      if (others.length) {
        const d = el('details', 'other-answers',
          `<summary>${others.length} other answer${others.length === 1 ? '' : 's'}</summary>`);
        for (const c of others) d.appendChild(el('div', 'card' + (c.state === 'error' ? ' failed' : ''), cardHtml(c)));
        node.appendChild(d);
      }
    } else {
      // No merged answer to lead with (synthesis is off, or every model including the
      // merger failed) — fall back to showing whatever individual answers/errors exist.
      for (const c of ordered) node.appendChild(el('div', 'card' + (c.state === 'error' ? ' failed' : ''), cardHtml(c)));
    }
    if (t.note) node.appendChild(el('div', 'note', R.esc(t.note)));
    return node;
  }

  /** Border color on the composer: idle (no color), thinking (red) while a model composes
   * text, running (light blue) while a model is actively calling a tool against the repo. */
  function updateComposerState() {
    const form = $('askForm');
    if (!form) return;
    let state = 'idle';
    for (const t of threads) {
      if (t.done) continue;
      for (const key of t.order) {
        const c = t.cards[key];
        if (!c || c.state !== 'pending') continue;
        if (c.progress && c.progress.startsWith('tool:')) { state = 'running'; break; }
        if (state !== 'running') state = 'thinking';
      }
      if (state === 'idle') state = 'thinking';  // a thread is live before its first card exists
      if (state === 'running') break;
    }
    form.classList.toggle('state-thinking', state === 'thinking');
    form.classList.toggle('state-running', state === 'running');
  }

  function renderThreads() {
    const box = $('threads');
    box.innerHTML = '';
    for (const t of threads.slice().reverse()) box.appendChild(renderThread(t));
    if (!threads.length) box.innerHTML = '<div class="empty-state"><span class="empty-mark">✦</span><h1>What are we building?</h1><p>Ask about the file you have open, select a few lines for a focused review, or describe the change you want.</p><div class="prompt-grid"><button class="prompt-chip" data-prompt="Find bugs in the code I have selected">Find a bug</button><button class="prompt-chip" data-prompt="Explain the code I have selected in plain English">Explain this code</button><button class="prompt-chip" data-prompt="What tests should this code have?">Plan tests</button><button class="prompt-chip" data-prompt="How could this code be simpler or faster?">Improve it</button></div></div>';
    updateComposerState();
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
      if (tab === 'settings') renderSettings();
    } else if (msg.type === 'info') { info = msg.data; renderStatus(); renderModelControls(); renderSettings(); }
    else if (msg.type === 'settings') { settings = msg.data; renderSettings(); save(); }
    else if (msg.type === 'settingsSaved') { $('settingsFeedback').className = 'dim'; $('settingsFeedback').textContent = msg.text || 'Saved.'; }
    else if (msg.type === 'settingsWarning') { $('settingsFeedback').className = 'warn'; $('settingsFeedback').textContent = msg.text || 'Saved with a warning.'; }
    else if (msg.type === 'settingsError') { $('settingsFeedback').textContent = msg.text || 'Could not save settings.'; $('settingsFeedback').className = 'err'; }
    else if (msg.type === 'keySaved') { settings = settings || {}; settings.keyConfigured = true; settings.keyStorage = msg.storage; $('apiKey').value = ''; $('settingsFeedback').className = 'dim'; $('settingsFeedback').textContent = `Key saved in ${msg.storage}.`; renderSettings(); }
    else if (msg.type === 'keyCleared') { settings = settings || {}; settings.keyConfigured = false; $('settingsFeedback').className = 'dim'; $('settingsFeedback').textContent = 'The encrypted key was forgotten on this laptop.'; renderSettings(); }
    else if (msg.type === 'landed') {
      const prevShas = new Set((landed || []).map((c) => c.sha));
      const isFirstLoad = landed === null;
      const commits = msg.commits || [];
      landed = commits; save(); if (tab === 'landed') renderLanded();
      if (!isFirstLoad && skin && skin.confettiOnLand && commits.some((c) => !prevShas.has(c.sha))) burstConfetti();
    }
    else if (msg.type === 'parked') { parked = msg.parked || []; save(); if (tab === 'queue') renderParked(); }
    else if (msg.type === 'tab') { tab = ['queue', 'landed', 'settings', 'help', 'skin'].includes(msg.tab) ? msg.tab : 'ask'; renderTab(); }
    else if (msg.type === 'skin') {
      skin = msg.skin || null; applySkin(skin);
      // Font lives in Settings, not here, so it needs filling regardless of which tab is open.
      if (tab === 'skin') renderSkinPane(); else fillSkinForm(skin);
      save();
    }
    else if (msg.type === 'updateVersion') { renderUpdateVersion(msg); }
    else if (msg.type === 'skinImageResult') {
      const field = msg.target || 'bgImage';
      skin = Object.assign({}, SKIN_DEFAULTS, skin, readSkinForm());
      skin[field] = `url("${msg.dataUri}")`;
      applySkin(skin); fillSkinForm(skin); scheduleSkinSave(); save();
      $('skinFeedback').className = 'dim'; $('skinFeedback').textContent = 'Background set.';
    } else if (msg.type === 'skinImageError') { $('skinFeedback').className = 'err'; $('skinFeedback').textContent = msg.text || 'Could not load that image.'; }
    else if (msg.type === 'queueTask') { editing = { id: msg.task.id, task: msg.task }; editError = null; showTab('queue'); renderQueue(); }
    else if (msg.type === 'queueSaved') { editing = null; editError = null; renderQueue(); }
    else if (msg.type === 'queueError') { editError = msg.error; renderQueue(); }
    else if (msg.type === 'askStart') { threads.push({ id: msg.id, kind: 'ask', question: msg.question, where: msg.where, repo: msg.repo, cards: {}, order: [] }); showTab('ask'); renderThreads(); }
    else if (msg.type === 'askEvent') { const t = thread(msg.id); if (t) { onAskEvent(t, msg.event); renderThreads(); } }
    else if (msg.type === 'notice') { $('notice').textContent = msg.text; $('notice').className = 'notice ' + (msg.level || ''); }
    else if (msg.type === 'prefill') { $('question').value = msg.question || ''; $('question').focus(); }
    save();
  });

  // ---------------------------------------------------------------- clicks

  function queued(id) { return ((status && status.queue) || []).find((t) => t.id === id); }

  document.addEventListener('click', (e) => {
    const tabBtn = e.target.closest('.tab');
    if (tabBtn) { showTab(tabBtn.dataset.tab); return; }
    const jump = e.target.closest('.tab-jump');
    if (jump) { showTab(jump.dataset.tab); return; }
    const prompt = e.target.closest('.prompt-chip');
    if (prompt) { $('question').value = prompt.dataset.prompt || ''; $('question').focus(); return; }
    if (e.target.closest('#board')) { vscode.postMessage({type:'board'}); return; }
    if (e.target.closest('#completed')) { vscode.postMessage({type:'completed'}); return; }
    if (e.target.closest('#costSettings')) { showTab('settings'); return; }
    if (e.target.closest('#budget')) { vscode.postMessage({ type: 'budget' }); return; }
    if (e.target.closest('#setkey')) { vscode.postMessage({ type: 'setApiKey' }); return; }
    if (e.target.closest('#qclear')) {
      vscode.postMessage({ type: 'queueClear', repo: status && status.repo });
      return;
    }
    if (e.target.closest('#pclear')) {
      vscode.postMessage({ type: 'parkedDismiss', repo: status && status.repo });
      return;
    }
    const landedRowEl = e.target.closest('#landedList .qrow');
    if (landedRowEl) {
      vscode.postMessage({ type: 'showCommit', sha: landedRowEl.dataset.sha, repo: status && status.repo });
      return;
    }
    const parkedRowEl = e.target.closest('#parkedList .qrow');
    if (parkedRowEl) {
      if (e.target.closest('.pdismiss')) {
        vscode.postMessage({ type: 'parkedDismiss', id: parkedRowEl.dataset.id, repo: status && status.repo });
        return;
      }
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
      if (e.target.closest('.qevidence')) {
        vscode.postMessage({ type: 'evidence', id, repo: status && status.repo });
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
    vscode.postMessage({ type: 'ask', question: q, withCode: $('withCode').checked,
      backend: $('providerSelect').value || 'openrouter',
      model: $('providerSelect').value === '__custom__' ? $('customModel').value.trim() : ($('modelSelect').value || '__auto__'),
      customUrl: $('customEndpoint').value.trim() });
  });
  $('question').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); $('askForm').requestSubmit(); }
  });
  $('providerSelect').addEventListener('change', () => {
    const provider = $('providerSelect').value;
    fillModels($('modelSelect'), provider, null);
    const custom = provider === '__custom__';
    $('modelSelect').hidden = custom;
    $('customEndpoint').hidden = !custom;
    $('customModel').hidden = !custom;
  });
  $('settingProvider').addEventListener('change', () => {
    const provider = $('settingProvider').value;
    fillModels($('settingModel'), provider, null);
    const custom = provider === '__custom__';
    $('settingModelField').hidden = custom;
    $('customSetting').hidden = !custom;
  });
  $('settingPaid').addEventListener('change', renderCostSettings);
  $('settingQuota').addEventListener('change', renderCostSettings);
  $('settingsForm').addEventListener('submit', (e) => {
    e.preventDefault();
    $('settingsFeedback').className = 'dim';
    $('settingsFeedback').textContent = 'Saving…';
    vscode.postMessage({ type: 'settingsSave', values: {
      backend: $('settingProvider').value,
      model: $('settingProvider').value === '__custom__' ? $('settingCustomModel').value.trim() : $('settingModel').value,
      customUrl: $('settingCustomEndpoint').value.trim(), models: +$('settingModels').value,
      stepsPerModel: +$('settingSteps').value, timeoutSeconds: +$('settingTimeout').value,
      paidFallback: $('settingPaid').value, quotaMode: $('settingQuota').value,
      quotaChanged: $('settingQuota').value !== ((status && status.quota_mode) || 'shutdown'),
      synthesize: $('settingSynthesize').checked,
      useStudy: $('settingStudy').checked, flintPath: $('settingPath').value,
      python: $('settingPython').value,
    } });
  });
  $('saveKey').addEventListener('click', () => {
    const key = $('apiKey').value.trim();
    if (!key) { $('apiKey').focus(); return; }
    $('settingsFeedback').textContent = 'Saving key securely…';
    vscode.postMessage({ type: 'settingsKeySave', key });
  });
  $('clearKey').addEventListener('click', () => vscode.postMessage({ type: 'settingsKeyClear' }));
  $('changeBudget').addEventListener('click', () => vscode.postMessage({ type: 'budget' }));
  $('updateVersion').addEventListener('click', () => {
    if (updateState.busy) return;
    updateState = Object.assign({}, updateState, { busy: true, error: null });
    renderUpdateVersion(updateState);
    vscode.postMessage({ type: 'updateInstall' });
  });
  $('queue').addEventListener('click', () => vscode.postMessage({ type: 'queue', title: $('question').value.trim(), withCode: $('withCode').checked }));
  $('start').addEventListener('click', () => vscode.postMessage({ type: 'start' }));
  $('drain').addEventListener('click', () => vscode.postMessage({ type: 'stop', repo: status && status.repo, drain: true }));
  $('restart').addEventListener('click', () => vscode.postMessage({ type: 'restart', repo: status && status.repo }));
  // Name the repository the panel is showing: stop means this swarm, not every swarm on the machine.
  $('stop').addEventListener('click', () => vscode.postMessage({ type: 'stop', repo: status && status.repo }));
  $('report').addEventListener('click', () => vscode.postMessage({ type: 'report' }));

  // ---------------------------------------------------------------- customization controls

  function applyFromForm() {
    skin = readSkinForm();
    applySkin(skin);
    scheduleSkinSave();
    save();
  }

  $('skinPresets').addEventListener('click', (e) => {
    const btn = e.target.closest('.skin-swatch');
    if (!btn) return;
    const preset = SKIN_PRESETS.find((p) => p.id === btn.dataset.preset);
    if (!preset) return;
    skin = Object.assign({}, SKIN_DEFAULTS, preset.skin);
    fillSkinForm(skin);
    applySkin(skin);
    scheduleSkinSave(); save();
    $('skinFeedback').className = 'dim'; $('skinFeedback').textContent = `${preset.name} applied.`;
  });
  for (const id of ['skinBg', 'skinSurface', 'skinText', 'skinAccent', 'skinBorder', 'skinMarqueeTextInput']) {
    $(id).addEventListener('input', applyFromForm);
  }
  $('skinRadius').addEventListener('input', () => { $('skinRadiusVal').textContent = $('skinRadius').value; applyFromForm(); });
  $('skinBgDark').addEventListener('input', () => { $('skinBgDarkVal').textContent = $('skinBgDark').value + '%'; applyFromForm(); });
  $('skinBgBlur').addEventListener('input', () => { $('skinBgBlurVal').textContent = $('skinBgBlur').value + 'px'; applyFromForm(); });
  for (const id of ['skinFont', 'skinBgFit']) $(id).addEventListener('change', applyFromForm);
  for (const id of ['skinGlow', 'skinCrt', 'skinConfetti']) $(id).addEventListener('change', applyFromForm);
  $('skinMarqueeToggle').addEventListener('change', () => {
    $('skinMarqueeTextField').hidden = !$('skinMarqueeToggle').checked;
    applyFromForm();
  });
  for (const id of ['skinFontCustom']) $(id).addEventListener('input', applyFromForm);
  for (const id of ['skinHeaderFit', 'skinComposerFit']) $(id).addEventListener('change', applyFromForm);
  for (const id of ['skinHeaderDark', 'skinHeaderBlur', 'skinComposerDark', 'skinComposerBlur']) {
    const suffix = id.endsWith('Blur') ? 'px' : '%';
    $(id).addEventListener('input', () => { $(id + 'Val').textContent = $(id).value + suffix; applyFromForm(); });
  }

  /** Wires upload/paste-URL/clear for one image layer. `field` is the skin property it sets. */
  function wireImageControls(prefix, field) {
    $(prefix + 'Upload').addEventListener('click', () => vscode.postMessage({ type: 'skinPickImage', target: field }));
    $(prefix + 'UrlApply').addEventListener('click', () => {
      const url = $(prefix + 'Url').value.trim();
      if (!url) return;
      skin = Object.assign({}, SKIN_DEFAULTS, skin, readSkinForm());
      skin[field] = `url("${sanitizeCssText(url)}")`;
      applySkin(skin); scheduleSkinSave(); save();
      $('skinFeedback').className = 'dim'; $('skinFeedback').textContent = 'Background set.';
    });
    $(prefix + 'Clear').addEventListener('click', () => {
      skin = Object.assign({}, readSkinForm());
      skin[field] = null;
      $(prefix + 'Url').value = '';
      applySkin(skin); scheduleSkinSave(); save();
    });
  }
  wireImageControls('skinBg', 'bgImage');
  wireImageControls('skinHeader', 'headerImage');
  wireImageControls('skinComposer', 'composerImage');

  $('skinConfettiTest').addEventListener('click', burstConfetti);
  $('skinChaos').addEventListener('click', () => {
    const rnd = () => '#' + Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, '0');
    const fonts = ["'Courier New', monospace", "'Comic Sans MS', cursive", 'Papyrus, fantasy',
      'Impact, sans-serif', "'Brush Script MT', cursive", "'Trebuchet MS', sans-serif"];
    const carry = skin || {};
    skin = Object.assign({}, SKIN_DEFAULTS, {
      bg: rnd(), surface: rnd(), text: rnd(), accent: rnd(), border: rnd(),
      radius: Math.round(Math.random() * 24), font: fonts[Math.floor(Math.random() * fonts.length)],
      fontCustom: '',   // chaos picks from the curated list; a leftover custom font would silently override it
      glow: Math.random() > .5, crt: Math.random() > .7, marquee: Math.random() > .6,
      marqueeText: carry.marqueeText, confettiOnLand: Math.random() > .5,
      // Chaos is for colors and fonts, not your pictures — carry every image layer forward as-is.
      bgImage: carry.bgImage || null, bgFit: carry.bgFit, bgDarkness: carry.bgDarkness, bgBlur: carry.bgBlur,
      headerImage: carry.headerImage || null, headerFit: carry.headerFit, headerDarkness: carry.headerDarkness, headerBlur: carry.headerBlur,
      composerImage: carry.composerImage || null, composerFit: carry.composerFit, composerDarkness: carry.composerDarkness, composerBlur: carry.composerBlur,
    });
    fillSkinForm(skin); applySkin(skin); scheduleSkinSave(); save();
    $('skinFeedback').className = 'dim'; $('skinFeedback').textContent = 'Chaos applied. Good luck.';
  });
  $('skinResetBtn').addEventListener('click', () => {
    skin = null;
    fillSkinForm(null); applySkin(null);
    vscode.postMessage({ type: 'skinReset' }); save();
    $('skinFeedback').className = 'dim'; $('skinFeedback').textContent = 'Back to default.';
  });

  renderStatus();
  renderThreads();
  renderTab();
  applySkin(skin);
  // The font control lives in Settings now, not the Customize tab, so it needs filling in
  // regardless of which tab (if any) happens to get opened first.
  fillSkinForm(skin);
  vscode.postMessage({ type: 'ready' });
})();
