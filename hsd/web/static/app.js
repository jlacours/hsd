/* HSD Web Dashboard — ES module, no dependencies */

const API_BASE = '/api';

// ── State ──────────────────────────────────────────────────────────
const state = {
  tasks: [],
  activity: [],
  stats: null,
  selectedTask: null,
  view: 'board',        // 'board' | 'needs-you' | 'activity' | 'stats' | 'lint'
  theme: loadTheme(),
  drawerOpen: false,
  paletteOpen: false,
};

// ── Init ───────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  applyTheme(state.theme);
  setupSSE();
  setupKeyboard();
  setupGlobalListeners();
  fetchAll();
});

// ── API ────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Accept': 'application/json', ...opts.headers },
    ...opts,
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
  return res.json();
}

async function fetchAll() {
  try {
    const [tasks, activity, stats] = await Promise.all([
      api('/tasks'),
      api('/activity?limit=50'),
      api('/stats'),
    ]);
    state.tasks = tasks;
    state.activity = activity;
    state.stats = stats;
    render();
  } catch (e) {
    console.error('Fetch error:', e);
  }
}

// ── SSE ────────────────────────────────────────────────────────────
function setupSSE() {
  const evtSource = new EventSource('/api/stream');
  evtSource.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.event === 'refresh') fetchAll();
    } catch {}
  };
  evtSource.onerror = () => {
    // Reconnect after 3s
    setTimeout(setupSSE, 3000);
  };
}

// ── Theme ──────────────────────────────────────────────────────────
function loadTheme() {
  const saved = localStorage.getItem('hsd-theme');
  if (saved === 'dark' || saved === 'light') return saved;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('hsd-theme', t);
}

function toggleTheme() {
  state.theme = state.theme === 'dark' ? 'light' : 'dark';
  applyTheme(state.theme);
  document.getElementById('theme-toggle').textContent = state.theme === 'dark' ? '☀' : '☾';
}

// ── Navigation / Views ─────────────────────────────────────────────
function switchView(view) {
  state.view = view;
  state.drawerOpen = false;
  state.selectedTask = null;
  render();
}

// ── Task Drawer ────────────────────────────────────────────────────
async function openDrawer(slug) {
  try {
    const task = await api(`/tasks/${slug}`);
    state.selectedTask = task;
    state.drawerOpen = true;
    renderDrawer();
  } catch (e) {
    console.error('Failed to load task:', e);
  }
}

function closeDrawer() {
  state.drawerOpen = false;
  state.selectedTask = null;
  render();
}

async function exportTask(slug, fmt) {
  try {
    const res = await fetch(`${API_BASE}/tasks/${slug}/export?fmt=${fmt}`);
    const data = await res.json();
    const blob = new Blob([data.content], { type: data.media_type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${slug}.${fmt}`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    console.error('Export failed:', e);
  }
}

async function resolveTask(slug) {
  if (!confirm('Resolve this task back to todo?')) return;
  try {
    await api(`/tasks/${slug}/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: 'Resolved via web dashboard' }),
    });
    fetchAll();
  } catch (e) {
    console.error('Resolve failed:', e);
  }
}

// ── Command Palette ────────────────────────────────────────────────
function openPalette() {
  state.paletteOpen = true;
  renderPalette();
}

function closePalette() {
  state.paletteOpen = false;
  document.getElementById('palette')?.remove();
}

