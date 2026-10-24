// Lea state module entry: SpacetimeDB requires that this file export only the schema, reducers and views.
import { t } from 'spacetimedb/server';
import type { Identity } from 'spacetimedb';
import spacetimedb, { repairSession, asset, qaPolicy, job, meshReport, finding, repairCheck, repairAttempt, voiceTurn, designCommand, designVersion, sessionEvent } from './schema';
import {
  onInit, authorizeBridgeImpl, revokeBridgeImpl, createSessionImpl, linkGrainProjectImpl, registerAssetImpl, pinReferenceMeshImpl,
  setQaPolicyImpl, enqueueJobImpl, claimJobImpl, reportProgressImpl, finishJobImpl, recordMeshReportImpl, recordFindingImpl,
  addRepairCheckImpl, recordCheckOutcomeImpl, recordRepairAttemptImpl, recordVoiceTurnImpl, submitDesignCommandImpl, recordDesignVersionImpl,
} from './logic';

export default spacetimedb;

export const init = spacetimedb.init(onInit);
export const authorize_bridge = spacetimedb.reducer({ identity: t.identity(), label: t.string() }, authorizeBridgeImpl);
export const revoke_bridge = spacetimedb.reducer({ identity: t.identity() }, revokeBridgeImpl);
export const create_session = spacetimedb.reducer({ id: t.string(), title: t.string() }, createSessionImpl);
export const link_grain_project = spacetimedb.reducer({ sessionId: t.string(), grainProjectId: t.string() }, linkGrainProjectImpl);
export const register_asset = spacetimedb.reducer({
  id: t.string(), sessionId: t.string(), kind: t.string(), sha256: t.string(), bytes: t.u64(), mediaType: t.string(),
  sourceAssetIds: t.array(t.string()), metaJson: t.string(),
}, registerAssetImpl);
export const pin_reference_mesh = spacetimedb.reducer({ sessionId: t.string(), assetId: t.string() }, pinReferenceMeshImpl);
export const set_qa_policy = spacetimedb.reducer({
  id: t.string(), sessionId: t.string(), scaleAnchorLabel: t.string(), scaleAnchorMm: t.f64(), holdoutLabel: t.string(),
  holdoutMm: t.f64(), maxHoldoutErrorMm: t.f64(), minCoverage: t.f64(), maxFitRmsMm: t.f64(), minFitInlierShare: t.f64(),
}, setQaPolicyImpl);
export const enqueue_job = spacetimedb.reducer({
  id: t.string(), sessionId: t.string(), kind: t.string(), idempotencyKey: t.string(), inputHash: t.string(), inputJson: t.string(),
}, enqueueJobImpl);
export const claim_job = spacetimedb.reducer({ jobId: t.string(), worker: t.string(), leaseSeconds: t.f64() }, claimJobImpl);
export const report_progress = spacetimedb.reducer({ jobId: t.string(), attempt: t.u32(), progress: t.string(), leaseSeconds: t.f64() }, reportProgressImpl);
export const finish_job = spacetimedb.reducer({ jobId: t.string(), attempt: t.u32(), status: t.string(), resultJson: t.string(), error: t.string() }, finishJobImpl);
export const record_mesh_report = spacetimedb.reducer({
  id: t.string(), jobId: t.string(), attempt: t.u32(), meshAssetId: t.string(), reportAssetId: t.string(), engine: t.string(),
  scaleFactor: t.f64(), measuredHoldoutMm: t.f64(), coverage: t.f64(), fitRmsMm: t.f64(), fitInlierShare: t.f64(),
  referenceMeshAssetId: t.string(),
}, recordMeshReportImpl);
export const record_finding = spacetimedb.reducer({
  id: t.string(), sessionId: t.string(), subject: t.string(), problem: t.string(), source: t.string(),
  evidenceAssetIds: t.array(t.string()), referenceAssetId: t.string(), meshReportId: t.string(), regionJson: t.string(),
  confidenceKind: t.string(), confidenceText: t.string(), detail: t.string(),
}, recordFindingImpl);
export const add_repair_check = spacetimedb.reducer({
  id: t.string(), sessionId: t.string(), findingId: t.string(), purpose: t.string(), instruction: t.string(), safety: t.string(),
  powerState: t.string(), expectedMin: t.option(t.f64()), expectedMax: t.option(t.f64()), expectedUnit: t.string(), expectedSource: t.string(),
}, addRepairCheckImpl);
export const record_check_outcome = spacetimedb.reducer({
  checkId: t.string(), observed: t.string(), observationAssetId: t.string(), readingValue: t.option(t.f64()), readingUnit: t.string(), note: t.string(),
}, recordCheckOutcomeImpl);
export const record_repair_attempt = spacetimedb.reducer({
  id: t.string(), sessionId: t.string(), findingId: t.string(), action: t.string(), verifyCheckId: t.string(),
}, recordRepairAttemptImpl);
export const record_voice_turn = spacetimedb.reducer({
  id: t.string(), sessionId: t.string(), mode: t.string(), transcript: t.string(), replyText: t.string(), audioAssetId: t.string(), commandId: t.string(),
}, recordVoiceTurnImpl);
export const submit_design_command = spacetimedb.reducer({
  id: t.string(), sessionId: t.string(), origin: t.string(), text: t.string(), meshReportId: t.string(), idempotencyKey: t.string(),
  jobId: t.string(), inputHash: t.string(),
}, submitDesignCommandImpl);
export const record_design_version = spacetimedb.reducer({
  id: t.string(), sessionId: t.string(), commandId: t.string(), phase: t.string(), manifestAssetId: t.string(), fileCount: t.u32(),
  buildOk: t.option(t.bool()), jobId: t.string(), attempt: t.u32(),
}, recordDesignVersionImpl);

