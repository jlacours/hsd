/* HSD Web Dashboard — ES module, no dependencies */

const API_BASE = '/api';

// ── State ──────────────────────────────────────────────────────────
const state = {
  tasks: [],
  activity: [],
  stats: null,
  selectedTask: null,
  view: 'board',        // 'board' | 'needs-you' | 'activity' | 'stats' | 'lint' | 'settings'
  theme: loadTheme(),
  settings: loadSettings(),
  drawerOpen: false,
  drawerFullscreen: false,
  paletteOpen: false,
  selection: new Set(),   // slugs of multi-selected cards (batch actions)
  _drag: null,            // { slugs: [...] } while a card drag is in flight
};

// ── Init ───────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  applyTheme(state.theme);
  setupSSE();
  setupKeyboard();
  setupDragAndDrop();
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
  render();
}

function loadSettings() {
  const defaults = {
    showSidebar: true,
    openTasksFullscreen: false,
    showSourceModelFallback: true,
  };
  try {
    return { ...defaults, ...JSON.parse(localStorage.getItem('hsd-settings') || '{}') };
  } catch {
    return defaults;
  }
}

function saveSettings() {
  localStorage.setItem('hsd-settings', JSON.stringify(state.settings));
}

// ── Navigation / Views ─────────────────────────────────────────────
function switchView(view) {
  state.view = view;
  state.drawerOpen = false;
  state.drawerFullscreen = false;
  state.selectedTask = null;
  render();
}

// ── Task Drawer ────────────────────────────────────────────────────
async function openDrawer(slug) {
  try {
    const task = await api(`/tasks/${slug}`);
    state.selectedTask = task;
    state.drawerOpen = true;
    state.drawerFullscreen = state.settings.openTasksFullscreen;
    renderDrawer();
  } catch (e) {
    console.error('Failed to load task:', e);
  }
}

function closeDrawer() {
  state.drawerOpen = false;
  state.drawerFullscreen = false;
  state.selectedTask = null;
  const el = document.getElementById('drawer');
  if (el) el.remove();
  render();
}

function toggleDrawerFullscreen() {
  state.drawerFullscreen = !state.drawerFullscreen;
  renderDrawer();
}

// Tasks in the order the current view displays them (board columns left to right).
function drawerTaskList() {
  if (state.view === 'needs-you') return needsYouTasks();
  return STAGES.flatMap(s => state.tasks.filter(t => t.stage === s.key));
}

function navigateDrawer(delta) {
  if (!state.selectedTask) return;
  const list = drawerTaskList();
  const idx = list.findIndex(t => t.slug === state.selectedTask.slug);
  if (idx === -1) return;
  const next = list[idx + delta];
  if (next) openDrawer(next.slug);
}

function neighborSlug(slug) {
  const list = drawerTaskList();
  const idx = list.findIndex(t => t.slug === slug);
  if (idx === -1) return null;
  const next = list[idx + 1] || list[idx - 1];
  return next ? next.slug : null;
}

