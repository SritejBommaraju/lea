// Bridge tests over real HTTP, with the module's real reducers (wasm datastore), the real local Grain
// server (GRAIN_URL), and real Open3D QA. Object Capture is replaced by a SYNTHETIC mesh because no
// real photo set exists on this machine; that substitution is the only stand-in.
import { execFileSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { cpSync, mkdirSync, mkdtempSync, readFileSync } from 'node:fs';
import type { AddressInfo } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { AssetStore } from '../src/assets';
import { DeviceAuth } from '../src/auth';
import { config } from '../src/config';
import { GrainClient } from '../src/grain';
import { createApi } from '../src/http';
import { Lease } from '../src/jobs/context';
import { runDesignTurn } from '../src/jobs/design';
import { runReconstruction } from '../src/jobs/reconstruction';
import { routeTranscript } from '../src/voice';
import { HarnessStore } from './harness-store';

const tmp = mkdtempSync(path.join(os.tmpdir(), 'lea-bridge-test-'));
const store = new HarnessStore();
const assets = new AssetStore(path.join(tmp, 'assets'));
const auth = new DeviceAuth(tmp);
const token = auth.pair('test phone');
const grain = new GrainClient(config.grainUrl, config.grainKey);
const server = createApi({ store, assets, auth, grain });
let base = '';
const sid = `se_${randomUUID().slice(0, 8)}`;

const api = async (method: string, p: string, body?: unknown, headers: Record<string, string> = {}) => {
  const res = await fetch(`${base}${p}`, {
    method, headers: { Authorization: `Bearer ${token}`, ...(body !== undefined && !(body instanceof Buffer) ? { 'Content-Type': 'application/json' } : {}), ...headers },
    body: body === undefined ? undefined : body instanceof Buffer ? body : JSON.stringify(body),
  });
  return { status: res.status, json: await res.json().catch(() => null) as any };
};
const sha = (b: Buffer) => createHash('sha256').update(b).digest('hex');
const upload = (id: string, kind: string, data: Buffer, type = 'image/jpeg', meta: object = {}) =>
  api('PUT', `/v1/sessions/${sid}/assets/${id}`, data, { 'Content-Type': type, 'x-lea-kind': kind, 'x-lea-sha256': sha(data), 'x-lea-meta': Buffer.from(JSON.stringify(meta)).toString('base64') });

beforeAll(async () => {
  await new Promise<void>(r => server.listen(0, '127.0.0.1', () => r()));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  expect((await api('POST', '/v1/sessions', { id: sid, title: 'Bracket repair' })).status).toBe(200);
});
afterAll(() => server.close());

describe('auth and evidence storage', () => {
  it('refuses unpaired devices', async () => {
    const res = await fetch(`${base}/v1/sessions`);
    expect(res.status).toBe(401);
  });

  it('rejects a body whose hash does not match, keeps originals byte-for-byte, and is retry-safe', async () => {
    const photo = Buffer.from(`original ${randomUUID()}`);
    const bad = await api('PUT', `/v1/sessions/${sid}/assets/as_bad`, photo, { 'Content-Type': 'image/jpeg', 'x-lea-kind': 'photo_closeup', 'x-lea-sha256': sha(Buffer.from('other')) });
    expect(bad.status).toBe(422);
    expect(store.read().assets.find(a => a.id === 'as_bad')).toBeUndefined();
    expect((await upload('as_close', 'photo_closeup', photo)).status).toBe(200);
    expect((await upload('as_close', 'photo_closeup', photo)).status).toBe(200);  // repeated after a dropped connection
    expect((await upload('as_close', 'photo_closeup', Buffer.from('tampered'))).status).toBe(409);
    const back = Buffer.from(await (await fetch(`${base}/v1/sessions/${sid}/assets/as_close`, { headers: { Authorization: `Bearer ${token}` } })).arrayBuffer());
    expect(back.equals(photo)).toBe(true);
  });
});

describe('capture -> reconstruction -> QA gate -> candidates -> automatic CAD request', () => {
  it('runs the real Open3D QA and records a module-computed verdict', async () => {
    const fixture = path.join(tmp, 'fixture');
    execFileSync(config.geometryPython, ['tests/make_fixture.py', fixture], { cwd: path.join(path.dirname(config.objcap), '..') });
    const reference = readFileSync(path.join(fixture, 'reference.ply'));
    expect((await upload('as_ref', 'reference_mesh', reference, 'application/x-ply', { extension: 'ply', units: 'mm', synthetic: true })).status).toBe(200);
    expect((await api('POST', `/v1/sessions/${sid}/reference`, { assetId: 'as_ref' })).status).toBe(200);
    const policy = await api('POST', `/v1/sessions/${sid}/qa-policies`, {
      scaleAnchorAxis: 'longest', scaleAnchorMm: 80, holdoutAxis: 'middle', holdoutMm: 46, maxHoldoutErrorMm: 1,
      minCoverage: 0.8, maxFitRmsMm: 1.0, minFitInlierShare: 0.9,
    });
    expect(policy.status).toBe(200);
    const ids: string[] = [];
    for (let i = 0; i < 20; i++) {
      ids.push(`as_orbit_${i}`);
      expect((await upload(`as_orbit_${i}`, 'photo_orbit', Buffer.from(`synthetic orbit placeholder ${i}`))).status).toBe(200);
    }
    const unlinked = await api('POST', `/v1/sessions/${sid}/reconstructions`, { photoAssetIds: ids, policyId: policy.json.id, referenceUnitsPerMm: 1, requestId: 'recon-unlinked', autoDesignObjective: 'x' });
    expect(unlinked.status).toBe(400);   // an objective that could never reach Grain is refused, not silently dropped
    const grainProject = await api('POST', `/v1/sessions/${sid}/grain`, { createName: 'Lea test bracket' });
    expect(grainProject.status).toBe(200);

    const job = await api('POST', `/v1/sessions/${sid}/reconstructions`, {
      photoAssetIds: ids, policyId: policy.json.id, referenceUnitsPerMm: 1, requestId: 'recon-request-1',
      autoDesignObjective: 'A bracket that holds the fan 10 mm above the base, clear of the upright.',
    });
    expect(job.status).toBe(200);
    const again = await api('POST', `/v1/sessions/${sid}/reconstructions`, { photoAssetIds: ids, policyId: policy.json.id, referenceUnitsPerMm: 1, requestId: 'recon-request-1', autoDesignObjective: 'A bracket that holds the fan 10 mm above the base, clear of the upright.' });
    expect(again.json.id).toBe(job.json.id);  // retry returns the same job

    const lease = await Lease.claim({ store, assets, workerId: 'test' }, store.read().jobs.find(j => j.id === job.json.id)!, 120);
    // SYNTHETIC stand-in for Object Capture: copies the fixture mesh where lea-objcap would write its OBJ.
    await runReconstruction({ store, assets, workerId: 'test' }, lease!, async (_images, out) => {
      mkdirSync(path.join(out, 'obj'), { recursive: true });
      cpSync(path.join(fixture, 'scan.obj'), path.join(out, 'obj', 'baked_mesh.obj'));
      return JSON.stringify({ done: true, seconds: 0, usdz: path.join(out, 'none.usdz'), objDir: path.join(out, 'obj'), errors: [] });
    });

    const snap = (await api('GET', `/v1/sessions/${sid}`)).json;
    const report = snap.meshReports[0];
    expect(report.verdict).toBe('pass');
    expect(report.holdoutErrorMm).toBeLessThan(1);
    expect(snap.jobs.find((j: any) => j.id === job.json.id).status).toBe('succeeded');
    const geometry = snap.findings.filter((f: any) => f.source === 'geometry');
    expect(geometry.length).toBeGreaterThan(0);
    expect(geometry.every((f: any) => f.state === 'candidate' && f.evidenceAssetIds.includes(report.meshAssetId) && f.referenceAssetId === 'as_ref')).toBe(true);
    const auto = snap.designCommands.filter((c: any) => c.origin === 'auto');
    expect(auto).toHaveLength(1);
    expect(auto[0].text).toContain('80.00');
    // Originals untouched: each photo's bytes still hash to its recorded digest.
    for (const a of snap.assets.filter((x: any) => x.kind === 'photo_orbit')) expect(sha(readFileSync(assets.pathFor(a.sha256)))).toBe(a.sha256);
  });

  it('measures a model the phone built (guided scan) without rebuilding it', async () => {
    const t0 = store.read();
    const policy = t0.policies.find(p => p.sessionId === sid)!;
    const photos = t0.assets.filter(a => a.sessionId === sid && a.kind === 'photo_orbit').map(a => a.id);
    const mesh = readFileSync(path.join(tmp, 'fixture', 'scan.obj'));
    expect((await api('PUT', `/v1/sessions/${sid}/assets/as_phone_mesh`, mesh, { 'Content-Type': 'model/obj', 'x-lea-kind': 'recon_mesh', 'x-lea-sha256': sha(mesh),
      'x-lea-sources': photos.join(','), 'x-lea-meta': Buffer.from(JSON.stringify({ engine: 'apple-object-capture-ios/reduced (SYNTHETIC test mesh)' })).toString('base64') })).status).toBe(200);
    expect((await api('POST', `/v1/sessions/${sid}/reconstructions`, { photoAssetIds: photos, policyId: policy.id, referenceUnitsPerMm: 1, meshAssetId: 'as_ref', requestId: 'phone-bad' })).status).toBe(400);
    const job = await api('POST', `/v1/sessions/${sid}/reconstructions`, { photoAssetIds: photos, policyId: policy.id, referenceUnitsPerMm: 1, meshAssetId: 'as_phone_mesh', requestId: 'phone-scan-1' });
    expect(job.status).toBe(200);
    const lease = await Lease.claim({ store, assets, workerId: 'test' }, store.read().jobs.find(j => j.id === job.json.id)!, 120);
    await runReconstruction({ store, assets, workerId: 'test' }, lease!, async () => { throw new Error('must not rebuild a phone mesh'); });
    const report = store.read().meshReports.find(r => r.jobId === job.json.id)!;
    expect(report).toMatchObject({ meshAssetId: 'as_phone_mesh', verdict: 'pass', engine: 'apple-object-capture-ios/reduced (SYNTHETIC test mesh)' });
  });

  it('saves a pre-change version and records the real Grain outcome of the design turn', async () => {
    const t = store.read();
    const cmd = t.designCommands.find(c => c.sessionId === sid && c.origin === 'auto')!;
    const health = await grain.health();
    const lease = await Lease.claim({ store, assets, workerId: 'test' }, t.jobs.find(j => j.id === cmd.jobId)!, 120);
    const outcome = await runDesignTurn({ store, assets, workerId: 'test' }, grain, lease!, cmd);
    const after = store.read();
    const versions = after.designVersions.filter(v => v.commandId === cmd.id);
    expect(versions.find(v => v.phase === 'pre')).toBeDefined();
    const status = after.designCommands.find(c => c.id === cmd.id)!.status;
    if (health.agent_ready) {
      expect(versions.find(v => v.phase === 'post')).toBeDefined();
      expect(['succeeded', 'failed', 'needs_user']).toContain(status);
    } else {
      // This Grain has no model key: its chat answers 503 and nothing is built. Recorded as failed, not faked.
      expect(outcome).toBeUndefined();
      expect(status).toBe('failed');
      expect(after.jobs.find(j => j.id === cmd.jobId)!.error).toMatch(/503/);
    }
    expect(after.sessions.find(s => s.id === sid)!.activeDesignVersionId).toBe('');
  });
});

describe('voice', () => {
  it('sends explicit design changes, asks when unclear, and never acts on a non-final turn', async () => {
    expect(routeTranscript('Hey Leah, increase the mounting hole diameter by 2 mm')).toEqual({ kind: 'design', text: 'increase the mounting hole diameter by 2 mm' });
    expect(routeTranscript('Hey Leah, make it bigger').kind).toBe('clarify');
    expect(routeTranscript('what did you find').kind).toBe('status');
    const empty = await api('POST', `/v1/sessions/${sid}/voice-turns`, { id: 'vt_partial', mode: 'wake', transcript: '' });
    expect(empty.status).toBe(409);
    const turn = await api('POST', `/v1/sessions/${sid}/voice-turns`, { id: 'vt_status', mode: 'ptt', transcript: 'Leah, what did you find?' });
    expect(turn.status).toBe(200);
    expect(turn.json.turn.replyText).toMatch(/passed its scale, coverage and fit checks/);
    expect(turn.json.turn.replyText).toMatch(/unverified candidate/);
  });

  it('reports Cartesia as unavailable instead of pretending', async () => {
    const res = await api('POST', '/v1/voice/token');
    if (!config.cartesiaKey) expect(res.status).toBe(503);
  });
});

describe('repair evidence through the API', () => {
  it('judges a meter reading in code and records whether the repair worked', async () => {
    const finding = store.read().findings.find(f => f.sessionId === sid)!;
    const check = await api('POST', `/v1/sessions/${sid}/findings/${finding.id}/checks`, {
      purpose: 'diagnose', instruction: 'Measure continuity across the tab joint', safety: 'Unplugged', powerState: 'off',
      expectedMin: 0, expectedMax: 5, expectedUnit: 'ohm', expectedSource: 'continuity rule',
    });
    expect(check.status).toBe(200);
    await api('POST', `/v1/sessions/${sid}/checks/${check.json.id}/outcome`, { observed: 'as_expected', readingValue: 1e6, readingUnit: 'ohm' });
    expect(store.read().findings.find(f => f.id === finding.id)!.state).toBe('confirmed');

    const verify = await api('POST', `/v1/sessions/${sid}/findings/${finding.id}/checks`, {
      purpose: 'verify_repair', instruction: 'Re-measure after gluing', safety: 'Unplugged', powerState: 'off',
      expectedMin: 0, expectedMax: 5, expectedUnit: 'ohm', expectedSource: 'continuity rule',
    });
    await api('POST', `/v1/sessions/${sid}/repairs`, { findingId: finding.id, action: 'Glued and clamped tab', verifyCheckId: verify.json.id });
    await api('POST', `/v1/sessions/${sid}/checks/${verify.json.id}/outcome`, { observed: 'could_not_check' });
    expect(store.read().repairs.find(r => r.findingId === finding.id)!.result).toBe('unverified');
  });
});

describe('close-up checks', () => {
  it('sets each continuity range so that a healthy reading rules the fault out', async () => {
    // STUBBED Claude client: returns fixed candidates, so this tests Lea's mapping, not Claude's judgement.
    const { runCloseupCompare } = await import('../src/jobs/closeup');
    const photo = readFileSync(path.join(path.dirname(config.objcap), '../tests/fixture.jpg'));
    expect((await upload('as_cu', 'photo_closeup', photo)).status).toBe(200);
    expect((await upload('as_rp', 'reference_photo', Buffer.concat([photo, Buffer.from('ref')]))).status).toBe(200);
    const kinds = ['continuity_should_conduct', 'continuity_should_be_open'];
    const candidates = kinds.map(kind => ({ part: kind, observation: 'differs_possible_damage', problem: 'p', evidence: 'e', confidence: 'medium', check: { kind, instruction: 'probe A to B' } }));
    const stub = { beta: { messages: { parse: async () => ({ stop_reason: 'end_turn', parsed_output: { same_object_and_area: true, image_quality_problem: '', candidates } }) } } };
    await store.call('enqueue_job', { id: 'jb_cu', sessionId: sid, kind: 'closeup_compare', idempotencyKey: 'closeup:test', inputHash: 'a'.repeat(64),
      inputJson: JSON.stringify({ closeupAssetId: 'as_cu', referencePhotoAssetId: 'as_rp', subject: 'fan lead' }) });
    const lease = await Lease.claim({ store, assets, workerId: 'test' }, store.read().jobs.find(j => j.id === 'jb_cu')!, 60);
    await runCloseupCompare({ store, assets, workerId: 'test' }, lease!, stub as any);
    const t = store.read();
    const checkFor = (kind: string) => t.checks.find(c => c.findingId === t.findings.find(f => f.subject === kind)!.id)!;
    expect(checkFor('continuity_should_conduct')).toMatchObject({ expectedMin: 0, expectedMax: 4.99, expectedUnit: 'ohm', powerState: 'off' });
    expect(checkFor('continuity_should_be_open')).toMatchObject({ expectedMin: 5, expectedUnit: 'ohm', powerState: 'off' });
    expect(checkFor('continuity_should_be_open').expectedMax).toBeUndefined();
  });
});

describe('agent tools', () => {
  it('acts through the same gates: no design without asking, phone actions returned, measured spec sent', async () => {
    // SCRIPTED stand-in for Claude: it calls Lea's real tools in order, so this tests the tools and gates, not Claude.
    const { runAgentTurn } = await import('../src/agent');
    const script = (calls: [string, unknown][], text: string) => ({ beta: { messages: { toolRunner: async (params: any) => {
      const results: string[] = [];
      for (const [name, input] of calls) results.push(String(await params.tools.find((t: any) => t.name === name).run(input)));
      return { stop_reason: 'end_turn', content: [{ type: 'text', text: `${text} [${results.join(' | ')}]` }] };
    } } } }) as any;

    const scan = await runAgentTurn({ store, assets }, sid, 'vt_agent_1', 'Hey Leah, make a render of this for me', undefined, script([['start_scan', {}]], 'Opening the scan.'));
    expect(scan.actions).toEqual([{ type: 'scan' }]);

    const before = store.read().designCommands.length;
    const idle = await runAgentTurn({ store, assets }, sid, 'vt_agent_2', 'what seems to be the issue here', undefined, script([['design_solution', { objective: 'x' }]], ''));
    expect(idle.reply).toMatch(/has not asked for a design/);
    expect(store.read().designCommands.length).toBe(before);

    const finding = store.read().findings.find(f => f.sessionId === sid)!;
    const check = await runAgentTurn({ store, assets }, sid, 'vt_agent_3', 'how do I check that', undefined, script([['add_check', { finding_id: finding.id, kind: 'continuity_should_be_open', instruction: 'Probe pin 1 to pin 2' }]], ''));
    expect(check.actions).toEqual([{ type: 'screen', screen: 'repair' }]);
    expect(store.read().checks.at(-1)).toMatchObject({ expectedMin: 5, expectedUnit: 'ohm', powerState: 'off' });

    const solve = await runAgentTurn({ store, assets }, sid, 'vt_agent_4', "Hey Leah, let's make a solution for this", undefined,
      script([['design_solution', { objective: 'a bracket holding the fan 10 mm above the base' }]], ''));
    expect(solve.reply).toMatch(/Sent to Grain with the measured spec/);
    const spec = store.read().designCommands.at(-1)!;
    expect(spec.meshReportId).not.toBe('');
    expect(spec.text).toMatch(/Measured features/);
    expect(spec.text).toMatch(/Wall between planes .* 4\.0 mm thick|Plane 0: 80/);   // the synthetic bracket's 4 mm base / 80 mm plate
  });
});
