// Reducer bodies as plain functions, so tests can run them in SpacetimeDB's portable datastore.
import { SenderError, type InferSchema, type ReducerCtx } from 'spacetimedb/server';
import { Timestamp, type Identity } from 'spacetimedb';
import spacetimedb, { MAX_ASSET_BYTES, SHA256, ID, ASSET_KINDS, JOB_KINDS, TERMINAL, FINDING_SOURCES, VOICE_MODES, COMMAND_ORIGINS } from './schema';

type Ctx = ReducerCtx<InferSchema<typeof spacetimedb>>;

// ---------- guards ----------

function fail(message: string): never {
  throw new SenderError(message);
}

function isAdmin(ctx: Ctx, who: Identity): boolean {
  return ctx.db.admin.identity.find(who) != null;
}

function requireBridge(ctx: Ctx) {
  if (ctx.db.bridgeIdentity.identity.find(ctx.sender) == null) fail('caller is not an authorized bridge');
}

function requireId(label: string, value: string) {
  if (!ID.test(value)) fail(`${label} must be 1-64 characters of A-Z a-z 0-9 _ -`);
}

function oneOf<T extends string>(label: string, value: string, allowed: readonly T[]): T {
  if (!(allowed as readonly string[]).includes(value)) fail(`${label} must be one of ${allowed.join(', ')}`);
  return value as T;
}

function finite(label: string, value: number, positive = false) {
  if (!Number.isFinite(value) || (positive ? value <= 0 : value < 0)) fail(`${label} must be a finite ${positive ? 'positive' : 'non-negative'} number`);
}

function session(ctx: Ctx, id: string) {
  return ctx.db.repairSession.id.find(id) ?? fail(`no session ${id}`);
}

function assetIn(ctx: Ctx, sessionId: string, id: string, kinds?: readonly string[]) {
  const row = ctx.db.asset.id.find(id) ?? fail(`no asset ${id}`);
  if (row.sessionId !== sessionId) fail(`asset ${id} belongs to another session`);
  if (kinds && !kinds.includes(row.kind)) fail(`asset ${id} is ${row.kind}, expected ${kinds.join(' or ')}`);
  return row;
}

function emit(ctx: Ctx, sessionId: string, kind: string, entityId: string) {
  ctx.db.sessionEvent.insert({ seq: 0n, sessionId, kind, entityId, at: ctx.timestamp });
  const s = ctx.db.repairSession.id.find(sessionId);
  if (s) ctx.db.repairSession.id.update({ ...s, updatedAt: ctx.timestamp });
}

// ---------- identity ----------

export function onInit(ctx: Ctx) {
  ctx.db.admin.insert({ identity: ctx.sender });
}

export function authorizeBridgeImpl(ctx: Ctx, { identity, label }: { identity: Identity; label: string }) {
  if (!isAdmin(ctx, ctx.sender)) fail('only an admin may authorize a bridge');
  if (ctx.db.bridgeIdentity.identity.find(identity)) return;
  ctx.db.bridgeIdentity.insert({ identity, label });
}

export function revokeBridgeImpl(ctx: Ctx, { identity }: { identity: Identity }) {
  if (!isAdmin(ctx, ctx.sender)) fail('only an admin may revoke a bridge');
  ctx.db.bridgeIdentity.identity.delete(identity);
}

// ---------- sessions and assets ----------

export function createSessionImpl(ctx: Ctx, a: { id: string; title: string }) {
  requireBridge(ctx);
  requireId('session id', a.id);
  const existing = ctx.db.repairSession.id.find(a.id);
  if (existing) {
    if (existing.title !== a.title) fail(`session ${a.id} exists with a different title`);
    return;
  }
  ctx.db.repairSession.insert({
    id: a.id, title: a.title.slice(0, 200), grainProjectId: '', referenceMeshAssetId: '',
    activeDesignVersionId: '', createdAt: ctx.timestamp, updatedAt: ctx.timestamp,
  });
  emit(ctx, a.id, 'session_created', a.id);
}

