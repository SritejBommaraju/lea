// Lea state module entry: SpacetimeDB requires that this file export only the schema, reducers and views.
import { t } from 'spacetimedb/server';
import type { Identity } from 'spacetimedb';
import { reducerParams } from './shapes';
import spacetimedb, { repairSession, asset, qaPolicy, job, meshReport, finding, repairCheck, repairAttempt, voiceTurn, designCommand, designVersion, sessionEvent } from './schema';
import {
  onInit, authorizeBridgeImpl, revokeBridgeImpl, createSessionImpl, linkGrainProjectImpl, registerAssetImpl, pinReferenceMeshImpl,
  setQaPolicyImpl, enqueueJobImpl, claimJobImpl, reportProgressImpl, finishJobImpl, recordMeshReportImpl, recordFindingImpl,
  addRepairCheckImpl, recordCheckOutcomeImpl, recordRepairAttemptImpl, recordVoiceTurnImpl, submitDesignCommandImpl, recordDesignVersionImpl,
  trimEventsImpl,
} from './logic';

export default spacetimedb;
const P = reducerParams(t);

export const init = spacetimedb.init(onInit);
export const authorize_bridge = spacetimedb.reducer(P.authorize_bridge, authorizeBridgeImpl);
export const revoke_bridge = spacetimedb.reducer(P.revoke_bridge, revokeBridgeImpl);
export const create_session = spacetimedb.reducer(P.create_session, createSessionImpl);
export const link_grain_project = spacetimedb.reducer(P.link_grain_project, linkGrainProjectImpl);
export const register_asset = spacetimedb.reducer(P.register_asset, registerAssetImpl);
export const pin_reference_mesh = spacetimedb.reducer(P.pin_reference_mesh, pinReferenceMeshImpl);
export const set_qa_policy = spacetimedb.reducer(P.set_qa_policy, setQaPolicyImpl);
export const enqueue_job = spacetimedb.reducer(P.enqueue_job, enqueueJobImpl);
export const claim_job = spacetimedb.reducer(P.claim_job, claimJobImpl);
export const report_progress = spacetimedb.reducer(P.report_progress, reportProgressImpl);
export const finish_job = spacetimedb.reducer(P.finish_job, finishJobImpl);
export const record_mesh_report = spacetimedb.reducer(P.record_mesh_report, recordMeshReportImpl);
export const record_finding = spacetimedb.reducer(P.record_finding, recordFindingImpl);
export const add_repair_check = spacetimedb.reducer(P.add_repair_check, addRepairCheckImpl);
export const record_check_outcome = spacetimedb.reducer(P.record_check_outcome, recordCheckOutcomeImpl);
export const record_repair_attempt = spacetimedb.reducer(P.record_repair_attempt, recordRepairAttemptImpl);
export const record_voice_turn = spacetimedb.reducer(P.record_voice_turn, recordVoiceTurnImpl);
export const submit_design_command = spacetimedb.reducer(P.submit_design_command, submitDesignCommandImpl);
export const record_design_version = spacetimedb.reducer(P.record_design_version, recordDesignVersionImpl);
export const trim_events = spacetimedb.reducer(P.trim_events, trimEventsImpl);

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
