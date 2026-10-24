// Lea state module (SpacetimeDB 2.13). Every base table is private; only identities an admin
// authorized as a bridge may call reducers or read the bridge_* views. Gates that decide whether
// evidence may drive diagnosis or CAD live here, in code, so no client can skip them.
import { schema, table, t } from 'spacetimedb/server';

export const MAX_ASSET_BYTES = 2n * 1024n * 1024n * 1024n;
export const SHA256 = /^[0-9a-f]{64}$/;
export const ID = /^[A-Za-z0-9_-]{1,64}$/;

export const ASSET_KINDS = [
  'photo_orbit', 'photo_closeup', 'photo_observation', 'voice', 'reference_mesh', 'reference_photo',
  'recon_mesh', 'recon_bundle', 'derived_ply', 'qa_report', 'design_snapshot', 'design_file',
] as const;
export const JOB_KINDS = ['reconstruction', 'geometry_compare', 'closeup_compare', 'design_turn'] as const;
// queued -> running -> succeeded | failed | needs_user | uncertain (handoff §3 job states)
export const TERMINAL = ['succeeded', 'failed', 'needs_user', 'uncertain'] as const;
export const FINDING_SOURCES = ['geometry', 'vision', 'meter', 'user'] as const;
export const VOICE_MODES = ['wake', 'ptt'] as const;
export const COMMAND_ORIGINS = ['voice', 'text', 'auto'] as const;

const admin = table({ name: 'admin' }, { identity: t.identity().primaryKey() });
const bridgeIdentity = table({ name: 'bridge_identity' }, {
  identity: t.identity().primaryKey(),
  label: t.string(),
});

const repairSession = table({ name: 'repair_session' }, {
  id: t.string().primaryKey(),
  title: t.string(),
  grainProjectId: t.string(),          // '' until linked
  referenceMeshAssetId: t.string(),    // the saved reference every scan in this session is pinned to
  activeDesignVersionId: t.string(),   // last successful post-build snapshot
  createdAt: t.timestamp(),
  updatedAt: t.timestamp(),
});

const asset = table({ name: 'asset' }, {
  id: t.string().primaryKey(),
  sessionId: t.string().index('btree'),
  kind: t.string(),
  sha256: t.string(),
  bytes: t.u64(),
  mediaType: t.string(),
  storageKey: t.string(),
  sourceAssetIds: t.array(t.string()),  // derived assets name the originals they came from
  metaJson: t.string(),                 // capture metadata (dimensions, time, crop); small
  createdAt: t.timestamp(),
});

// Owner-frozen acceptance limits (handoff O9). No defaults exist in code.
const qaPolicy = table({ name: 'qa_policy' }, {
  id: t.string().primaryKey(),
  sessionId: t.string().index('btree'),
  scaleAnchorLabel: t.string(),
  scaleAnchorMm: t.f64(),
  holdoutLabel: t.string(),
  holdoutMm: t.f64(),
  maxHoldoutErrorMm: t.f64(),
  minCoverage: t.f64(),
  maxFitRmsMm: t.f64(),
  minFitInlierShare: t.f64(),
  createdAt: t.timestamp(),
});

const job = table({ name: 'job' }, {
  id: t.string().primaryKey(),
  sessionId: t.string().index('btree'),
  kind: t.string(),
  status: t.string().index('btree'),
  idempotencyKey: t.string().unique(),
  inputHash: t.string(),
  inputJson: t.string(),
  attempt: t.u32(),
  leaseOwner: t.string(),
  leaseExpiresAt: t.timestamp(),
  progress: t.string(),
  resultJson: t.string(),
  error: t.string(),
  createdAt: t.timestamp(),
  updatedAt: t.timestamp(),
});

const meshReport = table({ name: 'mesh_report' }, {
  id: t.string().primaryKey(),
  sessionId: t.string().index('btree'),
  jobId: t.string(),
  policyId: t.string(),
  meshAssetId: t.string(),
  reportAssetId: t.string(),
  referenceMeshAssetId: t.string(),
  engine: t.string(),
  scaleFactor: t.f64(),
  measuredHoldoutMm: t.f64(),
  holdoutErrorMm: t.f64(),
  coverage: t.f64(),
  fitRmsMm: t.f64(),
  fitInlierShare: t.f64(),
  verdict: t.string(),            // 'pass' | 'fail'
  failedChecks: t.array(t.string()),
  createdAt: t.timestamp(),
});