function renderPalette() {
  const existing = document.getElementById('palette');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'palette';
  overlay.className = 'palette-overlay';
  overlay.innerHTML = `
    <div class="palette">
      <input type="text" id="palette-input" placeholder="Search tasks or type command..." autofocus>
      <div class="palette-results" id="palette-results"></div>
    </div>
  `;
  document.body.appendChild(overlay);

  const input = document.getElementById('palette-input');
  const results = document.getElementById('palette-results');
  let selectedIndex = -1;

  function filterTasks(query) {
    const q = query.toLowerCase();
    const commands = [
      { label: 'Go to Board', action: () => { switchView('board'); closePalette(); }, keys: 'g b' },
      { label: 'Go to Needs You', action: () => { switchView('needs-you'); closePalette(); }, keys: 'g n' },
      { label: 'Go to Activity', action: () => { switchView('activity'); closePalette(); }, keys: 'g a' },
      { label: 'Go to Stats', action: () => { switchView('stats'); closePalette(); }, keys: 'g s' },
      { label: 'Toggle Theme', action: () => { toggleTheme(); renderPalette(); }, keys: '' },
    ];
    const matchingCommands = commands.filter(c =>
      c.label.toLowerCase().includes(q)
    );
    const matchingTasks = state.tasks.filter(t =>
      t.slug.toLowerCase().includes(q) ||
      t.title.toLowerCase().includes(q)
    ).slice(0, 10);
    return { commands: matchingCommands, tasks: matchingTasks };
  }

  function renderResults(query) {
    const { commands, tasks } = filterTasks(query);
    let html = '';
    if (commands.length) {
      html += `<div class="text-secondary text-sm" style="padding:6px 16px">Commands</div>`;
      commands.forEach((c, i) => {
        html += `<div class="palette-item" data-index="${i}" data-type="command">
          <span>${c.label}</span>
          ${c.keys ? `<span class="kbd">${c.keys}</span>` : ''}
        </div>`;
      });
    }
    if (tasks.length) {
      html += `<div class="text-secondary text-sm" style="padding:6px 16px">Tasks</div>`;
      tasks.forEach((t, i) => {
        html += `<div class="palette-item" data-index="${i + commands.length}" data-type="task" data-slug="${t.slug}">
          <span>${t.title}</span>
          <span class="chip stage-${t.stage}">${t.stage}</span>
        </div>`;
      });
    }
    if (!commands.length && !tasks.length) {
      html += `<div class="empty-state">No results</div>`;
    }
    results.innerHTML = html;
    selectedIndex = -1;

    // Click handlers
    results.querySelectorAll('.palette-item').forEach(el => {
      el.addEventListener('click', () => {
        const type = el.dataset.type;
        if (type === 'command') {
          const idx = parseInt(el.dataset.index);
          const c = filterTasks(input.value).commands[idx];
          c.action();
        } else if (type === 'task') {
          closePalette();
          openDrawer(el.dataset.slug);
        }
      });
    });
  }

  input.addEventListener('input', () => renderResults(input.value));
  renderResults('');

  input.addEventListener('keydown', (e) => {
    const items = results.querySelectorAll('.palette-item');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedIndex = Math.min(selectedIndex + 1, items.length - 1);
      updateSelection(items);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedIndex = Math.max(selectedIndex - 1, 0);
      updateSelection(items);
    } else if (e.key === 'Enter' && selectedIndex >= 0) {
      items[selectedIndex]?.click();
    } else if (e.key === 'Escape') {
      closePalette();
    }
  });

  function updateSelection(items) {
    items.forEach((el, i) => el.classList.toggle('selected', i === selectedIndex));
    if (items[selectedIndex]) {
      items[selectedIndex].scrollIntoView({ block: 'nearest' });
    }
  }
}

// ── Keyboard ───────────────────────────────────────────────────────
function setupKeyboard() {
  document.addEventListener('keydown', (e) => {
    // Don't capture when in input
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

    if (e.ctrlKey && e.key === 'k') {
      e.preventDefault();
      openPalette();
      return;
    }

    if (state.paletteOpen && e.key === 'Escape') {
      closePalette();
      return;
    }

    if (state.drawerOpen && e.key === 'Escape') {
      closeDrawer();
      return;
    }

    // Navigation
    if (e.key === 'g') {
      state._gPending = true;
      setTimeout(() => state._gPending = false, 500);
      return;
    }

    if (state._gPending) {
      state._gPending = false;
      switch (e.key) {
        case 'b': switchView('board'); return;
        case 'n': switchView('needs-you'); return;
        case 'a': switchView('activity'); return;
        case 's': switchView('stats'); return;
      }
    }

    // j/k navigation in board
    if (e.key === 'j' || e.key === 'k') {
      const cards = document.querySelectorAll('.card');
      if (!cards.length) return;
      const current = document.querySelector('.card.selected');
      let idx = 0;
      if (current) {
        current.classList.remove('selected');
        idx = Array.from(cards).indexOf(current);
        idx = e.key === 'j' ? Math.min(idx + 1, cards.length - 1) : Math.max(idx - 1, 0);
      }
      cards[idx].classList.add('selected');
      cards[idx].scrollIntoView({ block: 'nearest' });
    }
  });
}

// ── Global Listeners ─────────────────────────────────────────────────
function setupGlobalListeners() {
  document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);

  document.addEventListener('click', (e) => {
    if (e.target.closest('.drawer-overlay')) closeDrawer();
    if (e.target.closest('.palette-overlay') && !e.target.closest('.palette')) closePalette();
  });
}

