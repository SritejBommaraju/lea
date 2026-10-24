// Runs the module's reducers inside SpacetimeDB's portable datastore (the real engine compiled to
// wasm), so transactions, unique indexes and rollbacks behave as they do on the server.
import { beforeEach, describe, expect, it } from 'vitest';
import { ConnectionId, Identity } from 'spacetimedb';
import { createModuleTestHarness, TestAuth, type ModuleTestHarness } from 'spacetimedb/server/test-utils';
import type { InferSchema } from 'spacetimedb/server';
import * as logic from '../src/logic';
import * as exportsOfModule from '../src/index';

const mod = logic;

const spacetimedb = exportsOfModule.default;
type Harness = ModuleTestHarness<InferSchema<typeof exportsOfModule.default>>;

const hex = (c: string) => c.repeat(64);
const jwt = (sub: string) => TestAuth.fromJwtPayload(JSON.stringify({ iss: 'lea-test', sub, aud: ['spacetimedb'], iat: 0 }), ConnectionId.random());
const ADMIN = TestAuth.internal();
const BRIDGE = jwt('bridge');
const STRANGER = jwt('stranger');

let h: Harness;
const call = <A>(auth: TestAuth, fn: (ctx: any, a: A) => void, a: A) => h.withReducerTx(auth, ctx => fn(ctx, a));
const senderOf = (auth: TestAuth): Identity => h.withReducerTx(auth, ctx => ctx.sender);

function asset(id: string, kind: string, digit = 'a', sessionId = 's1', sourceAssetIds: string[] = []) {
  call(BRIDGE, mod.registerAssetImpl, { id, sessionId, kind, sha256: hex(digit), bytes: 10n, mediaType: 'image/jpeg', sourceAssetIds, metaJson: '{}' });
}

const POLICY = {
  id: 'p1', sessionId: 's1', scaleAnchorLabel: 'base width', scaleAnchorMm: 80, holdoutLabel: 'post height', holdoutMm: 42,
  maxHoldoutErrorMm: 1.0, minCoverage: 0.85, maxFitRmsMm: 1.5, minFitInlierShare: 0.8,
};

function reconstructionReady() {
  call(BRIDGE, mod.setQaPolicyImpl, POLICY);
  const photos = Array.from({ length: 20 }, (_, i) => `ph${i}`);
  photos.forEach((id, i) => asset(id, 'photo_orbit', (i % 10).toString()));
  asset('ref', 'reference_mesh', 'b');
  call(BRIDGE, mod.pinReferenceMeshImpl, { sessionId: 's1', assetId: 'ref' });
  call(BRIDGE, mod.enqueueJobImpl, {
    id: 'j1', sessionId: 's1', kind: 'reconstruction', idempotencyKey: 'recon-s1-1', inputHash: hex('c'),
    inputJson: JSON.stringify({ photoAssetIds: photos, policyId: 'p1' }),
  });
  call(BRIDGE, mod.claimJobImpl, { jobId: 'j1', worker: 'w1', leaseSeconds: 60 });
  asset('mesh', 'recon_mesh', 'd', 's1', photos);
  asset('qa', 'qa_report', 'e');
}

const report = (over: Partial<Parameters<typeof mod.recordMeshReportImpl>[1]> = {}) => ({
  id: 'r1', jobId: 'j1', attempt: 1, meshAssetId: 'mesh', reportAssetId: 'qa', engine: 'test', scaleFactor: 1.0,
  measuredHoldoutMm: 42.4, coverage: 0.9, fitRmsMm: 0.8, fitInlierShare: 0.95, referenceMeshAssetId: 'ref', ...over,
});

beforeEach(() => {
  h = createModuleTestHarness(spacetimedb, exportsOfModule);
  call(ADMIN, mod.onInit, {});
  call(ADMIN, mod.authorizeBridgeImpl, { identity: senderOf(BRIDGE), label: 'bridge' });
  call(BRIDGE, mod.createSessionImpl, { id: 's1', title: 'Fan bracket' });
});

