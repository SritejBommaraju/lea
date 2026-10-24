// lea live mirror: a SpacetimeDB module that holds what a scan looks like right now, for viewers that subscribe.
//
// The Python backend (gateway + worker) is the source of truth and does all compute. The bridge
// (backend/repairos/live/spacetime_bridge.py) reads its live events and calls the reducers below; nothing here
// decides anything about the object. Image bytes never come here (this database is in memory): a still has at most a
// thumbnail URL.
//
// Writes: only the bridge identity (env WRITER_IDENTITY) or the identity that published the database (the owner,
// recorded at init). Everyone else reads the public tables through subscriptions.
import { schema, table, t, SenderError, type InferSchema, type ReducerCtx } from 'spacetimedb/server';

// ------------------------------------------------------------------ tables
const scan = table(
  { name: 'scan', public: true },
  {
    scanId: t.string().primaryKey(),
    sessionId: t.string().index('btree'),
    status: t.string(), // capturing | inspecting | running | done | failed | closed (the pipeline run's status)
    startedAt: t.string(), // ISO time of the first thing seen for this scan (backend clock)
    error: t.string(), // "" unless the run failed
    sourceAt: t.string(), // backend event time of the last change (ISO, microseconds): for latency
    updatedAt: t.timestamp(), // when this database applied it
  }
);

const objectIdentity = table(
  { name: 'object_identity', public: true },
  {
    scanId: t.string().primaryKey(),
    category: t.string(), // a short generic kind of product, from the vision model, or "unknown"
    brand: t.string(), // "" when not printed or recognised beyond doubt
    model: t.string(),
    confidence: t.string(), // low | medium | high
    labelText: t.string(),
    sourceAt: t.string(),
    updatedAt: t.timestamp(),
  }
);

const progress = table(
  { name: 'progress', public: true },
  {
    scanId: t.string().primaryKey(),
    stillsReceived: t.u32(),
    stillsDone: t.u32(),
    stillsInspected: t.u32(),
    areasChecked: t.u32(),
    couldNotJudge: t.u32(),
    toLookAt: t.u32(),
    sourceAt: t.string(),
    updatedAt: t.timestamp(),
  }
);

const still = table(
  { name: 'still', public: true },
  {
    stillId: t.string().primaryKey(),
    scanId: t.string().index('btree'),
    segment: t.u32(),
    kind: t.string(), // guided | macro_detail | label | unguided
    capturedAt: t.string(),
    thumbUrl: t.string(), // "" = no thumbnail available (never image bytes)
    sourceAt: t.string(),
    updatedAt: t.timestamp(),
  }
);

const finding = table(
  { name: 'finding', public: true },
  {
    findingId: t.string().primaryKey(),
    scanId: t.string().index('btree'),
    origin: t.string(), // live (a finding row while the scan runs) | report (a ranked row of the finished report)
    rank: t.u32(), // report order (0 = most likely); live rows keep arrival order
    issue: t.string(), // the generic issue word (damaged, loose, ...)
    severity: t.string(), // safety | functional | cosmetic | "" (not judged)
    confidenceWord: t.string(), // low | medium | high | "" (live rows: not yet in words)
    words: t.string(), // plain words that let a person find the part
    partClass: t.string(), // generic class, never a product name
    hasPoint: t.bool(),
    x: t.f64(), // point on the scan, arkit_world metres (Y up); 0 when hasPoint is false
    y: t.f64(),
    z: t.f64(),
    sourceAt: t.string(),
    updatedAt: t.timestamp(),
  }
);

const report = table(
  { name: 'report', public: true },
  {
    scanId: t.string().primaryKey(),
    reportId: t.string(),
    rev: t.u32(),
    verdict: t.string(), // looks_good | potential_issues | insufficient_evidence (picked in backend code)
    verdictText: t.string(),
    summary: t.string(),
    safety: t.array(t.string()), // safety notes, shown before anything else
    issues: t.u32(),
    cosmetic: t.u32(),
    notInspected: t.u32(),
    insufficient: t.u32(),
    sourceAt: t.string(),
    updatedAt: t.timestamp(),
  }
);