// ── Rendering ──────────────────────────────────────────────────────
function render() {
  renderHeader();
  renderSidebar();
  renderMain();
}

function renderHeader() {
  const h = document.getElementById('header');
  if (!h) return;
  const themeBtn = h.querySelector('#theme-toggle');
  if (themeBtn) themeBtn.textContent = state.theme === 'dark' ? '☀' : '☾';
}

function renderSidebar() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;

  let html = `<h2>Needs You</h2>`;

  // Tasks needing human attention
  const needsYou = state.tasks.filter(t =>
    t.stage === 'to-be-revised-by-human' ||
    (t.stage === 'done' && !t.owner_harness) ||
    (t.stage === 'todo' && t.destination === 'any') ||
    (t.stage === 'in-progress' && t.staleness_hours > 6)
  ).sort((a, b) => b.staleness_hours - a.staleness_hours);

  if (!needsYou.length) {
    html += `<div class="empty-state">Nothing needs you right now</div>`;
  } else {
    needsYou.forEach(t => {
      const reason = t.stage === 'to-be-revised-by-human' ? 'Needs revision' :
                     t.stage === 'done' ? 'Awaiting review' :
                     t.stage === 'todo' && t.destination === 'any' ? 'Unclaimed (any harness)' :
                     t.staleness_hours > 6 ? `Stale (${t.staleness_hours.toFixed(0)}h)` : '';
      html += `<div class="sidebar-card" onclick="openDrawer('${t.slug}')">
        <div class="title">${esc(t.title)}</div>
        <div class="meta">${reason} · ${t.stage}</div>
      </div>`;
    });
  }

  sidebar.innerHTML = html;
}

function renderMain() {
  const main = document.getElementById('main');
  if (!main) return;

  let html = '';

  switch (state.view) {
    case 'board':
      html = renderBoard();
      break;
    case 'needs-you':
      html = renderNeedsYou();
      break;
    case 'activity':
      html = renderActivity();
      break;
    case 'stats':
      html = renderStats();
      break;
    case 'lint':
      html = renderLint();
      break;
  }

  main.innerHTML = html;

  // Attach event listeners to dynamic content
  main.querySelectorAll('.card').forEach(el => {
    el.addEventListener('click', () => openDrawer(el.dataset.slug));
  });

  // Filter chips
  main.querySelectorAll('[data-filter]').forEach(el => {
    el.addEventListener('click', () => {
      // Simple harness filter toggle
      const harness = el.dataset.filter;
      const cards = main.querySelectorAll('.card');
      cards.forEach(c => {
        if (!harness || c.dataset.owner === harness || c.dataset.dest === harness) {
          c.style.display = '';
        } else {
          c.style.display = 'none';
        }
      });
    });
  });

  // Tab switching
  main.querySelectorAll('.tab').forEach(el => {
    el.addEventListener('click', () => {
      const view = el.dataset.view;
      if (view) switchView(view);
    });
  });
}

function renderBoard() {
  const stages = [
    { key: 'todo', label: 'Todo', color: 'stage-todo' },
    { key: 'in-progress', label: 'In Progress', color: 'stage-in-progress' },
    { key: 'done', label: 'Done', color: 'stage-done' },
    { key: 'reviewed', label: 'Reviewed', color: 'stage-reviewed' },
    { key: 'to-be-revised-by-human', label: 'Human Revision', color: 'stage-human' },
  ];

  let html = `<div class="board">`;
  stages.forEach(s => {
    const tasks = state.tasks.filter(t => t.stage === s.key);
    html += `<div class="column">
      <div class="column-header">
        <span>${s.label}</span>
        <span class="count">${tasks.length}</span>
      </div>
      <div class="column-body">`;
    if (!tasks.length) {
      html += `<div class="empty-state">Empty</div>`;
    } else {
      tasks.forEach(t => {
        html += renderCard(t);
      });
    }
    html += `</div></div>`;
  });
  html += `</div>`;
  return html;
}