// After a stage-changing action, move the drawer off the now-moved task.
function advanceDrawerFrom(slug, neighbor) {
  if (!state.drawerOpen || state.selectedTask?.slug !== slug) return;
  if (neighbor) openDrawer(neighbor);
  else closeDrawer();
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

async function doResolve(slug) {
  const neighbor = neighborSlug(slug);
  try {
    const note = encodeURIComponent('Resolved via web dashboard');
    await api(`/tasks/${slug}/resolve?note=${note}`, {
      method: 'POST',
    });
    await fetchAll();
    advanceDrawerFrom(slug, neighbor);
  } catch (e) {
    console.error('Resolve failed:', e);
  }
}

async function resolveTask(slug) {
  if (!confirm('Resolve this task back to todo?')) return;
  await doResolve(slug);
}

async function humanReviewTask(slug, verdict, note) {
  const neighbor = neighborSlug(slug);
  try {
    await api(`/tasks/${slug}/human-review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(note ? { verdict, note } : { verdict }),
    });
    await fetchAll();
    advanceDrawerFrom(slug, neighbor);
  } catch (e) {
    console.error('Human review failed:', e);
  }
}

async function acceptTask(slug) {
  if (!confirm('Accept this task and close it?')) return;
  await humanReviewTask(slug, 'accept');
}

async function reviseTask(slug) {
  const note = prompt('Optional note for the human-revision request:');
  if (note === null) return; // cancelled
  await humanReviewTask(slug, 'revise', note.trim() || undefined);
}

// ── Batch Selection / Human Moves ──────────────────────────────────
const HUMAN_MOVE_TARGETS = {
  'reviewed': ['closed', 'to-be-revised-by-human'],
  'to-be-revised-by-human': ['todo'],
};

async function applyHumanMove(slug, fromStage, toStage) {
  if (fromStage === 'reviewed' && toStage === 'closed') {
    await api(`/tasks/${slug}/human-review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ verdict: 'accept' }),
    });
  } else if (fromStage === 'reviewed' && toStage === 'to-be-revised-by-human') {
    await api(`/tasks/${slug}/human-review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ verdict: 'revise' }),
    });
  } else if (fromStage === 'to-be-revised-by-human' && toStage === 'todo') {
    const note = encodeURIComponent('Resolved via web dashboard');
    await api(`/tasks/${slug}/resolve?note=${note}`, { method: 'POST' });
  } else {
    throw new Error(`Illegal move: ${fromStage} → ${toStage}`);
  }
}

async function runHumanMoves(slugs, toStage) {
  const failures = [];
  for (const slug of slugs) {
    const task = state.tasks.find(t => t.slug === slug);
    if (!task) continue;
    try {
      await applyHumanMove(slug, task.stage, toStage);
    } catch (e) {
      failures.push(slug);
      console.error(`Move to ${toStage} failed for ${slug}:`, e);
    }
  }
  state.selection.clear();
  await fetchAll();
  if (state.drawerOpen && slugs.includes(state.selectedTask?.slug)) closeDrawer();
  return failures;
}

function toggleCardSelection(slug) {
  if (state.selection.has(slug)) state.selection.delete(slug);
  else state.selection.add(slug);
  render();
}

function clearSelection() {
  state.selection.clear();
  render();
}

function renderSelectionBar() {
  document.getElementById('selection-bar')?.remove();
  const known = new Set(state.tasks.map(t => t.slug));
  state.selection.forEach(slug => { if (!known.has(slug)) state.selection.delete(slug); });
  if (!state.selection.size) return;

  const selected = [...state.selection].map(slug => state.tasks.find(t => t.slug === slug));
  const allReviewed = selected.every(t => t.stage === 'reviewed');
  const allHuman = selected.every(t => t.stage === 'to-be-revised-by-human');

  const bar = document.createElement('div');
  bar.id = 'selection-bar';
  bar.className = 'selection-bar';
  bar.innerHTML = `
    <span class="selection-count">${state.selection.size} selected</span>
    ${allReviewed ? `
      <button class="btn primary" onclick="bulkAccept()">Accept all</button>
      <button class="btn" onclick="bulkRevise()">Revise all</button>
    ` : ''}
    ${allHuman ? `<button class="btn primary" onclick="bulkResolve()">Resolve all</button>` : ''}
    <button class="btn icon" onclick="clearSelection()">✕ Clear</button>
  `;
  document.body.appendChild(bar);
}

async function bulkAccept() {
  const slugs = [...state.selection];
  if (!confirm(`Accept ${slugs.length} tasks and close them?`)) return;
  await runHumanMoves(slugs, 'closed');
}

async function bulkRevise() {
  const slugs = [...state.selection];
  if (!confirm(`Send ${slugs.length} tasks to human revision?`)) return;
  await runHumanMoves(slugs, 'to-be-revised-by-human');
}

async function bulkResolve() {
  const slugs = [...state.selection];
  if (!confirm(`Resolve ${slugs.length} tasks back to todo?`)) return;
  await runHumanMoves(slugs, 'todo');
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
          <span>${esc(t.title)}</span>
          <span class="chip stage-${escAttr(t.stage)}">${esc(t.stage)}</span>
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
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

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
      if (state.drawerFullscreen) {
        state.drawerFullscreen = false;
        renderDrawer();
      } else {
        closeDrawer();
      }
      return;
    }

    if (state.drawerOpen && e.key === 'f') {
      toggleDrawerFullscreen();
      return;
    }

    if (state.drawerOpen && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
      e.preventDefault();
      navigateDrawer(e.key === 'ArrowLeft' ? -1 : 1);
      return;
    }

    if (e.key === 'Escape' && state.selection.size && !state.drawerOpen && !state.paletteOpen) {
      clearSelection();
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
        case 'p': switchView('settings'); return;
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

// ── Drag & Drop ────────────────────────────────────────────────────
function setupDragAndDrop() {
  document.addEventListener('dragstart', (e) => {
    const card = e.target.closest?.('.card[data-slug]');
    if (!card) return;
    const slug = card.dataset.slug;
    const slugs = state.selection.has(slug) ? [...state.selection] : [slug];
    let targets = null;
    slugs.forEach(s => {
      const task = state.tasks.find(t => t.slug === s);
      const legal = HUMAN_MOVE_TARGETS[task?.stage] || [];
      targets = targets === null
        ? new Set(legal)
        : new Set(legal.filter(stage => targets.has(stage)));
    });
    state._drag = { slugs, targets };
    document.querySelectorAll('.card[data-slug]').forEach(el => {
      if (slugs.includes(el.dataset.slug)) el.classList.add('dragging');
    });
    document.querySelectorAll('.column[data-stage]').forEach(col => {
      col.classList.toggle('drop-ok', targets.has(col.dataset.stage));
    });
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', slug);
  });

  document.addEventListener('dragover', (e) => {
    const col = e.target.closest?.('.column.drop-ok');
    if (!col) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    document.querySelectorAll('.column.drop-hover').forEach(c => {
      if (c !== col) c.classList.remove('drop-hover');
    });
    col.classList.add('drop-hover');
  });

  document.addEventListener('dragleave', (e) => {
    const col = e.target.closest?.('.column.drop-hover');
    if (!col) return;
    if (e.relatedTarget && col.contains(e.relatedTarget)) return;
    col.classList.remove('drop-hover');
  });

  document.addEventListener('drop', (e) => {
    const col = e.target.closest?.('.column.drop-ok');
    if (!col || !state._drag) return;
    e.preventDefault();
    runHumanMoves(state._drag.slugs, col.dataset.stage);
  });

  document.addEventListener('dragend', () => {
    document.querySelectorAll('.card.dragging').forEach(el => el.classList.remove('dragging'));
    document.querySelectorAll('.column.drop-ok, .column.drop-hover').forEach(el => {
      el.classList.remove('drop-ok', 'drop-hover');
    });
    state._drag = null;
  });
}

// ── Global Listeners ─────────────────────────────────────────────────
function setupGlobalListeners() {
  document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);

  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('drawer-overlay')) closeDrawer();
    if (e.target.closest('.palette-overlay') && !e.target.closest('.palette')) closePalette();

    const copyButton = e.target.closest('[data-copy-value]');
    if (copyButton) {
      e.stopPropagation();
      copyValue(copyButton);
    }
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
  sidebar.classList.toggle('hidden', !state.settings.showSidebar);

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
                     t.staleness_hours > 6 ? `Stale (${formatDuration(t.staleness_hours)})` : '';
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
    case 'settings':
      html = renderSettings();
      break;
  }

  main.innerHTML = html;

  // Attach event listeners to dynamic content
  main.querySelectorAll('.card').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('[data-copy-value]')) return;
      if (e.ctrlKey || e.metaKey) {
        toggleCardSelection(el.dataset.slug);
        return;
      }
      openDrawer(el.dataset.slug);
    });
  });

  renderSelectionBar();

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

  main.querySelectorAll('[data-setting]').forEach(el => {
    el.addEventListener('change', () => {
      const key = el.dataset.setting;
      if (key === 'theme') {
        state.theme = el.value;
        applyTheme(state.theme);
      } else {
        state.settings[key] = el.type === 'checkbox' ? el.checked : el.value;
        saveSettings();
      }
      render();
    });
  });
}

const STAGES = [
  { key: 'todo', label: 'Todo', color: 'stage-todo' },
  { key: 'in-progress', label: 'In Progress', color: 'stage-in-progress' },
  { key: 'done', label: 'Done', color: 'stage-done' },
  { key: 'reviewed', label: 'Reviewed', color: 'stage-reviewed' },
  { key: 'to-be-revised-by-human', label: 'Human Revision', color: 'stage-human' },
  { key: 'closed', label: 'Closed', color: 'stage-closed' },
];

function renderBoard() {
  let html = `<div class="board">`;
  STAGES.forEach(s => {
    const tasks = state.tasks.filter(t => t.stage === s.key);
    html += `<div class="column" data-stage="${escAttr(s.key)}">
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
  const model = t.owner_model || (state.settings.showSourceModelFallback ? t.source_model : null);
  const modelKind = t.owner_model ? 'Owner model' : 'Source model';
  return `<div class="card${state.selection.has(t.slug) ? ' checked' : ''}" draggable="true" data-slug="${escAttr(t.slug)}" data-owner="${escAttr(t.owner_harness || '')}" data-dest="${escAttr(t.destination)}">
    <div class="card-title">${esc(t.title)}</div>
    <div class="card-meta">
      ${t.owner_harness ? harnessChip(t.owner_harness) : ''}
      ${model ? modelChip(model, modelKind, t.owner_model ? 'current' : 'source') : ''}
      ${isAny ? `<span class="chip any-harness">any</span>` : ''}
      <button class="session-id" data-copy-value="${t.id}" title="Copy session ID ${t.id}">Session #${t.id}</button>
      <span class="age-badge">${formatDuration(t.age_hours)}</span>
    </div>
  </div>`;
}

function needsYouTasks() {
  return state.tasks.filter(t =>
    t.stage === 'to-be-revised-by-human' ||
    (t.stage === 'done') ||
    (t.stage === 'todo' && t.destination === 'any') ||
    (t.stage === 'in-progress' && t.staleness_hours > 6)
  ).sort((a, b) => b.staleness_hours - a.staleness_hours);
}

function renderNeedsYou() {
  const needsYou = needsYouTasks();

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
        <div><strong>${esc(a.task_title)}</strong> ${esc(a.from_stage || '—')} → ${esc(a.to_stage)}</div>
        <div class="text-sm text-secondary">${esc(a.actor_harness)}/${esc(a.actor_model)} at ${esc(a.at)}</div>
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
    'closed': 'Closed',
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
      issues.push({ slug: t.slug, issue: `Stale claim (${formatDuration(t.staleness_hours)} untouched)`, severity: 'warning' });
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

function renderSettings() {
  return `<div class="settings-page">
    <div class="settings-heading">
      <h2>Settings</h2>
      <p>Local display preferences for keeping harness sessions visible.</p>
    </div>
    <div class="settings-panel">
      <label class="setting-row">
        <span>
          <strong>Theme</strong>
          <small>Choose the dashboard color scheme.</small>
        </span>
        <select data-setting="theme">
          <option value="dark"${state.theme === 'dark' ? ' selected' : ''}>Dark</option>
          <option value="light"${state.theme === 'light' ? ' selected' : ''}>Light</option>
        </select>
      </label>
      ${settingToggle('showSidebar', 'Show Needs You sidebar', 'Keep sessions requiring attention visible beside the board.')}
      ${settingToggle('openTasksFullscreen', 'Open tasks fullscreen', 'Use the full viewport when inspecting task details and Markdown.')}
      ${settingToggle('showSourceModelFallback', 'Show source-model fallback', 'Show the source model on cards when no current owner model was recorded.')}
    </div>
    <p class="settings-note">Settings are stored locally in this browser.</p>
  </div>`;
}

function settingToggle(key, label, description) {
  return `<label class="setting-row">
    <span>
      <strong>${esc(label)}</strong>
      <small>${esc(description)}</small>
    </span>
    <input type="checkbox" data-setting="${escAttr(key)}"${state.settings[key] ? ' checked' : ''}>
  </label>`;
}

// ── Drawer ─────────────────────────────────────────────────────────
function renderDrawer() {
  const existing = document.getElementById('drawer');
  if (existing) existing.remove();
  if (!state.drawerOpen || !state.selectedTask) return;

  const t = state.selectedTask;
  const primaryPath = t.working_dir || t.repository || '';
  const overlay = document.createElement('div');
  overlay.className = 'drawer-overlay';
  overlay.id = 'drawer';

  const drawer = document.createElement('div');
  drawer.className = `drawer${state.drawerFullscreen ? ' fullscreen' : ''}`;
  drawer.setAttribute('role', 'dialog');
  drawer.setAttribute('aria-modal', 'true');
  drawer.setAttribute('aria-labelledby', 'drawer-heading');
  const navList = drawerTaskList();
  const navIdx = navList.findIndex(x => x.slug === t.slug);
  const createdShort = formatTimestampShort(t.created_at) || '—';
  const updatedShort = formatTimestampShort(t.updated_at) || '—';
  drawer.innerHTML = `
    <div class="drawer-header">
      <div class="drawer-title">
        <h2 id="drawer-heading">${esc(t.title)}</h2>
      </div>
      <div class="flex gap-2">
        ${navIdx !== -1 ? `
          <span class="drawer-nav-pos">${navIdx + 1} / ${navList.length}</span>
          <button class="btn icon" onclick="navigateDrawer(-1)"${navIdx === 0 ? ' disabled' : ''} aria-label="Previous task" title="Previous task (←)">‹</button>
          <button class="btn icon" onclick="navigateDrawer(1)"${navIdx === navList.length - 1 ? ' disabled' : ''} aria-label="Next task" title="Next task (→)">›</button>
        ` : ''}
        <button class="btn icon" onclick="toggleDrawerFullscreen()" aria-label="${state.drawerFullscreen ? 'Exit fullscreen' : 'Open fullscreen'}" title="${state.drawerFullscreen ? 'Exit fullscreen' : 'Open fullscreen'} (f)">⛶</button>
        <button class="btn icon" onclick="closeDrawer()" aria-label="Close task details">✕</button>
      </div>
    </div>
    <div class="drawer-actions">
      <div class="flex gap-2">
        <button class="session-id" data-copy-value="${t.id}" title="Copy session ID ${t.id}">Session #${t.id}</button>
        ${t.stage === 'to-be-revised-by-human' ? `<button class="btn primary" onclick="resolveTask('${t.slug}')">Resolve</button>` : ''}
        ${t.stage === 'reviewed' ? `
          <button class="btn primary" onclick="acceptTask('${t.slug}')">Accept</button>
          <button class="btn" onclick="reviseTask('${t.slug}')">Send to Human Revision</button>
        ` : ''}
      </div>
      <div class="flex gap-2">
        ${primaryPath ? `<button class="btn icon copy-control" data-copy-value="${escAttr(primaryPath)}" title="Copy working path">Copy path</button>` : ''}
        <button class="btn icon" onclick="exportTask('${t.slug}','md')" title="Export Markdown">.md</button>
        <button class="btn icon" onclick="exportTask('${t.slug}','org')" title="Export Org">.org</button>
        ${t.updated_at ? `<span class="drawer-date" title="Created ${escAttr(createdShort)} · Updated ${escAttr(updatedShort)}">${esc(updatedShort)}</span>` : ''}
      </div>
    </div>
    <div class="drawer-body">
      <div class="drawer-meta">
        <span class="chip stage-${t.stage}">${t.stage}</span>
        <span class="chip">${esc(t.status)}</span>
        <span class="chip">${esc(t.destination)}</span>
      </div>

      ${renderTaskMetadata(t)}
      ${renderTaskPaths(t)}

      ${t.sections ? Object.entries(t.sections).map(([name, content]) => `
        <div class="drawer-section">
          <h3>${sectionLabel(name)}</h3>
          <div class="content markdown-body">${t.sections_html?.[name] || `<p>${esc(content)}</p>`}</div>
        </div>
      `).join('') : ''}

      ${renderDiffSection(t)}

      ${t.transitions?.length ? `
        <div class="drawer-section">
          <h3>Timeline</h3>
          <ul class="timeline">
            ${t.transitions.map(tr => `
              <li>
                <div class="action">${esc(tr.from_stage || '—')} → ${esc(tr.to_stage)}</div>
                <div class="time">${esc(tr.actor_harness)}/${esc(tr.actor_model)} · ${esc(tr.at)}</div>
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

function renderDiffSection(task) {
  const verifyRow = task.verify_cmd
    ? `<div class="task-paths" style="margin-bottom:10px">
        <div class="task-path">
          <span class="label">Verify</span>
          <code title="${escAttr(task.verify_cmd)}">${esc(task.verify_cmd)}</code>
          <button class="btn icon copy-control" data-copy-value="${escAttr(task.verify_cmd)}" title="Copy verify command">Copy</button>
        </div>
      </div>`
    : '';
  const diffBody = task.diff
    ? `<pre class="diff-viewer">${esc(task.diff)}</pre>`
    : `<p class="metadata-empty">No diff captured</p>`;
  return `<div class="drawer-section">
    <h3>Diff</h3>
    ${verifyRow}
    ${diffBody}
  </div>`;
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

function formatDuration(hours) {
  const totalMinutes = Math.max(0, Math.round(Number(hours) * 60));
  if (totalMinutes < 1) return 'now';
  if (totalMinutes < 60) return `${totalMinutes}m`;

  const totalHours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (totalHours < 24) {
    return minutes ? `${totalHours}h ${minutes}m` : `${totalHours}h`;
  }

  const totalDays = Math.floor(totalHours / 24);
  const remainingHours = totalHours % 24;
  if (totalDays < 14) {
    return remainingHours ? `${totalDays}d ${remainingHours}h` : `${totalDays}d`;
  }

  const weeks = Math.floor(totalDays / 7);
  const remainingDays = totalDays % 7;
  return remainingDays ? `${weeks}w ${remainingDays}d` : `${weeks}w`;
}

function harnessColor(harness) {
  const brandColors = {
    'claude': 'var(--harness-claude-code)',
    'claude-code': 'var(--harness-claude-code)',
    'codex': 'var(--harness-codex)',
    'openai-codex': 'var(--harness-codex)',
    'opencode': 'var(--harness-opencode)',
    'open-code': 'var(--harness-opencode)',
  };
  const normalized = harness.trim().toLowerCase();
  if (brandColors[normalized]) return brandColors[normalized];

  let hash = 2166136261;
  for (let i = 0; i < normalized.length; i++) {
    hash ^= normalized.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue} 72% 58%)`;
}

function harnessChip(harness) {
  return `<span class="chip harness" style="--harness-color:${harnessColor(harness)}">${esc(harness)}</span>`;
}

function shortModel(name) {
  if (!name) return name;
  const cap = (w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
  const dots = (v) => v.replace(/(\d)-(?=\d)/g, '$1.');
  let s = String(name).split('/').pop().replace(/[-:]free$/i, '');
  const rules = [
    [/(opus|sonnet|haiku|fable)[-.]?(\d[\d.\-]*)?/i, (m) => m[2] ? `${cap(m[1])} ${dots(m[2])}` : cap(m[1])],
    [/claude[-.]?(\d[\d.\-]*)?/i, (m) => m[1] ? `Claude ${dots(m[1])}` : 'Claude'],
    [/gpt-?([\w.\-]+)/i, (m) => `GPT ${dots(m[1])}`],
    [/deepseek-?([\w.\-]+)/i, (m) => `DeepSeek ${m[1]}`],
    [/gemini-?([\w.\-]+)/i, (m) => `Gemini ${m[1]}`],
  ];
  for (const [re, fmt] of rules) {
    const m = s.match(re);
    if (m) { s = fmt(m); break; }
  }
  return s.length > 16 ? `${s.slice(0, 15)}…` : s;
}

function modelChip(model, label, prefix = '') {
  const rawModel = String(model).trim();
  const quotedModel = rawModel.match(/^`([^`]+)`/);
  const cleanModel = quotedModel
    ? quotedModel[1]
    : rawModel.split(' (', 1)[0].replace(/^`+|`+$/g, '');
  return `<span class="chip model" title="${escAttr(`${label}: ${rawModel}`)}">${prefix ? `<span class="chip-prefix">${esc(prefix)}</span>` : ''}${esc(shortModel(cleanModel))}</span>`;
}

function renderTaskMetadata(task) {
  const currentHarness = task.owner_harness
    ? harnessChip(task.owner_harness)
    : '<span class="metadata-empty">Unclaimed</span>';
  const currentModel = task.owner_model
    ? modelChip(task.owner_model, 'Current owner model')
    : `<span class="metadata-empty">${task.stage === 'todo' ? 'Unclaimed' : 'Not recorded'}</span>`;
  const sourceHarness = task.source_harness
    ? harnessChip(task.source_harness)
    : '<span class="metadata-empty">Not recorded</span>';
  const sourceModel = task.source_model
    ? `<code title="${escAttr(task.source_model)}">${esc(shortModel(task.source_model))}</code>`
    : '<span class="metadata-empty">Not recorded</span>';

  return `<div class="task-overview">
    <h3>Session Metadata</h3>
    <div class="metadata-grid">
      ${metadataItem('Current harness', currentHarness)}
      ${metadataItem('Current model', currentModel)}
      ${metadataItem('Source harness', sourceHarness)}
      ${metadataItem('Source model', sourceModel)}
      ${metadataItem('Task slug', `<code>${esc(task.slug)}</code><button class="btn icon copy-control" data-copy-value="${escAttr(task.slug)}" title="Copy task slug">Copy</button>`)}
      ${metadataItem('Author / agent', esc(task.author) || '<span class="metadata-empty">Not recorded</span>')}
      ${metadataItem('Branch / commit', task.branch_commit ? `<code>${esc(task.branch_commit)}</code>` : '<span class="metadata-empty">Not recorded</span>')}
      ${metadataItem('Working tree', esc(task.tree_state) || '<span class="metadata-empty">Not recorded</span>')}
      ${metadataItem('Created', formatTimestamp(task.created_at))}
      ${metadataItem('Updated', formatTimestamp(task.updated_at))}
      ${task.model_check_note ? metadataItem('Model check', esc(task.model_check_note), true) : ''}
    </div>
  </div>`;
}

function metadataItem(label, value, wide = false) {
  return `<div class="metadata-item${wide ? ' wide' : ''}">
    <div class="metadata-label">${esc(label)}</div>
    <div class="metadata-value">${value}</div>
  </div>`;
}

function formatTimestamp(value) {
  if (!value) return '<span class="metadata-empty">Not recorded</span>';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return `<code>${esc(value)}</code>`;
  return `<time datetime="${escAttr(value)}" title="${escAttr(value)}">${esc(parsed.toLocaleString())}</time>`;
}

function formatTimestampShort(value) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
}

function renderTaskPaths(task) {
  const paths = [
    ['Working dir', task.working_dir],
    ['Repository', task.repository],
  ].filter(([, path]) => path);

  if (!paths.length) return '';

  return `<div class="task-paths">${paths.map(([label, path]) => `
    <div class="task-path">
      <span class="label">${label}</span>
      <code title="${escAttr(path)}">${esc(path)}</code>
      <button class="btn icon copy-control" data-copy-value="${escAttr(path)}" title="Copy ${label.toLowerCase()}">Copy</button>
    </div>
  `).join('')}</div>`;
}

async function copyValue(button) {
  const value = button.dataset.copyValue;
  if (!value) return;

  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    textarea.remove();
  }

  const original = button.textContent;
  button.textContent = 'Copied';
  button.classList.add('copied');
  setTimeout(() => {
    button.textContent = original;
    button.classList.remove('copied');
  }, 1200);
}

function esc(s) {
  if (!s) return '';
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}

function escAttr(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/`/g, '&#96;');
}