const mesh = table(
  { name: 'mesh', public: true },
  {
    scanId: t.string().primaryKey(),
    rev: t.u32(),
    url: t.string(), // a short-lived signed URL to the PLY, "" when none can be served
    format: t.string(),
    triangles: t.u32(),
    hero: t.string(), // points | mesh | photos: which 3D view reads best
    cloudUrl: t.string(),
    cloudPoints: t.u32(),
    unavailable: t.string(), // why there is no mesh, "" otherwise
    sourceAt: t.string(),
    updatedAt: t.timestamp(),
  }
);

// One row (id 0): the scan the big screen shows. The bridge follows the latest active scan unless it is pinned.
const focus = table(
  { name: 'focus', public: true },
  {
    id: t.u32().primaryKey(),
    scanId: t.string(),
    pinned: t.bool(),
    sourceAt: t.string(),
    updatedAt: t.timestamp(),
  }
);

// One row (id 0): is the bridge alive and does it reach the backend.
const bridge = table(
  { name: 'bridge', public: true },
  {
    id: t.u32().primaryKey(),
    backendOk: t.bool(),
    note: t.string(),
    updatedAt: t.timestamp(),
  }
);

// Private: the publisher's identity, recorded at init (may also write).
const owner = table(
  { name: 'owner' },
  {
    id: t.u32().primaryKey(),
    identity: t.identity(),
  }
);

const spacetimedb = schema(
  { scan, objectIdentity, progress, still, finding, report, mesh, focus, bridge, owner },
  { env: { WRITER_IDENTITY: t.string().optional() } }
);
export default spacetimedb;

type Ctx = ReducerCtx<InferSchema<typeof spacetimedb>>;

// ------------------------------------------------------------------ helpers
function normHex(s: string): string {
  return s.trim().toLowerCase().replace(/^0x/, '');
}

function requireWriter(ctx: Ctx): void {
  const sender = normHex(ctx.sender.toHexString());
  const writer = ctx.env.WRITER_IDENTITY;
  if (writer && normHex(writer) === sender) return;
  const o = ctx.db.owner.id.find(0);
  if (o && normHex(o.identity.toHexString()) === sender) return;
  throw new SenderError('not allowed: only the bridge (WRITER_IDENTITY) or the owner may write');
}

function touchScan(ctx: Ctx, scanId: string, sessionId: string, sourceAt: string): void {
  const have = ctx.db.scan.scanId.find(scanId);
  if (have) {
    if (sessionId && have.sessionId !== sessionId) ctx.db.scan.scanId.update({ ...have, sessionId });
    return;
  }
  ctx.db.scan.insert({
    scanId, sessionId, status: 'capturing', startedAt: sourceAt, error: '', sourceAt, updatedAt: ctx.timestamp,
  });
}

function deleteFindings(ctx: Ctx, scanId: string): void {
  for (const f of [...ctx.db.finding.scanId.filter(scanId)]) ctx.db.finding.findingId.delete(f.findingId);
}

const FindingIn = t.object('FindingIn', {
  findingId: t.string(),
  rank: t.u32(),
  issue: t.string(),
  severity: t.string(),
  confidenceWord: t.string(),
  words: t.string(),
  partClass: t.string(),
  hasPoint: t.bool(),
  x: t.f64(),
  y: t.f64(),
  z: t.f64(),
});

// ------------------------------------------------------------------ lifecycle
export const init = spacetimedb.init((ctx) => {
  ctx.db.owner.insert({ id: 0, identity: ctx.sender });
});

// ------------------------------------------------------------------ reducers (bridge only)
export const upsert_scan = spacetimedb.reducer(
  { scanId: t.string(), sessionId: t.string(), status: t.string(), error: t.string(), sourceAt: t.string() },
  (ctx, { scanId, sessionId, status, error, sourceAt }) => {
    requireWriter(ctx);
    touchScan(ctx, scanId, sessionId, sourceAt);
    const have = ctx.db.scan.scanId.find(scanId)!;
    ctx.db.scan.scanId.update({ ...have, status: status || have.status, error, sourceAt, updatedAt: ctx.timestamp });
  }
);