function renderCard(t) {
  const isAny = t.destination === 'any' && !t.owner_harness;
  const isStale = t.staleness_hours > 6;
  return `<div class="card" data-slug="${t.slug}" data-owner="${t.owner_harness || ''}" data-dest="${t.destination}">
    <div class="card-title">${esc(t.title)}</div>
    <div class="card-meta">
      ${t.owner_harness ? `<span class="chip harness">${esc(t.owner_harness)}</span>` : ''}
      ${t.owner_model ? `<span class="chip model">${esc(t.owner_model)}</span>` : ''}
      ${isAny ? `<span class="chip any-harness">any</span>` : ''}
      ${isStale ? `<span class="chip stale">${t.staleness_hours.toFixed(0)}h</span>` : ''}
      <span class="age-badge">${t.age_hours.toFixed(0)}h</span>
    </div>
  </div>`;
}

function renderNeedsYou() {
  const needsYou = state.tasks.filter(t =>
    t.stage === 'to-be-revised-by-human' ||
    (t.stage === 'done') ||
    (t.stage === 'todo' && t.destination === 'any') ||
    (t.stage === 'in-progress' && t.staleness_hours > 6)
  ).sort((a, b) => b.staleness_hours - a.staleness_hours);

  let html = `<h2 style="margin-bottom:12px">Needs You <span class="text-secondary">(${needsYou.length})</span></h2>`;
  if (!needsYou.length) {
    html += `<div class="empty-state">Nothing needs you right now.</div>`;
    return html;
  }

  html += `<div class="board" style="overflow:visible;flex-wrap:wrap">`;
  needsYou.forEach(t => {
    html += renderCard(t);
  });
  html += `</div>`;
  return html;
}

function renderActivity() {
  let html = `<h2 style="margin-bottom:12px">Activity</h2>`;
  if (!state.activity.length) {
    html += `<div class="empty-state">No activity yet.</div>`;
    return html;
  }
  html += `<ul class="activity-list">`;
  state.activity.forEach(a => {
    const icon = a.to_stage === 'reviewed' ? '✓' : a.to_stage === 'in-progress' ? '▶' :
                 a.to_stage === 'done' ? '●' : a.to_stage === 'to-be-revised-by-human' ? '▲' : '○';
    html += `<li class="activity-item">
      <div class="activity-icon" style="background:color-mix(in srgb, var(--accent) 15%,transparent);color:var(--accent)">${icon}</div>
      <div>
        <div><strong>${esc(a.task_title)}</strong> ${a.from_stage || '—'} → ${a.to_stage}</div>
        <div class="text-sm text-secondary">${esc(a.actor_harness)}/${esc(a.actor_model)} at ${a.at}</div>
        ${a.note ? `<div class="text-sm text-secondary">${esc(a.note)}</div>` : ''}
      </div>
    </li>`;
  });
  html += `</ul>`;
  return html;
}

function renderStats() {
  const s = state.stats || { total: 0, by_stage: {} };
  let html = `<h2 style="margin-bottom:12px">Statistics</h2>`;
  html += `<div class="stats-grid">
    <div class="stat-card">
      <div class="value">${s.total}</div>
      <div class="label">Total Tasks</div>
    </div>`;

  const stageLabels = {
    'todo': 'Todo',
    'in-progress': 'In Progress',
    'done': 'Done',
    'reviewed': 'Reviewed',
    'to-be-revised-by-human': 'Human Revision',
  };
  Object.entries(stageLabels).forEach(([key, label]) => {
    const count = s.by_stage?.[key] || 0;
    if (count > 0) {
      html += `<div class="stat-card">
        <div class="value">${count}</div>
        <div class="label">${label}</div>
      </div>`;
    }
  });
  html += `</div>`;

  // Simple cycle time estimate based on transitions
  if (state.tasks.length > 0) {
    html += `<h3 style="margin-bottom:8px">Tasks Overview</h3>`;
    html += `<div class="board" style="overflow:visible;flex-wrap:wrap">`;
    state.tasks.slice(0, 20).forEach(t => {
      html += renderCard(t);
    });
    html += `</div>`;
  }

  return html;
}

function renderLint() {
  const issues = [];
  state.tasks.forEach(t => {
    if (t.source_model === 'MODEL NOT EXPOSED' && !t.model_check_note) {
      issues.push({ slug: t.slug, issue: 'MODEL NOT EXPOSED without check note', severity: 'warning' });
    }
    if (t.stage === 'in-progress' && t.staleness_hours > 24) {
      issues.push({ slug: t.slug, issue: `Stale claim (${t.staleness_hours.toFixed(0)}h untouched)`, severity: 'warning' });
    }
  });

  let html = `<h2 style="margin-bottom:12px">Lint <span class="text-secondary">(${issues.length} issues)</span></h2>`;
  if (!issues.length) {
    html += `<div class="empty-state">No protocol violations found.</div>`;
  } else {
    html += `<ul class="activity-list">`;
    issues.forEach(iss => {
      html += `<li class="activity-item">
        <div class="activity-icon" style="background:color-mix(in srgb, var(--warning) 15%,transparent);color:var(--warning)">!</div>
        <div>
          <div><strong>${esc(iss.slug)}</strong></div>
          <div class="text-sm text-secondary">${esc(iss.issue)}</div>
        </div>
      </li>`;
    });
    html += `</ul>`;
  }
  return html;
}