describe('identity', () => {
  it('rejects reducer calls from identities that are not authorized bridges', () => {
    expect(() => call(STRANGER, mod.createSessionImpl, { id: 's2', title: 'x' })).toThrow(/not an authorized bridge/);
    expect(() => call(STRANGER, mod.authorizeBridgeImpl, { identity: senderOf(STRANGER), label: 'me' })).toThrow(/only an admin/);
  });

  it('views return rows only to bridges', () => {
    expect(h.withReducerTx(BRIDGE, ctx => [...ctx.db.repairSession.iter()].length)).toBe(1);
    const viewFor = (auth: TestAuth) => {
      const ctx = h.viewContext(auth) as any;
      return ctx.db.bridgeIdentity.identity.find(ctx.sender) ? [...ctx.db.repairSession.iter()] : [];
    };
    expect(viewFor(BRIDGE)).toHaveLength(1);
    expect(viewFor(STRANGER)).toHaveLength(0);
  });
});

describe('assets', () => {
  it('validates digests and refuses to rebind an id to other content', () => {
    expect(() => call(BRIDGE, mod.registerAssetImpl, { id: 'x', sessionId: 's1', kind: 'photo_orbit', sha256: 'abc', bytes: 1n, mediaType: 'image/jpeg', sourceAssetIds: [], metaJson: '{}' })).toThrow(/sha256/);
    asset('a1', 'photo_orbit', 'a');
    asset('a1', 'photo_orbit', 'a');  // same content: idempotent
    expect(() => asset('a1', 'photo_orbit', 'f')).toThrow(/different content/);
    expect(h.db.asset.id.find('a1')!.storageKey).toBe(`sha256/${hex('a')}`);
  });

  it('keeps a session pinned to its first reference', () => {
    asset('ref', 'reference_mesh', 'b');
    asset('ref2', 'reference_mesh', 'c');
    call(BRIDGE, mod.pinReferenceMeshImpl, { sessionId: 's1', assetId: 'ref' });
    expect(() => call(BRIDGE, mod.pinReferenceMeshImpl, { sessionId: 's1', assetId: 'ref2' })).toThrow(/pinned to another reference/);
  });
});

describe('jobs', () => {
  it('needs a frozen policy with a holdout different from the scale anchor', () => {
    expect(() => call(BRIDGE, mod.setQaPolicyImpl, { ...POLICY, holdoutLabel: 'Base Width' })).toThrow(/different dimension/);
    call(BRIDGE, mod.setQaPolicyImpl, POLICY);
    expect(() => call(BRIDGE, mod.setQaPolicyImpl, POLICY)).toThrow(/frozen/);
  });

  it('refuses reconstruction with too few orbit photos', () => {
    call(BRIDGE, mod.setQaPolicyImpl, POLICY);
    asset('ph0', 'photo_orbit');
    expect(() => call(BRIDGE, mod.enqueueJobImpl, {
      id: 'j1', sessionId: 's1', kind: 'reconstruction', idempotencyKey: 'recon-s1-1', inputHash: hex('c'),
      inputJson: JSON.stringify({ photoAssetIds: ['ph0'], policyId: 'p1' }),
    })).toThrow(/at least 20/);
  });

  it('grants one lease, rejects stale attempts and changed idempotent payloads', () => {
    reconstructionReady();
    expect(() => call(BRIDGE, mod.claimJobImpl, { jobId: 'j1', worker: 'w2', leaseSeconds: 60 })).toThrow(/is running/);
    expect(() => call(BRIDGE, mod.finishJobImpl, { jobId: 'j1', attempt: 2, status: 'failed', resultJson: '', error: 'x' })).toThrow(/stale attempt/);
    expect(() => call(BRIDGE, mod.enqueueJobImpl, {
      id: 'j9', sessionId: 's1', kind: 'reconstruction', idempotencyKey: 'recon-s1-1', inputHash: hex('9'), inputJson: '{}',
    })).toThrow(/different request/);
    expect(() => call(BRIDGE, mod.finishJobImpl, { jobId: 'j1', attempt: 1, status: 'succeeded', resultJson: '', error: '' })).toThrow(/own result reducer/);
  });

  it('lets a new worker reclaim an expired lease and fences the old one', () => {
    reconstructionReady();
    h.clock.set(new (h.clock.now().constructor as any)(h.clock.now().microsSinceUnixEpoch + 61_000_000n));
    call(BRIDGE, mod.claimJobImpl, { jobId: 'j1', worker: 'w2', leaseSeconds: 60 });
    expect(h.db.job.id.find('j1')!.attempt).toBe(2);
    expect(() => call(BRIDGE, mod.recordMeshReportImpl, report())).toThrow(/stale attempt 1/);
  });
});

