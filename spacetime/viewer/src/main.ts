// lea live: one scan on a big screen, straight from SpacetimeDB subscriptions. Nothing here decides anything about
// the object; every word on screen comes from the rows the backend mirrored (category, issues, verdict, safety notes).
import { DbConnection, tables } from './module_bindings';
import type { ScanScene, Pin } from './scene';

declare global {
  interface Window {
    LEA_LIVE?: { host?: string; db?: string };
    __leaLatency?: { table: string; ms: number; toDb: number; at: number }[];
  }
}

const params = new URLSearchParams(location.search);
const HOST = params.get('host') || window.LEA_LIVE?.host || 'http://127.0.0.1:3000';
const DB = params.get('db') || window.LEA_LIVE?.db || 'lea-live';
const PINNED_SCAN = params.get('scan') || '';
const TOKEN_KEY = `lea-live:${HOST}/${DB}:token`;

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const app = $('app');

// ------------------------------------------------------------------ words (generic; the data supplies the nouns)
const PHASE: Record<string, string> = {
  capturing: 'Scanning',
  inspecting: 'Inspecting the photos',
  running: 'Writing the report',
  done: 'Report ready',
  closed: 'Report ready',
  failed: 'The run stopped',
};
const SEVERITY: Record<string, { word: string; tone: string; rank: number }> = {
  safety: { word: 'Safety', tone: 'var(--danger)', rank: 0 },
  functional: { word: 'Functional', tone: 'var(--caution)', rank: 1 },
  '': { word: 'To check', tone: 'var(--accent)', rank: 2 },
  cosmetic: { word: 'Cosmetic', tone: 'var(--cosmetic)', rank: 3 },
};
const human = (s: string) => s.replace(/_/g, ' ').trim();

// ------------------------------------------------------------------ the connection
let ready = false;
let connected = false;
let lastError = '';
let downSince = Date.now();

function storedToken(): string | undefined {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? undefined;
  } catch {
    return undefined;
  }
}

const conn = DbConnection.builder()
  .withUri(HOST)
  .withDatabaseName(DB)
  .withToken(storedToken())
  .withAutomaticReconnect()
  .onConnect((_c, _identity, token) => {
    connected = true;
    downSince = 0;
    try {
      localStorage.setItem(TOKEN_KEY, token);
    } catch {
      /* private window: a fresh anonymous identity next time */
    }
    schedule();
  })
  .onDisconnect((_c, err) => {
    connected = false;
    downSince ||= Date.now();
    lastError = err ? String(err) : '';
    schedule();
  })
  .onConnectError((_c, err) => {
    connected = false;
    downSince ||= Date.now();
    lastError = String(err);
    try {
      localStorage.removeItem(TOKEN_KEY); // a token from another server or a reset database
    } catch {
      /* ignore */
    }
    schedule();
  })
  .build();

conn
  .subscriptionBuilder()
  .onApplied(() => {
    ready = true;
    schedule();
  })
  .subscribe([
    tables.scan, tables.objectIdentity, tables.progress, tables.still, tables.finding, tables.report, tables.mesh,
    tables.focus, tables.bridge,
  ]);

// Latency: backend event time (sourceAt) to this page applying the row, for rows that change while we watch.
const latencies: number[] = [];
window.__leaLatency = [];
function measure(table: string, row: { sourceAt?: string; updatedAt?: { microsSinceUnixEpoch: bigint } }): void {
  if (!ready || !row.sourceAt) return;
  const t = Date.parse(row.sourceAt);
  if (!Number.isFinite(t)) return;
  const ms = Date.now() - t;
  if (ms < -5000 || ms > 10000) return; // a replay or a skewed clock, not a live change
  latencies.push(ms);
  if (latencies.length > 40) latencies.shift();
  const db = row.updatedAt ? Number(row.updatedAt.microsSinceUnixEpoch / 1000n) : NaN;
  window.__leaLatency!.push({ table, ms, toDb: Math.round(db - t), at: Date.now() });
}