export function linkGrainProjectImpl(ctx: Ctx, a: { sessionId: string; grainProjectId: string }) {
  requireBridge(ctx);
  const s = session(ctx, a.sessionId);
  if (!/^[0-9a-f]{12}$/.test(a.grainProjectId)) fail('Grain project ids are 12 hex characters');
  if (s.grainProjectId && s.grainProjectId !== a.grainProjectId) fail('session is already linked to another Grain project');
  ctx.db.repairSession.id.update({ ...s, grainProjectId: a.grainProjectId });
  emit(ctx, a.sessionId, 'grain_linked', a.grainProjectId);
}

export function registerAssetImpl(ctx: Ctx, a: {
  id: string; sessionId: string; kind: string; sha256: string; bytes: bigint; mediaType: string;
  sourceAssetIds: string[]; metaJson: string;
}) {
  requireBridge(ctx);
  requireId('asset id', a.id);
  session(ctx, a.sessionId);
  oneOf('asset kind', a.kind, ASSET_KINDS);
  if (!SHA256.test(a.sha256)) fail('sha256 must be 64 lowercase hex characters');
  if (a.bytes <= 0n || a.bytes > MAX_ASSET_BYTES) fail('asset size out of range');
  if (a.metaJson.length > 16_384) fail('asset metadata is limited to 16 KB');
  for (const src of a.sourceAssetIds) assetIn(ctx, a.sessionId, src);
  const storageKey = `sha256/${a.sha256}`;  // content-addressed; the bridge writes exactly here
  const existing = ctx.db.asset.id.find(a.id);
  if (existing) {
    if (existing.sha256 !== a.sha256 || existing.sessionId !== a.sessionId) fail(`asset ${a.id} already exists with different content`);
    return;
  }
  ctx.db.asset.insert({ ...a, storageKey, createdAt: ctx.timestamp });
  emit(ctx, a.sessionId, 'asset_registered', a.id);
}

export function pinReferenceMeshImpl(ctx: Ctx, a: { sessionId: string; assetId: string }) {
  requireBridge(ctx);
  const s = session(ctx, a.sessionId);
  assetIn(ctx, a.sessionId, a.assetId, ['reference_mesh']);
  if (s.referenceMeshAssetId && s.referenceMeshAssetId !== a.assetId) {
    fail('this session is pinned to another reference; start a new session to change it');
  }
  ctx.db.repairSession.id.update({ ...s, referenceMeshAssetId: a.assetId });
  emit(ctx, a.sessionId, 'reference_pinned', a.assetId);
}

export function setQaPolicyImpl(ctx: Ctx, a: {
  id: string; sessionId: string; scaleAnchorLabel: string; scaleAnchorMm: number; holdoutLabel: string;
  holdoutMm: number; maxHoldoutErrorMm: number; minCoverage: number; maxFitRmsMm: number; minFitInlierShare: number;
}) {
  requireBridge(ctx);
  requireId('policy id', a.id);
  session(ctx, a.sessionId);
  if (ctx.db.qaPolicy.id.find(a.id)) fail(`policy ${a.id} is frozen; create a new one`);
  finite('scale anchor', a.scaleAnchorMm, true);
  finite('holdout', a.holdoutMm, true);
  finite('max holdout error', a.maxHoldoutErrorMm, true);
  finite('max fit RMS', a.maxFitRmsMm, true);
  for (const [k, v] of [['min coverage', a.minCoverage], ['min inlier share', a.minFitInlierShare]] as const) {
    if (!(v > 0 && v <= 1)) fail(`${k} must be in (0, 1]`);
  }
  if (!a.scaleAnchorLabel.trim() || !a.holdoutLabel.trim()) fail('name both measured dimensions');
  if (a.scaleAnchorLabel.trim().toLowerCase() === a.holdoutLabel.trim().toLowerCase()) {
    fail('the holdout must be a different dimension from the scale anchor');
  }
  ctx.db.qaPolicy.insert({ ...a, createdAt: ctx.timestamp });
  emit(ctx, a.sessionId, 'qa_policy_set', a.id);
}

// ---------- jobs ----------

export function enqueueJobImpl(ctx: Ctx, a: {
  id: string; sessionId: string; kind: string; idempotencyKey: string; inputHash: string; inputJson: string;
}) {
  requireBridge(ctx);
  requireId('job id', a.id);
  session(ctx, a.sessionId);
  oneOf('job kind', a.kind, JOB_KINDS);
  if (!SHA256.test(a.inputHash)) fail('inputHash must be a sha256 hex digest');
  insertJob(ctx, a);
}

