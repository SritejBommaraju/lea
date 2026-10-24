// Test-only StateStore: the real module reducers running in SpacetimeDB's portable datastore (wasm).
// It exercises the same transactions and constraints as the server; it is not the network path.
import { ConnectionId } from 'spacetimedb';
import { createModuleTestHarness, TestAuth } from 'spacetimedb/server/test-utils';
import * as logic from '../../module/src/logic';
import * as moduleExports from '../../module/src/index';
import { type EventRow, ReducerError, type ReducerArgs, type ReducerName, type StateStore, type Tables } from '../src/state';

const IMPL: Record<ReducerName, (ctx: any, a: any) => void> = {
  create_session: logic.createSessionImpl, link_grain_project: logic.linkGrainProjectImpl, register_asset: logic.registerAssetImpl,
  pin_reference_mesh: logic.pinReferenceMeshImpl, set_qa_policy: logic.setQaPolicyImpl, enqueue_job: logic.enqueueJobImpl,
  claim_job: logic.claimJobImpl, report_progress: logic.reportProgressImpl, finish_job: logic.finishJobImpl,
  record_mesh_report: logic.recordMeshReportImpl, record_finding: logic.recordFindingImpl, add_repair_check: logic.addRepairCheckImpl,
  record_check_outcome: logic.recordCheckOutcomeImpl, record_repair_attempt: logic.recordRepairAttemptImpl,
  record_voice_turn: logic.recordVoiceTurnImpl, submit_design_command: logic.submitDesignCommandImpl, record_design_version: logic.recordDesignVersionImpl,
};

export class HarnessStore implements StateStore {
  connected = true;
  readonly h = createModuleTestHarness(moduleExports.default, moduleExports);
  private readonly bridge = TestAuth.fromJwtPayload(JSON.stringify({ iss: 'lea-test', sub: 'bridge', aud: ['spacetimedb'], iat: 0 }), ConnectionId.random());
  private readonly listeners = new Set<(e: EventRow) => void>();
  private lastSeq = 0n;

  constructor() {
    const admin = TestAuth.internal();
    this.h.withReducerTx(admin, ctx => logic.onInit(ctx));
    const identity = this.h.withReducerTx(this.bridge, ctx => ctx.sender);
    this.h.withReducerTx(admin, ctx => logic.authorizeBridgeImpl(ctx, { identity, label: 'test' }));
  }

  async call<R extends ReducerName>(name: R, args: ReducerArgs[R]) {
    try {
      this.h.clock.set(new (this.h.clock.now().constructor as any)(BigInt(Date.now()) * 1000n));
      this.h.withReducerTx(this.bridge, ctx => IMPL[name](ctx, args));
    } catch (e) {
      throw new ReducerError((e as Error).message);
    }
    const fresh = [...this.h.db.sessionEvent.iter()].filter(e => e.seq > this.lastSeq).sort((a, b) => Number(a.seq - b.seq));
    for (const e of fresh) { this.lastSeq = e.seq; this.listeners.forEach(l => l(e)); }
  }

  read(): Tables {
    const d = this.h.db;
    return {
      sessions: [...d.repairSession.iter()], assets: [...d.asset.iter()], policies: [...d.qaPolicy.iter()], jobs: [...d.job.iter()],
      meshReports: [...d.meshReport.iter()], findings: [...d.finding.iter()], checks: [...d.repairCheck.iter()], repairs: [...d.repairAttempt.iter()],
      voiceTurns: [...d.voiceTurn.iter()], designCommands: [...d.designCommand.iter()], designVersions: [...d.designVersion.iter()], events: [...d.sessionEvent.iter()],
    } as Tables;
  }

  onEvent(l: (e: EventRow) => void) { this.listeners.add(l); return () => this.listeners.delete(l); }
}