describe('mesh gate', () => {
  it('computes a failing verdict itself and blocks geometry findings and automatic CAD', () => {
    reconstructionReady();
    call(BRIDGE, mod.recordMeshReportImpl, report({ measuredHoldoutMm: 44.0, coverage: 0.6 }));
    const r = h.db.meshReport.id.find('r1')!;
    expect(r.verdict).toBe('fail');
    expect(r.failedChecks).toEqual(['holdout', 'coverage']);
    expect(h.db.job.id.find('j1')!.status).toBe('needs_user');
    expect(() => call(BRIDGE, mod.recordFindingImpl, {
      id: 'f1', sessionId: 's1', subject: 'post', problem: 'snapped', source: 'geometry', evidenceAssetIds: ['mesh'],
      referenceAssetId: 'ref', meshReportId: 'r1', regionJson: '{}', confidenceKind: 'measured', confidenceText: '3.1 mm', detail: '',
    })).toThrow(/cannot drive diagnosis or CAD/);
    call(BRIDGE, mod.linkGrainProjectImpl, { sessionId: 's1', grainProjectId: 'abcdef012345' });
    expect(() => call(BRIDGE, mod.submitDesignCommandImpl, {
      id: 'c1', sessionId: 's1', origin: 'auto', text: 'bracket', meshReportId: 'r1', idempotencyKey: 'auto-r1', jobId: 'dj1', inputHash: hex('1'),
    })).toThrow(/cannot drive diagnosis or CAD/);
  });

  it('fails scale and holdout that QA could not trust (sent as NaN)', () => {
    reconstructionReady();
    call(BRIDGE, mod.recordMeshReportImpl, report({ scaleFactor: Number.NaN, measuredHoldoutMm: Number.NaN }));
    expect(h.db.meshReport.id.find('r1')!.failedChecks).toEqual(['scale', 'holdout']);
  });

  it('fails a scan that is not fitted against the pinned reference', () => {
    reconstructionReady();
    call(BRIDGE, mod.recordMeshReportImpl, report({ referenceMeshAssetId: '' }));
    expect(h.db.meshReport.id.find('r1')!.failedChecks).toEqual(['reference_missing']);
  });

  it('passing scan allows exactly one automatic design turn', () => {
    reconstructionReady();
    call(BRIDGE, mod.recordMeshReportImpl, report());
    expect(h.db.meshReport.id.find('r1')!.verdict).toBe('pass');
    expect(h.db.job.id.find('j1')!.status).toBe('succeeded');
    call(BRIDGE, mod.linkGrainProjectImpl, { sessionId: 's1', grainProjectId: 'abcdef012345' });
    const cmd = { id: 'c1', sessionId: 's1', origin: 'auto', text: 'bracket', meshReportId: 'r1', idempotencyKey: 'auto-r1', jobId: 'dj1', inputHash: hex('1') };
    call(BRIDGE, mod.submitDesignCommandImpl, cmd);
    call(BRIDGE, mod.submitDesignCommandImpl, cmd);  // retry after a timeout: same row
    expect([...h.db.designCommand.iter()]).toHaveLength(1);
    expect(() => call(BRIDGE, mod.submitDesignCommandImpl, { ...cmd, id: 'c2', idempotencyKey: 'auto-r1-again', jobId: 'dj2' })).toThrow(/already produced/);
  });
});