function insertJob(ctx: Ctx, a: { id: string; sessionId: string; kind: string; idempotencyKey: string; inputHash: string; inputJson: string }) {
  if (a.idempotencyKey.length < 8 || a.idempotencyKey.length > 200) fail('idempotency key must be 8-200 characters');
  const prior = ctx.db.job.idempotencyKey.find(a.idempotencyKey);
  if (prior) {
    if (prior.inputHash !== a.inputHash || prior.kind !== a.kind) fail('idempotency key reused with a different request');
    return prior;
  }
  if (a.kind === 'reconstruction') validateReconstructionInput(ctx, a.sessionId, a.inputJson);
  const row = {
    ...a, status: 'queued', attempt: 0, leaseOwner: '', leaseExpiresAt: ctx.timestamp, progress: '',
    resultJson: '', error: '', createdAt: ctx.timestamp, updatedAt: ctx.timestamp,
  };
  ctx.db.job.insert(row);
  emit(ctx, a.sessionId, 'job_queued', a.id);
  return row;
}

function validateReconstructionInput(ctx: Ctx, sessionId: string, inputJson: string) {
  let input: { photoAssetIds?: unknown; policyId?: unknown };
  try { input = JSON.parse(inputJson); } catch { fail('reconstruction input is not JSON'); }
  const ids = input.photoAssetIds;
  if (!Array.isArray(ids) || ids.length < 20) fail('reconstruction needs at least 20 overlapping orbit photos');
  for (const id of ids) assetIn(ctx, sessionId, String(id), ['photo_orbit']);
  if (typeof input.policyId !== 'string') fail('reconstruction needs a frozen QA policy');
  const policy = ctx.db.qaPolicy.id.find(input.policyId);
  if (!policy || policy.sessionId !== sessionId) fail('QA policy not found in this session');
}

export function claimJobImpl(ctx: Ctx, a: { jobId: string; worker: string; leaseSeconds: number }) {
  requireBridge(ctx);
  const j = ctx.db.job.id.find(a.jobId) ?? fail(`no job ${a.jobId}`);
  const now = ctx.timestamp.microsSinceUnixEpoch;
  const expired = j.status === 'running' && j.leaseExpiresAt.microsSinceUnixEpoch < now;
  if (j.status !== 'queued' && !expired) fail(`job ${j.id} is ${j.status}`);
  if (!(a.leaseSeconds >= 5 && a.leaseSeconds <= 3600)) fail('lease must be 5-3600 s');
  const leaseExpiresAt = new Timestamp(now + BigInt(Math.round(a.leaseSeconds * 1e6)));
  ctx.db.job.id.update({ ...j, status: 'running', attempt: j.attempt + 1, leaseOwner: a.worker, leaseExpiresAt, updatedAt: ctx.timestamp });
  emit(ctx, j.sessionId, 'job_running', j.id);
}

function runningJob(ctx: Ctx, jobId: string, attempt: number) {
  const j = ctx.db.job.id.find(jobId) ?? fail(`no job ${jobId}`);
  if (j.status !== 'running') fail(`job ${jobId} is ${j.status}, not running`);
  if (j.attempt !== attempt) fail(`stale attempt ${attempt}; job is on attempt ${j.attempt}`);
  return j;
}

export function reportProgressImpl(ctx: Ctx, a: { jobId: string; attempt: number; progress: string; leaseSeconds: number }) {
  requireBridge(ctx);
  const j = runningJob(ctx, a.jobId, a.attempt);
  const now = ctx.timestamp.microsSinceUnixEpoch;
  const leaseExpiresAt = new Timestamp(now + BigInt(Math.round(Math.min(Math.max(a.leaseSeconds, 5), 3600) * 1e6)));
  ctx.db.job.id.update({ ...j, progress: a.progress.slice(0, 500), leaseExpiresAt, updatedAt: ctx.timestamp });
  emit(ctx, j.sessionId, 'job_progress', j.id);
}

