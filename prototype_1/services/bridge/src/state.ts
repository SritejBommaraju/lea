// The bridge's view of SpacetimeDB. Reducer names and argument types come straight from the module's
// own logic, so a renamed field fails the TypeScript build here instead of at runtime.
import type * as logic from '../../module/src/logic';

type Args<F> = F extends (ctx: any, a: infer A) => void ? A : never;

export interface ReducerArgs {
  create_session: Args<typeof logic.createSessionImpl>;
  link_grain_project: Args<typeof logic.linkGrainProjectImpl>;
  register_asset: Args<typeof logic.registerAssetImpl>;
  pin_reference_mesh: Args<typeof logic.pinReferenceMeshImpl>;
  set_qa_policy: Args<typeof logic.setQaPolicyImpl>;
  enqueue_job: Args<typeof logic.enqueueJobImpl>;
  claim_job: Args<typeof logic.claimJobImpl>;
  report_progress: Args<typeof logic.reportProgressImpl>;
  finish_job: Args<typeof logic.finishJobImpl>;
  record_mesh_report: Args<typeof logic.recordMeshReportImpl>;
  record_finding: Args<typeof logic.recordFindingImpl>;
  add_repair_check: Args<typeof logic.addRepairCheckImpl>;
  record_check_outcome: Args<typeof logic.recordCheckOutcomeImpl>;
  record_repair_attempt: Args<typeof logic.recordRepairAttemptImpl>;
  record_voice_turn: Args<typeof logic.recordVoiceTurnImpl>;
  submit_design_command: Args<typeof logic.submitDesignCommandImpl>;
  record_design_version: Args<typeof logic.recordDesignVersionImpl>;
}
export type ReducerName = keyof ReducerArgs;

// Row shapes as the module defines them (timestamps are SpacetimeDB Timestamps).
type Ts = { microsSinceUnixEpoch: bigint };
export interface SessionRow { id: string; title: string; grainProjectId: string; referenceMeshAssetId: string; activeDesignVersionId: string; createdAt: Ts; updatedAt: Ts }
export interface AssetRow { id: string; sessionId: string; kind: string; sha256: string; bytes: bigint; mediaType: string; storageKey: string; sourceAssetIds: string[]; metaJson: string; createdAt: Ts }
export interface PolicyRow { id: string; sessionId: string; scaleAnchorLabel: string; scaleAnchorMm: number; holdoutLabel: string; holdoutMm: number; maxHoldoutErrorMm: number; minCoverage: number; maxFitRmsMm: number; minFitInlierShare: number; createdAt: Ts }
export interface JobRow { id: string; sessionId: string; kind: string; status: string; idempotencyKey: string; inputHash: string; inputJson: string; attempt: number; leaseOwner: string; leaseExpiresAt: Ts; progress: string; resultJson: string; error: string; createdAt: Ts; updatedAt: Ts }
export interface MeshReportRow { id: string; sessionId: string; jobId: string; policyId: string; meshAssetId: string; reportAssetId: string; referenceMeshAssetId: string; engine: string; scaleFactor: number; measuredHoldoutMm: number; holdoutErrorMm: number; coverage: number; fitRmsMm: number; fitInlierShare: number; verdict: string; failedChecks: string[]; createdAt: Ts }
export interface FindingRow { id: string; sessionId: string; subject: string; problem: string; source: string; state: string; evidenceAssetIds: string[]; referenceAssetId: string; meshReportId: string; regionJson: string; confidenceKind: string; confidenceText: string; detail: string; createdAt: Ts; updatedAt: Ts }
export interface CheckRow { id: string; sessionId: string; findingId: string; purpose: string; instruction: string; safety: string; powerState: string; expectedMin?: number; expectedMax?: number; expectedUnit: string; expectedSource: string; outcome: string; observationAssetId: string; readingValue?: number; readingUnit: string; note: string; createdAt: Ts; updatedAt: Ts }
export interface RepairRow { id: string; sessionId: string; findingId: string; action: string; verifyCheckId: string; result: string; createdAt: Ts; updatedAt: Ts }
export interface VoiceTurnRow { id: string; sessionId: string; mode: string; transcript: string; replyText: string; audioAssetId: string; commandId: string; createdAt: Ts }
export interface DesignCommandRow { id: string; sessionId: string; origin: string; text: string; meshReportId: string; idempotencyKey: string; jobId: string; grainProjectId: string; preVersionId: string; postVersionId: string; status: string; createdAt: Ts; updatedAt: Ts }
export interface DesignVersionRow { id: string; sessionId: string; grainProjectId: string; commandId: string; phase: string; manifestAssetId: string; fileCount: number; buildOk?: boolean; createdAt: Ts }
export interface EventRow { seq: bigint; sessionId: string; kind: string; entityId: string; at: Ts }

export interface Tables {
  sessions: SessionRow[]; assets: AssetRow[]; policies: PolicyRow[]; jobs: JobRow[]; meshReports: MeshReportRow[];
  findings: FindingRow[]; checks: CheckRow[]; repairs: RepairRow[]; voiceTurns: VoiceTurnRow[];
  designCommands: DesignCommandRow[]; designVersions: DesignVersionRow[]; events: EventRow[];
}

export interface StateStore {
  /** Resolves once SpacetimeDB has committed the reducer; rejects with the reducer's message. */
  call<R extends ReducerName>(name: R, args: ReducerArgs[R]): Promise<void>;
  /** The bridge's current, subscription-backed copy of every table it may read. */
  read(): Tables;
  onEvent(listener: (e: EventRow) => void): () => void;
  readonly connected: boolean;
}

export class ReducerError extends Error {}

export function sessionSnapshot(t: Tables, sessionId: string) {
  const of = <R extends { sessionId: string }>(rows: R[]) => rows.filter(r => r.sessionId === sessionId);
  const session = t.sessions.find(s => s.id === sessionId);
  if (!session) return null;
  const events = of(t.events);
  return {
    session, assets: of(t.assets), policies: of(t.policies), jobs: of(t.jobs), meshReports: of(t.meshReports),
    findings: of(t.findings), checks: of(t.checks), repairs: of(t.repairs), voiceTurns: of(t.voiceTurns),
    designCommands: of(t.designCommands), designVersions: of(t.designVersions),
    lastSeq: events.reduce((m, e) => (e.seq > m ? e.seq : m), 0n),
  };
}

/** JSON for the phone: bigint -> number (all values here are far below 2^53), Timestamp -> ISO string. */
export function toWire(value: unknown): unknown {
  if (typeof value === 'bigint') return Number(value);
  if (Array.isArray(value)) return value.map(toWire);
  if (value && typeof value === 'object') {
    const micros = (value as Ts).microsSinceUnixEpoch;
    if (typeof micros === 'bigint' && Object.keys(value).length <= 1) return new Date(Number(micros / 1000n)).toISOString();
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) out[k] = v === undefined ? null : toWire(v);
    return out;
  }
  return value;
}