describe('repair evidence', () => {
  function findingReady() {
    asset('close', 'photo_closeup', 'a');
    asset('refphoto', 'reference_photo', 'b');
    call(BRIDGE, mod.recordFindingImpl, {
      id: 'f1', sessionId: 's1', subject: 'fan connector', problem: 'unseated', source: 'vision', evidenceAssetIds: ['close'],
      referenceAssetId: 'refphoto', meshReportId: '', regionJson: '{}', confidenceKind: 'model_word', confidenceText: 'medium', detail: '',
    });
  }
  const check = (over: object = {}) => ({
    id: 'k1', sessionId: 's1', findingId: 'f1', purpose: 'diagnose', instruction: 'Measure fan supply', safety: 'Battery only, 5 V max',
    powerState: 'on_low_voltage', expectedMin: 4.75, expectedMax: 5.25, expectedUnit: 'V', expectedSource: 'fan datasheet', ...over,
  });

  it('requires a saved reference and evidence for every finding', () => {
    asset('close', 'photo_closeup', 'a');
    expect(() => call(BRIDGE, mod.recordFindingImpl, {
      id: 'f1', sessionId: 's1', subject: 's', problem: 'p', source: 'vision', evidenceAssetIds: ['close'], referenceAssetId: '',
      meshReportId: '', regionJson: '{}', confidenceKind: 'none', confidenceText: '', detail: '',
    })).toThrow(/saved reference/);
  });

  it('judges a reading against the sourced range in code, overriding the tap', () => {
    findingReady();
    call(BRIDGE, mod.addRepairCheckImpl, check());
    call(BRIDGE, mod.recordCheckOutcomeImpl, { checkId: 'k1', observed: 'as_expected', observationAssetId: '', readingValue: 0.2, readingUnit: 'V', note: '' });
    expect(h.db.repairCheck.id.find('k1')!.outcome).toBe('not_as_expected');
    expect(h.db.finding.id.find('f1')!.state).toBe('confirmed');
  });

  it('keeps a check without evidence unverified', () => {
    findingReady();
    call(BRIDGE, mod.addRepairCheckImpl, check({ expectedMin: undefined, expectedMax: undefined, expectedUnit: '', expectedSource: '' }));
    call(BRIDGE, mod.recordCheckOutcomeImpl, { checkId: 'k1', observed: 'not_as_expected', observationAssetId: '', readingValue: undefined, readingUnit: '', note: 'looked loose' });
    expect(h.db.repairCheck.id.find('k1')!.outcome).toBe('unverified');
    expect(h.db.finding.id.find('f1')!.state).toBe('unverified');
  });

  it('refuses continuity checks with power on', () => {
    findingReady();
    expect(() => call(BRIDGE, mod.addRepairCheckImpl, check({ expectedUnit: 'ohm', expectedMin: 0, expectedMax: 1 }))).toThrow(/power off/);
  });

  it('records whether the repair worked from a verification reading', () => {
    findingReady();
    call(BRIDGE, mod.addRepairCheckImpl, check({ id: 'v1', purpose: 'verify_repair' }));
    call(BRIDGE, mod.recordRepairAttemptImpl, { id: 'rep1', sessionId: 's1', findingId: 'f1', action: 'Reseat connector', verifyCheckId: 'v1' });
    call(BRIDGE, mod.recordCheckOutcomeImpl, { checkId: 'v1', observed: 'as_expected', observationAssetId: '', readingValue: 5.02, readingUnit: 'V', note: '' });
    expect(h.db.repairAttempt.id.find('rep1')!.result).toBe('fixed_verified');
  });
});