function watch<R extends { sourceAt?: string; updatedAt?: { microsSinceUnixEpoch: bigint } }>(name: string, handle: {
  onInsert: (cb: (ctx: unknown, row: R) => void) => void;
  onUpdate?: (cb: (ctx: unknown, oldRow: R, row: R) => void) => void;
  onDelete: (cb: (ctx: unknown, row: R) => void) => void;
}): void {
  handle.onInsert((_c, row) => { measure(name, row); schedule(); });
  handle.onUpdate?.((_c, old, row) => { if (old.sourceAt !== row.sourceAt) measure(name, row); schedule(); });
  handle.onDelete(() => schedule());
}
watch('scan', conn.db.scan as never);
watch('object_identity', conn.db.objectIdentity as never);
watch('progress', conn.db.progress as never);
watch('still', conn.db.still as never);
watch('finding', conn.db.finding as never);
watch('report', conn.db.report as never);
watch('mesh', conn.db.mesh as never);
watch('focus', conn.db.focus as never);
watch('bridge', conn.db.bridge as never);

// ------------------------------------------------------------------ render
let queued = false;
function schedule(): void {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    render();
  });
}
// The bridge heartbeat goes stale on its own; and a big screen must come back by itself: when the database has been
// unreachable for a while (a restart, a dropped network), start over with a fresh page rather than wait on retries.
setInterval(() => {
  if (!connected && downSince && Date.now() - downSince > 20000) location.reload();
  schedule();
}, 2000);

let scene: ScanScene | null = null;
let sceneLoading: Promise<ScanScene> | null = null;
function getScene(): Promise<ScanScene> {
  sceneLoading ??= import('./scene').then((m) => (scene = new m.ScanScene($('three') as HTMLCanvasElement)));
  return sceneLoading;
}

const tweens = new Map<HTMLElement, number>();
function count(el: HTMLElement, to: number): void {
  const from = Number(el.dataset.v ?? '0');
  if (from === to) return;
  el.dataset.v = String(to);
  if (to > from) {
    el.classList.add('bump');
    setTimeout(() => el.classList.remove('bump'), 700);
  }
  const start = performance.now(), dur = Math.min(900, 300 + Math.abs(to - from) * 60);
  cancelAnimationFrame(tweens.get(el) ?? 0);
  const step = (now: number) => {
    const k = Math.max(0, Math.min(1, (now - start) / dur));
    const e = 1 - Math.pow(1 - k, 3);
    el.textContent = String(Math.round(from + (to - from) * e));
    if (k < 1) tweens.set(el, requestAnimationFrame(step));
  };
  tweens.set(el, requestAnimationFrame(step));
}

function setText(el: HTMLElement, text: string, swap = false): void {
  if (el.textContent === text) return;
  if (!swap || !el.textContent || el.textContent === '…') {
    el.textContent = text;
    return;
  }
  el.classList.add('swap');
  setTimeout(() => {
    el.textContent = text;
    el.classList.remove('swap');
  }, 380);
}

function focusScanId(): string {
  if (PINNED_SCAN) return PINNED_SCAN;
  const f = conn.db.focus.id.find(0);
  if (f?.scanId && conn.db.scan.scanId.find(f.scanId)) return f.scanId;
  let best = '', bestAt = '';
  for (const s of conn.db.scan.iter()) if (s.sourceAt > bestAt) { best = s.scanId; bestAt = s.sourceAt; }
  return best;
}

function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  return s.length ? s[Math.floor(s.length / 2)] : NaN;
}