export function finishJobImpl(ctx: Ctx, a: { jobId: string; attempt: number; status: string; resultJson: string; error: string }) {
  requireBridge(ctx);
  const j = runningJob(ctx, a.jobId, a.attempt);
  const status = oneOf('status', a.status, TERMINAL);
  if (status === 'succeeded' && (j.kind === 'reconstruction' || j.kind === 'design_turn')) {
    fail(`${j.kind} jobs succeed only through their own result reducer`);
  }
  if (a.resultJson.length > 64_000) fail('result too large; store it as an asset');
  ctx.db.job.id.update({ ...j, status, resultJson: a.resultJson, error: a.error.slice(0, 4000), updatedAt: ctx.timestamp });
  if (j.kind === 'design_turn') {
    const cmd = [...ctx.db.designCommand.iter()].find(c => c.jobId === j.id);
    if (cmd) ctx.db.designCommand.id.update({ ...cmd, status, updatedAt: ctx.timestamp });
  }
  emit(ctx, j.sessionId, `job_${status}`, j.id);
}

// ---------- reconstruction result and the mesh gate ----------

export function recordMeshReportImpl(ctx: Ctx, a: {
  id: string; jobId: string; attempt: number; meshAssetId: string; reportAssetId: string; engine: string;
  scaleFactor: number; measuredHoldoutMm: number; coverage: number; fitRmsMm: number; fitInlierShare: number;
  referenceMeshAssetId: string;
}) {
  requireBridge(ctx);
  requireId('report id', a.id);
  const j = runningJob(ctx, a.jobId, a.attempt);
  if (j.kind !== 'reconstruction') fail('mesh reports come only from reconstruction jobs');
  const s = session(ctx, j.sessionId);
  const input = JSON.parse(j.inputJson) as { policyId: string };
  const policy = ctx.db.qaPolicy.id.find(input.policyId) ?? fail('QA policy vanished');
  assetIn(ctx, j.sessionId, a.meshAssetId, ['recon_mesh']);
  assetIn(ctx, j.sessionId, a.reportAssetId, ['qa_report']);

  // The verdict is computed here from numbers, never accepted from the worker.
  const failed: string[] = [];
  const holdoutErrorMm = Math.abs(a.measuredHoldoutMm - policy.holdoutMm);
  if (!(Number.isFinite(a.scaleFactor) && a.scaleFactor > 0)) failed.push('scale');
  if (!(Number.isFinite(holdoutErrorMm) && holdoutErrorMm <= policy.maxHoldoutErrorMm)) failed.push('holdout');
  if (!(a.coverage >= policy.minCoverage)) failed.push('coverage');
  if (!a.referenceMeshAssetId) {
    failed.push('reference_missing');
  } else {
    if (a.referenceMeshAssetId !== s.referenceMeshAssetId) failed.push('reference_not_pinned');
    if (!(a.fitRmsMm <= policy.maxFitRmsMm)) failed.push('fit_rms');
    if (!(a.fitInlierShare >= policy.minFitInlierShare)) failed.push('fit_inliers');
  }
  const verdict = failed.length === 0 ? 'pass' : 'fail';
  const nan = (v: number) => (Number.isFinite(v) ? v : -1);
  ctx.db.meshReport.insert({
    id: a.id, sessionId: j.sessionId, jobId: j.id, policyId: policy.id, meshAssetId: a.meshAssetId,
    reportAssetId: a.reportAssetId, referenceMeshAssetId: a.referenceMeshAssetId, engine: a.engine.slice(0, 200),
    scaleFactor: nan(a.scaleFactor), measuredHoldoutMm: nan(a.measuredHoldoutMm), holdoutErrorMm: nan(holdoutErrorMm),
    coverage: nan(a.coverage), fitRmsMm: nan(a.fitRmsMm), fitInlierShare: nan(a.fitInlierShare),
    verdict, failedChecks: failed, createdAt: ctx.timestamp,
  });
  ctx.db.job.id.update({
    ...j, status: verdict === 'pass' ? 'succeeded' : 'needs_user', progress: '',
    resultJson: JSON.stringify({ meshReportId: a.id, verdict, failedChecks: failed }), updatedAt: ctx.timestamp,
  });
  emit(ctx, j.sessionId, 'mesh_report', a.id);
}

function passingReport(ctx: Ctx, sessionId: string, id: string) {
  const r = ctx.db.meshReport.id.find(id) ?? fail(`no mesh report ${id}`);
  if (r.sessionId !== sessionId) fail('mesh report belongs to another session');
  if (r.verdict !== 'pass') fail(`mesh report ${id} failed ${r.failedChecks.join(', ')}; it cannot drive diagnosis or CAD`);
  return r;
}