describe('design versions', () => {
  it('saves pre and post versions and never activates a failed build', () => {
    call(BRIDGE, mod.linkGrainProjectImpl, { sessionId: 's1', grainProjectId: 'abcdef012345' });
    asset('m0', 'design_snapshot', '0');
    call(BRIDGE, mod.recordDesignVersionImpl, { id: 'v0', sessionId: 's1', commandId: '', phase: 'baseline', manifestAssetId: 'm0', fileCount: 3, buildOk: true, jobId: '', attempt: 0 });
    expect(h.db.repairSession.id.find('s1')!.activeDesignVersionId).toBe('v0');

    call(BRIDGE, mod.submitDesignCommandImpl, { id: 'c1', sessionId: 's1', origin: 'voice', text: 'widen slot by 2 mm', meshReportId: '', idempotencyKey: 'voice-t1', jobId: 'dj1', inputHash: hex('1') });
    call(BRIDGE, mod.claimJobImpl, { jobId: 'dj1', worker: 'grain', leaseSeconds: 600 });
    asset('m1', 'design_snapshot', '1');
    asset('m2', 'design_snapshot', '2');
    expect(() => call(BRIDGE, mod.recordDesignVersionImpl, { id: 'v2', sessionId: 's1', commandId: 'c1', phase: 'post', manifestAssetId: 'm2', fileCount: 3, buildOk: false, jobId: 'dj1', attempt: 1 })).toThrow(/pre-change version/);
    call(BRIDGE, mod.recordDesignVersionImpl, { id: 'v1', sessionId: 's1', commandId: 'c1', phase: 'pre', manifestAssetId: 'm1', fileCount: 3, buildOk: true, jobId: 'dj1', attempt: 1 });
    call(BRIDGE, mod.recordDesignVersionImpl, { id: 'v2', sessionId: 's1', commandId: 'c1', phase: 'post', manifestAssetId: 'm2', fileCount: 3, buildOk: false, jobId: 'dj1', attempt: 1 });
    expect(h.db.designCommand.id.find('c1')!.status).toBe('failed');
    expect(h.db.job.id.find('dj1')!.status).toBe('failed');
    expect(h.db.repairSession.id.find('s1')!.activeDesignVersionId).toBe('v0');
  });

  it('marks a turn that ended in a Grain question as needs_user, not failed', () => {
    call(BRIDGE, mod.linkGrainProjectImpl, { sessionId: 's1', grainProjectId: 'abcdef012345' });
    call(BRIDGE, mod.submitDesignCommandImpl, { id: 'c1', sessionId: 's1', origin: 'text', text: 'make a bracket', meshReportId: '', idempotencyKey: 'text-t1', jobId: 'dj1', inputHash: hex('1') });
    call(BRIDGE, mod.claimJobImpl, { jobId: 'dj1', worker: 'grain', leaseSeconds: 600 });
    asset('m1', 'design_snapshot', '1');
    asset('m2', 'design_snapshot', '2');
    call(BRIDGE, mod.recordDesignVersionImpl, { id: 'v1', sessionId: 's1', commandId: 'c1', phase: 'pre', manifestAssetId: 'm1', fileCount: 0, buildOk: undefined, jobId: 'dj1', attempt: 1 });
    call(BRIDGE, mod.recordDesignVersionImpl, { id: 'v2', sessionId: 's1', commandId: 'c1', phase: 'post', manifestAssetId: 'm2', fileCount: 0, buildOk: undefined, jobId: 'dj1', attempt: 1 });
    expect(h.db.designCommand.id.find('c1')!.status).toBe('needs_user');
  });

  it('records only final voice turns', () => {
    expect(() => call(BRIDGE, mod.recordVoiceTurnImpl, { id: 't1', sessionId: 's1', mode: 'wake', transcript: '  ', replyText: '', audioAssetId: '', commandId: '' })).toThrow(/final/);
  });
});