const finding = table({ name: 'finding' }, {
  id: t.string().primaryKey(),
  sessionId: t.string().index('btree'),
  subject: t.string(),
  problem: t.string(),
  source: t.string(),
  // candidate -> confirmed | ruled_out | unverified; never 'ok' by default
  state: t.string(),
  evidenceAssetIds: t.array(t.string()),
  referenceAssetId: t.string(),
  meshReportId: t.string(),
  regionJson: t.string(),
  confidenceKind: t.string(),     // 'model_word' | 'measured' | 'none'
  confidenceText: t.string(),
  detail: t.string(),
  createdAt: t.timestamp(),
  updatedAt: t.timestamp(),
});

const repairCheck = table({ name: 'repair_check' }, {
  id: t.string().primaryKey(),
  sessionId: t.string().index('btree'),
  findingId: t.string(),
  purpose: t.string(),            // 'diagnose' | 'verify_repair'
  instruction: t.string(),
  safety: t.string(),
  powerState: t.string(),         // 'off' | 'on_low_voltage' | 'not_applicable'
  expectedMin: t.option(t.f64()),
  expectedMax: t.option(t.f64()),
  expectedUnit: t.string(),
  expectedSource: t.string(),
  // pending -> as_expected | not_as_expected | unverified
  outcome: t.string(),
  observationAssetId: t.string(),
  readingValue: t.option(t.f64()),
  readingUnit: t.string(),
  note: t.string(),
  createdAt: t.timestamp(),
  updatedAt: t.timestamp(),
});

const repairAttempt = table({ name: 'repair_attempt' }, {
  id: t.string().primaryKey(),
  sessionId: t.string().index('btree'),
  findingId: t.string(),
  action: t.string(),
  verifyCheckId: t.string(),
  result: t.string(),             // 'pending' | 'fixed_verified' | 'not_fixed' | 'unverified'
  createdAt: t.timestamp(),
  updatedAt: t.timestamp(),
});

const voiceTurn = table({ name: 'voice_turn' }, {
  id: t.string().primaryKey(),
  sessionId: t.string().index('btree'),
  mode: t.string(),
  transcript: t.string(),
  replyText: t.string(),
  audioAssetId: t.string(),
  commandId: t.string(),
  createdAt: t.timestamp(),
});

const designCommand = table({ name: 'design_command' }, {
  id: t.string().primaryKey(),
  sessionId: t.string().index('btree'),
  origin: t.string(),
  text: t.string(),
  meshReportId: t.string(),
  idempotencyKey: t.string().unique(),
  jobId: t.string(),
  grainProjectId: t.string(),
  preVersionId: t.string(),
  postVersionId: t.string(),
  status: t.string(),             // queued | running | succeeded | failed | needs_user | uncertain
  createdAt: t.timestamp(),
  updatedAt: t.timestamp(),
});

const designVersion = table({ name: 'design_version' }, {
  id: t.string().primaryKey(),
  sessionId: t.string().index('btree'),
  grainProjectId: t.string(),
  commandId: t.string(),
  phase: t.string(),              // 'baseline' | 'pre' | 'post'
  manifestAssetId: t.string(),
  fileCount: t.u32(),
  buildOk: t.option(t.bool()),
  createdAt: t.timestamp(),
});

const sessionEvent = table({ name: 'session_event' }, {
  seq: t.u64().primaryKey().autoInc(),
  sessionId: t.string().index('btree'),
  kind: t.string(),
  entityId: t.string(),
  at: t.timestamp(),
});

const spacetimedb = schema({
  admin, bridgeIdentity, repairSession, asset, qaPolicy, job, meshReport, finding, repairCheck,
  repairAttempt, voiceTurn, designCommand, designVersion, sessionEvent,
});
export default spacetimedb;
export { repairSession, asset, qaPolicy, job, meshReport, finding, repairCheck, repairAttempt, voiceTurn, designCommand, designVersion, sessionEvent };