// ---------- findings, checks and repairs ----------

export function recordFindingImpl(ctx: Ctx, a: {
  id: string; sessionId: string; subject: string; problem: string; source: string; evidenceAssetIds: string[];
  referenceAssetId: string; meshReportId: string; regionJson: string; confidenceKind: string; confidenceText: string; detail: string;
}) {
  requireBridge(ctx);
  requireId('finding id', a.id);
  session(ctx, a.sessionId);
  const source = oneOf('source', a.source, FINDING_SOURCES);
  oneOf('confidence kind', a.confidenceKind, ['model_word', 'measured', 'none'] as const);
  if (a.evidenceAssetIds.length === 0) fail('a finding needs at least one evidence asset');
  for (const id of a.evidenceAssetIds) assetIn(ctx, a.sessionId, id);
  if (!a.referenceAssetId) fail('a finding must name the saved reference it was compared with');
  assetIn(ctx, a.sessionId, a.referenceAssetId, ['reference_mesh', 'reference_photo']);
  if (source === 'geometry') passingReport(ctx, a.sessionId, a.meshReportId);
  else if (a.meshReportId) passingReport(ctx, a.sessionId, a.meshReportId);
  if (ctx.db.finding.id.find(a.id)) return;
  ctx.db.finding.insert({
    ...a, subject: a.subject.slice(0, 200), problem: a.problem.slice(0, 200), detail: a.detail.slice(0, 4000),
    state: 'candidate', createdAt: ctx.timestamp, updatedAt: ctx.timestamp,
  });
  emit(ctx, a.sessionId, 'finding', a.id);
}

export function addRepairCheckImpl(ctx: Ctx, a: {
  id: string; sessionId: string; findingId: string; purpose: string; instruction: string; safety: string; powerState: string;
  expectedMin?: number; expectedMax?: number; expectedUnit: string; expectedSource: string;
}) {
  requireBridge(ctx);
  requireId('check id', a.id);
  const f = ctx.db.finding.id.find(a.findingId) ?? fail(`no finding ${a.findingId}`);
  if (f.sessionId !== a.sessionId) fail('finding belongs to another session');
  oneOf('purpose', a.purpose, ['diagnose', 'verify_repair'] as const);
  oneOf('power state', a.powerState, ['off', 'on_low_voltage', 'not_applicable'] as const);
  if (!a.safety.trim()) fail('every check states its safety precondition');
  const hasRange = a.expectedMin !== undefined || a.expectedMax !== undefined;
  if (hasRange && (!a.expectedUnit || !a.expectedSource)) fail('an expected range needs a unit and the source it came from');
  if (a.expectedUnit === 'ohm' && a.powerState !== 'off') fail('resistance and continuity are measured with power off');
  if (ctx.db.repairCheck.id.find(a.id)) return;
  ctx.db.repairCheck.insert({
    ...a, expectedMin: a.expectedMin, expectedMax: a.expectedMax, outcome: 'pending', observationAssetId: '',
    readingValue: undefined, readingUnit: '', note: '', createdAt: ctx.timestamp, updatedAt: ctx.timestamp,
  });
  emit(ctx, a.sessionId, 'repair_check', a.id);
}

/** Records what the person saw or measured. A numeric reading is judged here against the sourced range;
 *  without evidence or a range the outcome is `unverified`, whatever the person tapped. */