// ── Drawer ─────────────────────────────────────────────────────────
function renderDrawer() {
  const existing = document.getElementById('drawer');
  if (existing) existing.remove();
  if (!state.drawerOpen || !state.selectedTask) return;

  const t = state.selectedTask;
  const overlay = document.createElement('div');
  overlay.className = 'drawer-overlay';
  overlay.id = 'drawer';

  const drawer = document.createElement('div');
  drawer.className = 'drawer';
  drawer.innerHTML = `
    <div class="drawer-header">
      <h2>${esc(t.title)}</h2>
      <div class="flex gap-2">
        <button class="btn icon" onclick="exportTask('${t.slug}','md')" title="Export Markdown">.md</button>
        <button class="btn icon" onclick="exportTask('${t.slug}','org')" title="Export Org">.org</button>
        ${t.stage === 'to-be-revised-by-human' ? `<button class="btn primary" onclick="resolveTask('${t.slug}')">Resolve</button>` : ''}
        <button class="btn icon" onclick="closeDrawer()">✕</button>
      </div>
    </div>
    <div class="drawer-body">
      <div class="drawer-meta">
        <span class="chip stage-${t.stage}">${t.stage}</span>
        ${t.owner_harness ? `<span class="chip harness">${esc(t.owner_harness)}</span>` : ''}
        ${t.owner_model ? `<span class="chip model">${esc(t.owner_model)}</span>` : ''}
        <span class="chip">${esc(t.destination)}</span>
        <span style="font-size:11px;color:var(--text-tertiary);margin-left:auto">${t.created_at}</span>
      </div>

      ${t.sections ? Object.entries(t.sections).map(([name, content]) => `
        <div class="drawer-section">
          <h3>${sectionLabel(name)}</h3>
          <div class="content">${esc(content)}</div>
        </div>
      `).join('') : ''}

      ${t.transitions?.length ? `
        <div class="drawer-section">
          <h3>Timeline</h3>
          <ul class="timeline">
            ${t.transitions.map(tr => `
              <li>
                <div class="action">${esc(tr.from_stage || '—')} → ${esc(tr.to_stage)}</div>
                <div class="time">${tr.actor_harness}/${tr.actor_model} · ${tr.at}</div>
                ${tr.note ? `<div class="text-sm text-secondary">${esc(tr.note)}</div>` : ''}
              </li>
            `).join('')}
          </ul>
        </div>
      ` : ''}

      ${t.reviews?.length ? `
        <div class="drawer-section">
          <h3>Reviews</h3>
          ${t.reviews.map(r => `
            <div style="padding:8px;border:1px solid var(--border-light);border-radius:var(--radius-sm);margin-bottom:8px">
              <div><strong>${esc(r.verdict)}</strong> by ${esc(r.reviewer_harness)}/${esc(r.reviewer_model)}</div>
              <div class="text-sm text-secondary">${esc(r.findings)}</div>
            </div>
          `).join('')}
        </div>
      ` : ''}
    </div>
  `;

  overlay.appendChild(drawer);
  document.body.appendChild(overlay);
}

// ── Helpers ────────────────────────────────────────────────────────
function sectionLabel(name) {
  const labels = {
    'objective': 'Objective',
    'current_state': 'Current State',
    'summary_for_review': 'Summary for Review',
    'work_completed': 'Work Completed',
    'files_changed': 'Files Changed',
    'commands_verification': 'Commands & Verification',
    'decisions_assumptions': 'Decisions & Assumptions',
    'blockers_risks': 'Blockers & Risks',
    'warnings': 'Warnings',
    'next_actions': 'Next Actions',
    'artifacts': 'Artifacts',
    'continuation_prompt': 'Continuation Prompt',
    'raw': 'Raw Content',
  };
  return labels[name] || name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function esc(s) {
  if (!s) return '';
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}
