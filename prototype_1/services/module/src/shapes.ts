// Row and reducer-argument shapes, shared by the module (server) and the bridge (client). They take `t` as an
// argument so each side builds them with its own copy of the SDK; the bridge's client schema then matches the
// module by construction, with no generated bindings.
import type { t as T } from 'spacetimedb';

export const TABLE_NAMES = {
  admin: 'admin',
  bridgeIdentity: 'bridge_identity',
  repairSession: 'repair_session',
  asset: 'asset',
  qaPolicy: 'qa_policy',
  job: 'job',
  meshReport: 'mesh_report',
  finding: 'finding',
  repairCheck: 'repair_check',
  repairAttempt: 'repair_attempt',
  voiceTurn: 'voice_turn',
  designCommand: 'design_command',
  designVersion: 'design_version',
  sessionEvent: 'session_event',
} as const;

export const rowShapes = (t: typeof T) => ({
  admin: { identity: t.identity().primaryKey() },
  bridgeIdentity: {
    identity: t.identity().primaryKey(),
    label: t.string(),
  },
  repairSession: {
    id: t.string().primaryKey(),
    title: t.string(),
    grainProjectId: t.string(),          // '' until linked
    referenceMeshAssetId: t.string(),    // the saved reference every scan in this session is pinned to
    activeDesignVersionId: t.string(),   // last successful post-build snapshot
    createdAt: t.timestamp(),
    updatedAt: t.timestamp(),
  },
  asset: {
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
  },
  // Owner-frozen acceptance limits (handoff O9). No defaults exist in code.
  qaPolicy: {
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
  },
  job: {
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
  },
  meshReport: {
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
  },
  finding: {
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
  },
  repairCheck: {
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
    net: t.string(),               // the circuit a reading is on, when known ('' otherwise)
    crossCheck: t.string(),         // Grain's own judgement of the reading, kept beside ours, never instead of it
    createdAt: t.timestamp(),
    updatedAt: t.timestamp(),
  },
  repairAttempt: {
    id: t.string().primaryKey(),
    sessionId: t.string().index('btree'),
    findingId: t.string(),
    action: t.string(),
    verifyCheckId: t.string(),
    result: t.string(),             // 'pending' | 'fixed_verified' | 'not_fixed' | 'unverified'
    createdAt: t.timestamp(),
    updatedAt: t.timestamp(),
  },
  voiceTurn: {
    id: t.string().primaryKey(),
    sessionId: t.string().index('btree'),
    mode: t.string(),
    transcript: t.string(),
    replyText: t.string(),
    audioAssetId: t.string(),
    commandId: t.string(),
    createdAt: t.timestamp(),
  },
  designCommand: {
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
  },
  designVersion: {
    id: t.string().primaryKey(),
    sessionId: t.string().index('btree'),
    grainProjectId: t.string(),
    commandId: t.string(),
    phase: t.string(),              // 'baseline' | 'pre' | 'post'
    manifestAssetId: t.string(),
    fileCount: t.u32(),
    buildOk: t.option(t.bool()),
    createdAt: t.timestamp(),
  },
  sessionEvent: {
    seq: t.u64().primaryKey().autoInc(),
    sessionId: t.string().index('btree'),
    kind: t.string(),
    entityId: t.string(),
    at: t.timestamp(),
  },
});

export const reducerParams = (t: typeof T) => ({
  authorize_bridge: { identity: t.identity(), label: t.string() },
  revoke_bridge: { identity: t.identity() },
  create_session: { id: t.string(), title: t.string() },
  link_grain_project: { sessionId: t.string(), grainProjectId: t.string() },
  register_asset: {
    id: t.string(), sessionId: t.string(), kind: t.string(), sha256: t.string(), bytes: t.u64(), mediaType: t.string(),
    sourceAssetIds: t.array(t.string()), metaJson: t.string(),
  },
  pin_reference_mesh: { sessionId: t.string(), assetId: t.string() },
  set_qa_policy: {
    id: t.string(), sessionId: t.string(), scaleAnchorLabel: t.string(), scaleAnchorMm: t.f64(), holdoutLabel: t.string(),
    holdoutMm: t.f64(), maxHoldoutErrorMm: t.f64(), minCoverage: t.f64(), maxFitRmsMm: t.f64(), minFitInlierShare: t.f64(),
  },
  enqueue_job: {
    id: t.string(), sessionId: t.string(), kind: t.string(), idempotencyKey: t.string(), inputHash: t.string(), inputJson: t.string(),
  },
  claim_job: { jobId: t.string(), worker: t.string(), leaseSeconds: t.f64() },
  report_progress: { jobId: t.string(), attempt: t.u32(), progress: t.string(), leaseSeconds: t.f64() },
  finish_job: { jobId: t.string(), attempt: t.u32(), status: t.string(), resultJson: t.string(), error: t.string() },
  record_mesh_report: {
    id: t.string(), jobId: t.string(), attempt: t.u32(), meshAssetId: t.string(), reportAssetId: t.string(), engine: t.string(),
    scaleFactor: t.f64(), measuredHoldoutMm: t.f64(), coverage: t.f64(), fitRmsMm: t.f64(), fitInlierShare: t.f64(),
    referenceMeshAssetId: t.string(),
  },
  record_finding: {
    id: t.string(), sessionId: t.string(), subject: t.string(), problem: t.string(), source: t.string(),
    evidenceAssetIds: t.array(t.string()), referenceAssetId: t.string(), meshReportId: t.string(), regionJson: t.string(),
    confidenceKind: t.string(), confidenceText: t.string(), detail: t.string(),
  },
  add_repair_check: {
    id: t.string(), sessionId: t.string(), findingId: t.string(), purpose: t.string(), instruction: t.string(), safety: t.string(),
    powerState: t.string(), expectedMin: t.option(t.f64()), expectedMax: t.option(t.f64()), expectedUnit: t.string(), expectedSource: t.string(), net: t.string(),
  },
  record_check_outcome: {
    checkId: t.string(), observed: t.string(), observationAssetId: t.string(), readingValue: t.option(t.f64()), readingUnit: t.string(), note: t.string(), crossCheck: t.string(),
  },
  record_repair_attempt: {
    id: t.string(), sessionId: t.string(), findingId: t.string(), action: t.string(), verifyCheckId: t.string(),
  },
  record_voice_turn: {
    id: t.string(), sessionId: t.string(), mode: t.string(), transcript: t.string(), replyText: t.string(), audioAssetId: t.string(), commandId: t.string(),
  },
  submit_design_command: {
    id: t.string(), sessionId: t.string(), origin: t.string(), text: t.string(), meshReportId: t.string(), idempotencyKey: t.string(),
    jobId: t.string(), inputHash: t.string(), checksJson: t.string(),
  },
  record_design_version: {
    id: t.string(), sessionId: t.string(), commandId: t.string(), phase: t.string(), manifestAssetId: t.string(), fileCount: t.u32(),
    buildOk: t.option(t.bool()), jobId: t.string(), attempt: t.u32(),
  },
  trim_events: { before: t.timestamp() },
});