export function recordCheckOutcomeImpl(ctx: Ctx, a: {
  checkId: string; observed: string; observationAssetId: string; readingValue?: number; readingUnit: string; note: string;
}) {
  requireBridge(ctx);
  const c = ctx.db.repairCheck.id.find(a.checkId) ?? fail(`no check ${a.checkId}`);
  if (c.outcome !== 'pending') fail(`check ${c.id} already recorded ${c.outcome}; add a new check to re-measure`);
  const observed = oneOf('observed', a.observed, ['as_expected', 'not_as_expected', 'could_not_check'] as const);
  if (a.observationAssetId) assetIn(ctx, c.sessionId, a.observationAssetId);
  let outcome: string;
  const hasRange = c.expectedMin !== undefined || c.expectedMax !== undefined;
  if (a.readingValue !== undefined) {
    if (!Number.isFinite(a.readingValue)) fail('reading must be a finite number');
    if (a.readingUnit !== c.expectedUnit) fail(`reading unit ${a.readingUnit} does not match expected ${c.expectedUnit || 'none'}`);
    if (!hasRange) outcome = 'unverified';
    else {
      const inRange = (c.expectedMin === undefined || a.readingValue >= c.expectedMin) && (c.expectedMax === undefined || a.readingValue <= c.expectedMax);
      outcome = inRange ? 'as_expected' : 'not_as_expected';
    }
  } else if (observed !== 'could_not_check' && a.observationAssetId) {
    outcome = observed;
  } else {
    outcome = 'unverified';
  }
  ctx.db.repairCheck.id.update({
    ...c, outcome, observationAssetId: a.observationAssetId, readingValue: a.readingValue, readingUnit: a.readingUnit,
    note: a.note.slice(0, 2000), updatedAt: ctx.timestamp,
  });

  const f = ctx.db.finding.id.find(c.findingId);
  if (f && c.purpose === 'diagnose' && f.state === 'candidate') {
    const state = outcome === 'not_as_expected' ? 'confirmed' : outcome === 'as_expected' ? 'ruled_out' : 'unverified';
    ctx.db.finding.id.update({ ...f, state, updatedAt: ctx.timestamp });
  }
  if (c.purpose === 'verify_repair') {
    const attempt = [...ctx.db.repairAttempt.iter()].find(r => r.verifyCheckId === c.id);
    if (attempt) {
      const result = outcome === 'as_expected' ? 'fixed_verified' : outcome === 'not_as_expected' ? 'not_fixed' : 'unverified';
      ctx.db.repairAttempt.id.update({ ...attempt, result, updatedAt: ctx.timestamp });
      emit(ctx, c.sessionId, 'repair_result', attempt.id);
    }
  }
  emit(ctx, c.sessionId, 'check_outcome', c.id);
}

export function recordRepairAttemptImpl(ctx: Ctx, a: { id: string; sessionId: string; findingId: string; action: string; verifyCheckId: string }) {
  requireBridge(ctx);
  requireId('repair id', a.id);
  const f = ctx.db.finding.id.find(a.findingId) ?? fail(`no finding ${a.findingId}`);
  if (f.sessionId !== a.sessionId) fail('finding belongs to another session');
  const c = ctx.db.repairCheck.id.find(a.verifyCheckId) ?? fail('a repair needs a verification check');
  if (c.purpose !== 'verify_repair' || c.findingId !== f.id) fail('verification check must be a verify_repair check on the same finding');
  if (ctx.db.repairAttempt.id.find(a.id)) return;
  ctx.db.repairAttempt.insert({ ...a, action: a.action.slice(0, 1000), result: 'pending', createdAt: ctx.timestamp, updatedAt: ctx.timestamp });
  emit(ctx, a.sessionId, 'repair_attempt', a.id);
}

// ---------- voice and design ----------

export function recordVoiceTurnImpl(ctx: Ctx, a: { id: string; sessionId: string; mode: string; transcript: string; replyText: string; audioAssetId: string; commandId: string }) {
  requireBridge(ctx);
  requireId('turn id', a.id);
  session(ctx, a.sessionId);
  oneOf('mode', a.mode, VOICE_MODES);
  if (!a.transcript.trim()) fail('only final, non-empty transcripts are recorded');
  if (a.audioAssetId) assetIn(ctx, a.sessionId, a.audioAssetId, ['voice']);
  if (ctx.db.voiceTurn.id.find(a.id)) return;
  ctx.db.voiceTurn.insert({ ...a, transcript: a.transcript.slice(0, 4000), replyText: a.replyText.slice(0, 4000), createdAt: ctx.timestamp });
  emit(ctx, a.sessionId, 'voice_turn', a.id);
}