let shownScan = '';
function render(): void {
  // connection + bridge state
  const status = $('status');
  const bridge = conn.db.bridge.id.find(0);
  const bridgeAge = bridge ? Date.now() - Number(bridge.updatedAt.microsSinceUnixEpoch / 1000n) : Infinity;
  const statusText = status.querySelector('.status-text')!;
  if (!connected) {
    status.dataset.live = 'off';
    statusText.textContent = lastError ? 'Reconnecting' : 'Connecting';
  } else if (bridgeAge > 30000) {
    status.dataset.live = 'warn';
    statusText.textContent = 'Live · waiting for the scanner';
  } else if (bridge && !bridge.backendOk) {
    status.dataset.live = 'warn';
    statusText.textContent = 'Live · backend offline';
  } else {
    status.dataset.live = 'on';
    statusText.textContent = 'Live';
  }
  if (!ready) return;

  const id = focusScanId();
  const scan = id ? conn.db.scan.scanId.find(id) : undefined;
  app.dataset.state = scan ? 'scan' : 'waiting';
  if (id !== shownScan) {
    shownScan = id;
    $('findings').replaceChildren();
    $('mosaic').replaceChildren();
    $('strip').replaceChildren();
    scene?.clear();
    $('viewport').dataset.mode = 'photos';
  }
  if (!scan) {
    $('phase').textContent = 'Waiting for a scan';
    setText($('category'), '…');
    return;
  }
  const report = conn.db.report.scanId.find(id);
  const phase = report && scan.status !== 'inspecting' && scan.status !== 'running' ? 'done' : scan.status;
  $('phase').textContent = PHASE[phase] ?? human(phase);
  app.dataset.phase = ['capturing', 'inspecting', 'running'].includes(phase) ? 'active' : 'still';

  // what it is
  const ident = conn.db.objectIdentity.scanId.find(id);
  setText($('category'), ident ? ident.category : '…', true);
  const sub = ident
    ? [[ident.brand, ident.model].filter(Boolean).join(' '), ident.confidence && `${ident.confidence} confidence`]
        .filter(Boolean).join(' · ')
    : 'Identifying from the first photos';
  $('identity-sub').textContent = sub;

  // counters
  const stills = [...conn.db.still.iter()].filter((s) => s.scanId === id)
    .sort((a, b) => (a.capturedAt < b.capturedAt ? -1 : a.capturedAt > b.capturedAt ? 1 : a.stillId.localeCompare(b.stillId)));
  const p = conn.db.progress.scanId.find(id);
  count($('n-stills'), Math.max(stills.length, p?.stillsReceived ?? 0));
  count($('n-checked'), p?.areasChecked ?? 0);
  count($('n-unsure'), p?.couldNotJudge ?? 0);
  count($('n-look'), report ? report.issues : p?.toLookAt ?? 0);

  // verdict
  const v = $('verdict');
  if (report) {
    const findings = [...conn.db.finding.iter()].filter((f) => f.scanId === id && f.origin === 'report');
    const safety = findings.some((f) => f.severity === 'safety');
    const words: Record<string, [string, string]> = {
      looks_good: ['Looks good', 'var(--good)'],
      potential_issues: ['Potential issues', safety ? 'var(--danger)' : 'var(--caution)'],
      insufficient_evidence: ['Not enough to call it yet', 'var(--accent)'],
    };
    const [word, tone] = words[report.verdict] ?? [human(report.verdict), 'var(--accent)'];
    v.hidden = false;
    v.style.setProperty('--tone', tone);
    $('verdict-word').textContent = word;
    $('verdict-text').textContent = report.verdictText;
    const notes = $('safety');
    const want = report.safety.slice(0, 1).join('\n');
    if (notes.dataset.v !== want) {
      notes.dataset.v = want;
      notes.replaceChildren(...report.safety.slice(0, 1).map((t) => Object.assign(document.createElement('div'), { textContent: t })));
    }
  } else {
    v.hidden = true;
    $('safety').replaceChildren();
    $('safety').dataset.v = '';
  }

  renderFindings(id);
  renderPhotos(stills);
  render3d(id);

  const lat = median(latencies);
  $('meta').textContent = `Live from SpacetimeDB${Number.isFinite(lat) ? ` · event to screen ${Math.round(lat)} ms` : ''}`;
}

/** The scan's finding rows to show: one row per finding id (the bridge already keys live rows by the backend's place
 *  on the scan, and the report's rows by its own collapsing); once the report is in, only its rows. */
function findingRows(id: string) {
  const all = [...conn.db.finding.iter()].filter((f) => f.scanId === id);
  const reported = all.some((f) => f.origin === 'report');
  const seen = new Set<string>();
  return all.filter((f) => (!reported || f.origin === 'report') && !seen.has(f.findingId) && !!seen.add(f.findingId));
}