// Bridge-only views: an unauthorized identity subscribes and receives nothing.
function bridgeOnly<R>(ctx: { sender: Identity; db: any }, rows: () => Iterable<R>): R[] {
  return ctx.db.bridgeIdentity.identity.find(ctx.sender) ? [...rows()] : [];
}
export const bridge_sessions = spacetimedb.view({ name: 'bridge_sessions', public: true }, t.array(repairSession.rowType), ctx => bridgeOnly(ctx, () => ctx.db.repairSession.iter()));
export const bridge_assets = spacetimedb.view({ name: 'bridge_assets', public: true }, t.array(asset.rowType), ctx => bridgeOnly(ctx, () => ctx.db.asset.iter()));
export const bridge_policies = spacetimedb.view({ name: 'bridge_policies', public: true }, t.array(qaPolicy.rowType), ctx => bridgeOnly(ctx, () => ctx.db.qaPolicy.iter()));
export const bridge_jobs = spacetimedb.view({ name: 'bridge_jobs', public: true }, t.array(job.rowType), ctx => bridgeOnly(ctx, () => ctx.db.job.iter()));
export const bridge_mesh_reports = spacetimedb.view({ name: 'bridge_mesh_reports', public: true }, t.array(meshReport.rowType), ctx => bridgeOnly(ctx, () => ctx.db.meshReport.iter()));
export const bridge_findings = spacetimedb.view({ name: 'bridge_findings', public: true }, t.array(finding.rowType), ctx => bridgeOnly(ctx, () => ctx.db.finding.iter()));
export const bridge_checks = spacetimedb.view({ name: 'bridge_checks', public: true }, t.array(repairCheck.rowType), ctx => bridgeOnly(ctx, () => ctx.db.repairCheck.iter()));
export const bridge_repairs = spacetimedb.view({ name: 'bridge_repairs', public: true }, t.array(repairAttempt.rowType), ctx => bridgeOnly(ctx, () => ctx.db.repairAttempt.iter()));
export const bridge_voice_turns = spacetimedb.view({ name: 'bridge_voice_turns', public: true }, t.array(voiceTurn.rowType), ctx => bridgeOnly(ctx, () => ctx.db.voiceTurn.iter()));
export const bridge_design_commands = spacetimedb.view({ name: 'bridge_design_commands', public: true }, t.array(designCommand.rowType), ctx => bridgeOnly(ctx, () => ctx.db.designCommand.iter()));
export const bridge_design_versions = spacetimedb.view({ name: 'bridge_design_versions', public: true }, t.array(designVersion.rowType), ctx => bridgeOnly(ctx, () => ctx.db.designVersion.iter()));
export const bridge_events = spacetimedb.view({ name: 'bridge_events', public: true }, t.array(sessionEvent.rowType), ctx => bridgeOnly(ctx, () => ctx.db.sessionEvent.iter()));