export function submitDesignCommandImpl(ctx: Ctx, a: {
  id: string; sessionId: string; origin: string; text: string; meshReportId: string; idempotencyKey: string; jobId: string; inputHash: string;
}) {
  requireBridge(ctx);
  requireId('command id', a.id);
  requireId('job id', a.jobId);
  const s = session(ctx, a.sessionId);
  const origin = oneOf('origin', a.origin, COMMAND_ORIGINS);
  if (!s.grainProjectId) fail('link a Grain project before sending design changes');
  if (!a.text.trim() || a.text.length > 20_000) fail('design text must be 1-20000 characters');
  if (a.meshReportId) passingReport(ctx, a.sessionId, a.meshReportId);
  if (origin === 'auto') {
    if (!a.meshReportId) fail('automatic design requests need a passing mesh report');
    // One scan + objective creates at most one automatic Grain turn (handoff §3).
    const prior = [...ctx.db.designCommand.iter()].find(c => c.origin === 'auto' && c.meshReportId === a.meshReportId);
    if (prior && prior.idempotencyKey !== a.idempotencyKey) fail(`mesh report ${a.meshReportId} already produced command ${prior.id}`);
  }
  const prior = ctx.db.designCommand.idempotencyKey.find(a.idempotencyKey);
  if (prior) {
    if (prior.text !== a.text || prior.sessionId !== a.sessionId) fail('idempotency key reused with a different command');
    return;
  }
  insertJob(ctx, {
    id: a.jobId, sessionId: a.sessionId, kind: 'design_turn', idempotencyKey: `job:${a.idempotencyKey}`,
    inputHash: a.inputHash, inputJson: JSON.stringify({ commandId: a.id }),
  });
  ctx.db.designCommand.insert({
    id: a.id, sessionId: a.sessionId, origin, text: a.text, meshReportId: a.meshReportId, idempotencyKey: a.idempotencyKey,
    jobId: a.jobId, grainProjectId: s.grainProjectId, preVersionId: '', postVersionId: '', status: 'queued',
    createdAt: ctx.timestamp, updatedAt: ctx.timestamp,
  });
  emit(ctx, a.sessionId, 'design_command', a.id);
}

export function recordDesignVersionImpl(ctx: Ctx, a: {
  id: string; sessionId: string; commandId: string; phase: string; manifestAssetId: string; fileCount: number;
  buildOk?: boolean; jobId: string; attempt: number;
}) {
  requireBridge(ctx);
  requireId('version id', a.id);
  const s = session(ctx, a.sessionId);
  const phase = oneOf('phase', a.phase, ['baseline', 'pre', 'post'] as const);
  assetIn(ctx, a.sessionId, a.manifestAssetId, ['design_snapshot']);
  if (ctx.db.designVersion.id.find(a.id)) return;
  let cmd: any;
  if (phase !== 'baseline') {
    cmd = ctx.db.designCommand.id.find(a.commandId) ?? fail(`no command ${a.commandId}`);
    runningJob(ctx, a.jobId, a.attempt);
    if (cmd.jobId !== a.jobId) fail('version and job do not match');
  }
  ctx.db.designVersion.insert({
    id: a.id, sessionId: a.sessionId, grainProjectId: s.grainProjectId, commandId: a.commandId, phase,
    manifestAssetId: a.manifestAssetId, fileCount: a.fileCount, buildOk: a.buildOk, createdAt: ctx.timestamp,
  });
  if (phase === 'pre') {
    ctx.db.designCommand.id.update({ ...cmd, preVersionId: a.id, status: 'running', updatedAt: ctx.timestamp });
  } else if (phase === 'post') {
    if (!cmd.preVersionId) fail('save the pre-change version before the post-change version');
    // undefined: Grain stopped to ask a question, so the change is neither built nor failed.
    const status = a.buildOk === true ? 'succeeded' : a.buildOk === false ? 'failed' : 'needs_user';
    ctx.db.designCommand.id.update({ ...cmd, postVersionId: a.id, status, updatedAt: ctx.timestamp });
    const j = runningJob(ctx, a.jobId, a.attempt);
    ctx.db.job.id.update({ ...j, status, resultJson: JSON.stringify({ postVersionId: a.id, buildOk: a.buildOk ?? null }), updatedAt: ctx.timestamp });
    // Only a successful build becomes the active version; a failed attempt never replaces it.
    if (a.buildOk === true) ctx.db.repairSession.id.update({ ...session(ctx, a.sessionId), activeDesignVersionId: a.id, updatedAt: ctx.timestamp });
  } else if (!s.activeDesignVersionId) {
    ctx.db.repairSession.id.update({ ...s, activeDesignVersionId: a.id, updatedAt: ctx.timestamp });
  }
  emit(ctx, a.sessionId, 'design_version', a.id);
}