function renderFindings(id: string): void {
  const list = $('findings');
  const rows = findingRows(id)
    .sort((a, b) => (SEVERITY[a.severity]?.rank ?? 2) - (SEVERITY[b.severity]?.rank ?? 2) ||
      (a.origin === b.origin ? 0 : a.origin === 'report' ? -1 : 1) || a.rank - b.rank ||
      Number(a.updatedAt.microsSinceUnixEpoch - b.updatedAt.microsSinceUnixEpoch))
    .slice(0, 6);
  // FLIP: remember where rows were, re-order, then glide them from there
  const before = new Map<string, DOMRect>();
  for (const el of list.children) before.set((el as HTMLElement).dataset.id!, el.getBoundingClientRect());
  const keep = new Set(rows.map((r) => r.findingId));
  for (const el of [...list.children]) if (!keep.has((el as HTMLElement).dataset.id!)) el.remove();
  rows.forEach((f, i) => {
    let el = list.querySelector<HTMLElement>(`[data-id="${CSS.escape(f.findingId)}"]`);
    if (!el) {
      el = document.createElement('li');
      el.className = 'finding';
      el.dataset.id = f.findingId;
      el.innerHTML = '<span class="sev"></span><span class="what"></span><span class="words"></span>';
    }
    const sev = SEVERITY[f.severity] ?? SEVERITY[''];
    el.dataset.origin = f.origin;
    el.style.setProperty('--tone', sev.tone);
    el.querySelector('.sev')!.textContent = sev.word;
    el.querySelector('.what')!.textContent = [human(f.issue), f.partClass].filter(Boolean).join(' · ');
    el.querySelector('.words')!.textContent = [f.words, f.confidenceWord && `${f.confidenceWord} confidence`]
      .filter(Boolean).join(' · ');
    if (list.children[i] !== el) list.insertBefore(el, list.children[i] ?? null);
  });
  for (const el of list.children) {
    const h = el as HTMLElement, old = before.get(h.dataset.id!);
    if (!old) continue;
    const dy = old.top - h.getBoundingClientRect().top;
    if (Math.abs(dy) > 1) {
      h.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }], { duration: 500, easing: 'cubic-bezier(0.22,1,0.36,1)' });
    }
  }
}

function renderPhotos(stills: { stillId: string; thumbUrl: string }[]): void {
  const withThumb = stills.filter((s) => s.thumbUrl);
  const mosaic = $('mosaic');
  if (!withThumb.length) {
    if (!mosaic.querySelector('.empty-photos')) {
      mosaic.replaceChildren(Object.assign(document.createElement('div'), { className: 'empty-photos' }));
    }
    const box = mosaic.querySelector('.empty-photos')!;
    box.innerHTML = `${stills.length}<small>photos so far</small>`;
  } else {
    mosaic.querySelector('.empty-photos')?.remove();
    sync(mosaic, withThumb.slice(-24));
  }
  sync($('strip'), withThumb.slice(-14));
}

function sync(box: HTMLElement, stills: { stillId: string; thumbUrl: string }[]): void {
  const want = new Set(stills.map((s) => s.stillId));
  for (const el of [...box.querySelectorAll('img')]) if (!want.has(el.dataset.id!)) el.remove();
  for (const s of stills) {
    let img = box.querySelector<HTMLImageElement>(`img[data-id="${CSS.escape(s.stillId)}"]`);
    if (!img) {
      img = document.createElement('img');
      img.dataset.id = s.stillId;
      img.alt = '';
      img.decoding = 'async';
      img.src = s.thumbUrl;
      box.appendChild(img);
    }
  }
}

function render3d(id: string): void {
  const m = conn.db.mesh.scanId.find(id);
  const viewport = $('viewport');
  const note = $('viewport-note');
  if (!m || !(m.url || m.cloudUrl)) {
    note.textContent = m?.unavailable ? 'No 3D for this scan' : m ? '' : 'The 3D view appears when the scan is built';
    return;
  }
  if (m.hero === 'photos') {
    // The backend chose photos: the object is too small or too little surface was captured for a useful 3D view.
    // clear() also invalidates a 3D download still in flight, so its completion cannot switch the page back.
    scene?.clear();
    viewport.dataset.mode = 'photos';
    note.textContent = '';
    return;
  }
  const points = m.hero === 'points' && !!m.cloudUrl;
  const url = points ? m.cloudUrl : m.url || m.cloudUrl;
  void getScene().then((s) => s.load(url, points || !m.url)).then((ok) => {
    // The scan may have changed to photos (or another scan is shown) while the model downloaded.
    if (!ok || shownScan !== id || conn.db.mesh.scanId.find(id)?.hero === 'photos') return;
    viewport.dataset.mode = '3d';
    note.textContent = points ? 'LiDAR scan' : '3D mesh';
    // One pin per computed point: rows the backend located at the very same point are one place.
    const at = new Set<string>();
    const pins: Pin[] = findingRows(id)
      .filter((f) => f.hasPoint && !at.has(`${f.x},${f.y},${f.z}`) && !!at.add(`${f.x},${f.y},${f.z}`))
      .map((f) => ({ id: f.findingId, x: f.x, y: f.y, z: f.z, tone: f.severity }));
    scene?.setPins(pins);
  });
}

schedule();