export const set_identity = spacetimedb.reducer(
  {
    scanId: t.string(), sessionId: t.string(), category: t.string(), brand: t.string(), model: t.string(),
    confidence: t.string(), labelText: t.string(), sourceAt: t.string(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    touchScan(ctx, a.scanId, a.sessionId, a.sourceAt);
    const row = {
      scanId: a.scanId, category: a.category, brand: a.brand, model: a.model, confidence: a.confidence,
      labelText: a.labelText, sourceAt: a.sourceAt, updatedAt: ctx.timestamp,
    };
    if (ctx.db.objectIdentity.scanId.find(a.scanId)) ctx.db.objectIdentity.scanId.update(row);
    else ctx.db.objectIdentity.insert(row);
  }
);

export const set_progress = spacetimedb.reducer(
  {
    scanId: t.string(), sessionId: t.string(), stillsReceived: t.u32(), stillsDone: t.u32(),
    stillsInspected: t.u32(), areasChecked: t.u32(), couldNotJudge: t.u32(), toLookAt: t.u32(), sourceAt: t.string(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    touchScan(ctx, a.scanId, a.sessionId, a.sourceAt);
    const row = {
      scanId: a.scanId, stillsReceived: a.stillsReceived, stillsDone: a.stillsDone, stillsInspected: a.stillsInspected,
      areasChecked: a.areasChecked, couldNotJudge: a.couldNotJudge, toLookAt: a.toLookAt, sourceAt: a.sourceAt,
      updatedAt: ctx.timestamp,
    };
    if (ctx.db.progress.scanId.find(a.scanId)) ctx.db.progress.scanId.update(row);
    else ctx.db.progress.insert(row);
  }
);

export const add_still = spacetimedb.reducer(
  {
    stillId: t.string(), scanId: t.string(), sessionId: t.string(), segment: t.u32(), kind: t.string(),
    capturedAt: t.string(), thumbUrl: t.string(), sourceAt: t.string(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    touchScan(ctx, a.scanId, a.sessionId, a.sourceAt);
    const row = {
      stillId: a.stillId, scanId: a.scanId, segment: a.segment, kind: a.kind, capturedAt: a.capturedAt,
      thumbUrl: a.thumbUrl, sourceAt: a.sourceAt, updatedAt: ctx.timestamp,
    };
    if (ctx.db.still.stillId.find(a.stillId)) ctx.db.still.stillId.update(row);
    else ctx.db.still.insert(row);
  }
);

// A live finding while the scan runs (one with an issue). The finished report replaces them all (set_report).
export const upsert_finding = spacetimedb.reducer(
  { scanId: t.string(), sessionId: t.string(), finding: FindingIn, sourceAt: t.string() },
  (ctx, { scanId, sessionId, finding: f, sourceAt }) => {
    requireWriter(ctx);
    touchScan(ctx, scanId, sessionId, sourceAt);
    const have = ctx.db.finding.findingId.find(f.findingId);
    if (have && have.origin === 'report') return; // the report's rows stand
    const row = { ...f, scanId, origin: 'live', sourceAt, updatedAt: ctx.timestamp };
    if (have) ctx.db.finding.findingId.update(row);
    else ctx.db.finding.insert(row);
  }
);

// The finished report, and its ranked findings in place of the live ones, in one transaction: a viewer never sees a
// half-swapped list.
export const set_report = spacetimedb.reducer(
  {
    scanId: t.string(), sessionId: t.string(), reportId: t.string(), rev: t.u32(), verdict: t.string(),
    verdictText: t.string(), summary: t.string(), safety: t.array(t.string()), issues: t.u32(), cosmetic: t.u32(),
    notInspected: t.u32(), insufficient: t.u32(), findings: t.array(FindingIn), sourceAt: t.string(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    touchScan(ctx, a.scanId, a.sessionId, a.sourceAt);
    const have = ctx.db.report.scanId.find(a.scanId);
    if (have && have.rev > a.rev) return; // an older revision arriving late
    const row = {
      scanId: a.scanId, reportId: a.reportId, rev: a.rev, verdict: a.verdict, verdictText: a.verdictText,
      summary: a.summary, safety: a.safety, issues: a.issues, cosmetic: a.cosmetic, notInspected: a.notInspected,
      insufficient: a.insufficient, sourceAt: a.sourceAt, updatedAt: ctx.timestamp,
    };
    if (have) ctx.db.report.scanId.update(row);
    else ctx.db.report.insert(row);
    deleteFindings(ctx, a.scanId);
    for (const f of a.findings) {
      ctx.db.finding.insert({ ...f, scanId: a.scanId, origin: 'report', sourceAt: a.sourceAt, updatedAt: ctx.timestamp });
    }
  }
);

export const set_mesh = spacetimedb.reducer(
  {
    scanId: t.string(), sessionId: t.string(), rev: t.u32(), url: t.string(), format: t.string(), triangles: t.u32(),
    hero: t.string(), cloudUrl: t.string(), cloudPoints: t.u32(), unavailable: t.string(), sourceAt: t.string(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    touchScan(ctx, a.scanId, a.sessionId, a.sourceAt);
    const row = {
      scanId: a.scanId, rev: a.rev, url: a.url, format: a.format, triangles: a.triangles, hero: a.hero,
      cloudUrl: a.cloudUrl, cloudPoints: a.cloudPoints, unavailable: a.unavailable, sourceAt: a.sourceAt,
      updatedAt: ctx.timestamp,
    };
    if (ctx.db.mesh.scanId.find(a.scanId)) ctx.db.mesh.scanId.update(row);
    else ctx.db.mesh.insert(row);
  }
);

// The scan on the big screen. `pinned` holds it there (the bridge stops following new scans until unpinned).
export const set_focus = spacetimedb.reducer(
  { scanId: t.string(), pinned: t.bool(), sourceAt: t.string() },
  (ctx, { scanId, pinned, sourceAt }) => {
    requireWriter(ctx);
    const row = { id: 0, scanId, pinned, sourceAt, updatedAt: ctx.timestamp };
    if (ctx.db.focus.id.find(0)) ctx.db.focus.id.update(row);
    else ctx.db.focus.insert(row);
  }
);

// Follow a new scan unless the focus is pinned (the bridge calls this; set_focus overrides).
export const follow_scan = spacetimedb.reducer(
  { scanId: t.string(), sourceAt: t.string() },
  (ctx, { scanId, sourceAt }) => {
    requireWriter(ctx);
    const have = ctx.db.focus.id.find(0);
    if (have && (have.pinned || have.scanId === scanId)) return;
    const row = { id: 0, scanId, pinned: false, sourceAt, updatedAt: ctx.timestamp };
    if (have) ctx.db.focus.id.update(row);
    else ctx.db.focus.insert(row);
  }
);

export const heartbeat = spacetimedb.reducer(
  { backendOk: t.bool(), note: t.string() },
  (ctx, { backendOk, note }) => {
    requireWriter(ctx);
    const row = { id: 0, backendOk, note, updatedAt: ctx.timestamp };
    if (ctx.db.bridge.id.find(0)) ctx.db.bridge.id.update(row);
    else ctx.db.bridge.insert(row);
  }
);

// Drop a scan's rows (keeps the in-memory mirror small; the backend keeps everything).
export const forget_scan = spacetimedb.reducer({ scanId: t.string() }, (ctx, { scanId }) => {
  requireWriter(ctx);
  deleteFindings(ctx, scanId);
  for (const s of [...ctx.db.still.scanId.filter(scanId)]) ctx.db.still.stillId.delete(s.stillId);
  ctx.db.objectIdentity.scanId.delete(scanId);
  ctx.db.progress.scanId.delete(scanId);
  ctx.db.report.scanId.delete(scanId);
  ctx.db.mesh.scanId.delete(scanId);
  ctx.db.scan.scanId.delete(scanId);
});
