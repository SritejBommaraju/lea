// The phone-facing API (/v1). Every route except /v1/health needs a paired-device token. Writes go
// through module reducers and return only after SpacetimeDB committed them.
import { randomUUID } from 'node:crypto';
import { mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import http, { type IncomingMessage, type ServerResponse } from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { AssetError, type AssetStore } from './assets';
import type { DeviceAuth } from './auth';
import { config } from './config';
import { GrainClient, GrainError } from './grain';
import { lastJsonLine, newId, run, sha256Hex } from './jobs/context';
import { type EventRow, ReducerError, sessionSnapshot, type StateStore, toWire } from './state';
import { AgentBudgetError, type ClientAction, runAgentTurn } from './agent';
import { askJev } from './jev';
import { lookAtFrame } from './look';
import { mintCartesiaToken, routeTranscriptWithJev, statusReply, VoiceUnavailable } from './voice';

const BOX_AXES = ['longest', 'middle', 'shortest'];
const ID = /^[A-Za-z0-9_-]{1,64}$/;

export class HttpError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}

export interface ApiDeps { store: StateStore; assets: AssetStore; auth: DeviceAuth; grain: GrainClient }

type Handler = (req: IncomingMessage, res: ServerResponse, p: Record<string, string>, url: URL) => Promise<unknown>;

export function createApi(deps: ApiDeps) {
  const { store, assets, auth, grain } = deps;
  const routes: [string, RegExp, string[], Handler][] = [];
  const route = (method: string, pattern: string, h: Handler) => {
    const names: string[] = [];
    const re = new RegExp('^' + pattern.replace(/:(\w+)/g, (_, n) => { names.push(n); return '([^/]+)'; }) + '$');
    routes.push([method, re, names, h]);
  };

  const call = async (...a: Parameters<StateStore['call']>) => {
    try { await store.call(...a); } catch (e) { throw new HttpError(409, (e as Error).message); }
  };
  const session = (sid: string) => {
    const s = store.read().sessions.find(x => x.id === sid);
    if (!s) throw new HttpError(404, `no session ${sid}`);
    return s;
  };
  const snapshot = (sid: string) => toWire(sessionSnapshot(store.read(), sid));

  route('GET', '/v1/health', async () => {
    const g = await grain.health().then(h => ({ reachable: true, agentReady: h.agent_ready, keysRequired: h.keys_required, model: h.model }))
      .catch((e: Error) => ({ reachable: false, error: e.message }));
    return {
      ok: store.connected, spacetimedb: { connected: store.connected, database: config.stdbDatabase }, grain: { url: config.grainUrl, ...g },
      voice: { cartesiaConfigured: Boolean(config.cartesiaKey && config.cartesiaVoiceId), wakeDetector: 'supplied on device' },
      vision: { anthropicConfigured: config.anthropicConfigured, jevConfigured: Boolean(config.jevKey) }, reconstruction: { engine: 'apple-object-capture', detail: config.objcapDetail },
    };
  });

  route('GET', '/v1/sessions', async () => toWire(store.read().sessions));

  route('POST', '/v1/sessions', async req => {
    const b = await body<{ id?: string; title: string }>(req);
    const id = b.id ?? newId('se');
    if (!ID.test(id)) throw new HttpError(400, 'bad session id');
    await call('create_session', { id, title: String(b.title ?? 'Repair session') });
    return snapshot(id);
  });

  route('GET', '/v1/sessions/:sid', async (_q, _r, p) => { session(p.sid); return snapshot(p.sid); });

  route('GET', '/v1/sessions/:sid/events', async (req, res, p, url) => {
    session(p.sid);
    const after = BigInt(url.searchParams.get('after') ?? '0');
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
    const send = (e: EventRow) => res.write(`id: ${e.seq}\nevent: change\ndata: ${JSON.stringify(toWire(e))}\n\n`);
    // Replay what the phone missed, then stream live; the phone refetches the snapshot on each event.
    for (const e of store.read().events.filter(e => e.sessionId === p.sid && e.seq > after).sort((a, b) => Number(a.seq - b.seq))) send(e);
    const off = store.onEvent(e => { if (e.sessionId === p.sid) send(e); });
    const ping = setInterval(() => res.write(': ping\n\n'), 15_000);
    req.on('close', () => { off(); clearInterval(ping); });
    return STREAMING;
  });

  route('PUT', '/v1/sessions/:sid/assets/:aid', async (req, _r, p) => {
    session(p.sid);
    if (!ID.test(p.aid)) throw new HttpError(400, 'bad asset id');
    const kind = header(req, 'x-lea-kind');
    const declared = header(req, 'x-lea-sha256');
    const meta = req.headers['x-lea-meta'] ? Buffer.from(String(req.headers['x-lea-meta']), 'base64').toString('utf8') : '{}';
    JSON.parse(meta);
    const prior = store.read().assets.find(a => a.id === p.aid);
    if (prior) {
      // Resumed or repeated upload of the same original: drain and confirm, never overwrite.
      req.resume();
      if (prior.sha256 !== declared) throw new HttpError(409, `asset ${p.aid} already holds different content`);
      return toWire(prior);
    }
    const { sha256, bytes } = await assets.put(req, declared);
    await call('register_asset', {
      id: p.aid, sessionId: p.sid, kind, sha256, bytes: BigInt(bytes), mediaType: req.headers['content-type'] ?? 'application/octet-stream',
      sourceAssetIds: String(req.headers['x-lea-sources'] ?? '').split(',').filter(Boolean), metaJson: meta,
    });
    return toWire(store.read().assets.find(a => a.id === p.aid));
  });

  route('GET', '/v1/sessions/:sid/assets/:aid', async (_q, res, p) => {
    const a = store.read().assets.find(x => x.id === p.aid && x.sessionId === p.sid);
    if (!a) throw new HttpError(404, 'no such asset');
    res.writeHead(200, { 'Content-Type': a.mediaType, 'Content-Length': String(a.bytes), 'X-Lea-SHA256': a.sha256, 'Cache-Control': 'private, max-age=31536000, immutable' });
    assets.open(a.sha256).pipe(res);
    return STREAMING;
  });

  route('POST', '/v1/sessions/:sid/reference', async (req, _r, p) => {
    const b = await body<{ assetId: string }>(req);
    await call('pin_reference_mesh', { sessionId: p.sid, assetId: b.assetId });
    return snapshot(p.sid);
  });

  // The validated scan as the phone draws it: aligned to the reference (mm), decimated, with each geometry candidate's region.
  route('GET', '/v1/sessions/:sid/mesh-reports/:rid/view', async (_q, _r, p) => {
    const t = store.read();
    const report = t.meshReports.find(r => r.id === p.rid && r.sessionId === p.sid);
    if (!report) throw new HttpError(404, 'no such mesh report');
    const aligned = t.assets.find(a => a.kind === 'derived_ply' && a.sourceAssetIds.includes(report.meshAssetId) && JSON.parse(a.metaJson || '{}').units === 'mm');
    if (!aligned) throw new HttpError(409, 'this scan has no aligned mesh');
    const dir = await mkdtemp(path.join(os.tmpdir(), 'lea-view-'));
    try {
      const src = path.join(dir, 'aligned.ply'), out = path.join(dir, 'view.bin');
      await symlink(assets.pathFor(aligned.sha256), src);
      const info = lastJsonLine(await run(config.geometryPython, ['-m', 'lea_geometry.mesh_view', src, out], undefined, 120_000, path.join(path.dirname(config.objcap), '..')));
      if (info.error) throw new HttpError(422, info.error);
      const bin = await readFile(out);
      const split = info.vertices * 12;
      return {
        units: 'mm', verdict: report.verdict, vertices: info.vertices, triangles: info.triangles,
        positions: bin.subarray(0, split).toString('base64'), indices: bin.subarray(split).toString('base64'),
        regions: t.findings.filter(f => f.meshReportId === report.id && f.source === 'geometry').map(f => {
          const r = JSON.parse(f.regionJson || '{}');
          return { findingId: f.id, kind: r.kind ?? '', centroid: r.centroid_mm ?? [0, 0, 0], extent: r.extent_mm ?? [0, 0, 0] };
        }),
      };
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  route('POST', '/v1/sessions/:sid/qa-policies', async (req, _r, p) => {
    const b = await body<{ scaleAnchorAxis: string; scaleAnchorMm: number; holdoutAxis: string; holdoutMm: number; maxHoldoutErrorMm: number; minCoverage: number; maxFitRmsMm: number; minFitInlierShare: number }>(req);
    if (!BOX_AXES.includes(b.scaleAnchorAxis) || !BOX_AXES.includes(b.holdoutAxis)) throw new HttpError(400, `axes must be one of ${BOX_AXES.join(', ')}`);
    const id = newId('qp');
    await call('set_qa_policy', {
      id, sessionId: p.sid, scaleAnchorLabel: b.scaleAnchorAxis, scaleAnchorMm: num(b.scaleAnchorMm), holdoutLabel: b.holdoutAxis,
      holdoutMm: num(b.holdoutMm), maxHoldoutErrorMm: num(b.maxHoldoutErrorMm), minCoverage: num(b.minCoverage),
      maxFitRmsMm: num(b.maxFitRmsMm), minFitInlierShare: num(b.minFitInlierShare),
    });
    return toWire(store.read().policies.find(x => x.id === id));
  });

  route('POST', '/v1/sessions/:sid/reconstructions', async (req, _r, p) => {
    const b = await body<{ photoAssetIds: string[]; policyId: string; referenceUnitsPerMm: number; autoDesignObjective?: string; meshAssetId?: string; requestId: string }>(req);
    if (b.meshAssetId && !store.read().assets.some(a => a.id === b.meshAssetId && a.sessionId === p.sid && a.kind === 'recon_mesh')) {
      throw new HttpError(400, 'meshAssetId must name a recon_mesh uploaded to this session');
    }
    // An objective with no linked Grain project would silently never become a design request.
    if (b.autoDesignObjective && !session(p.sid).grainProjectId) throw new HttpError(400, 'link a Grain project before asking for automatic CAD, or leave the objective empty');
    const input = { photoAssetIds: [...b.photoAssetIds].sort(), policyId: b.policyId, referenceUnitsPerMm: num(b.referenceUnitsPerMm), ...(b.autoDesignObjective ? { autoDesignObjective: b.autoDesignObjective } : {}), ...(b.meshAssetId ? { meshAssetId: b.meshAssetId } : {}) };
    const inputJson = JSON.stringify(input);
    return enqueue(p.sid, 'reconstruction', b.requestId, inputJson);
  });

  // A live camera frame for coaching (body: JPEG). Not stored: only photos the person takes are evidence.
  route('POST', '/v1/sessions/:sid/look', async (req, _r, p) => {
    session(p.sid);
    if (!config.jevKey) throw new HttpError(503, 'live look needs JEV_API_KEY on the bridge');
    const frame = await rawBody(req, 8 * 1024 * 1024);
    const refId = typeof req.headers['x-lea-reference'] === 'string' ? req.headers['x-lea-reference'] : '';
    const ref = refId ? store.read().assets.find(a => a.id === refId && a.sessionId === p.sid && a.kind === 'reference_photo') : undefined;
    if (refId && !ref) throw new HttpError(400, 'x-lea-reference must name a reference_photo in this session');
    const subject = decodeURIComponent(String(req.headers['x-lea-subject'] ?? '')).slice(0, 120);
    const refBytes = ref ? await readStream(assets.open(ref.sha256)) : undefined;
    const seen = await lookAtFrame(config.jevKey, frame, subject, refBytes).catch(e => { throw new HttpError(400, (e as Error).message); });
    if (!seen) throw new HttpError(504, 'Jev did not answer in time');
    return seen;
  });

  route('POST', '/v1/sessions/:sid/closeup-comparisons', async (req, _r, p) => {
    const b = await body<{ closeupAssetId: string; referencePhotoAssetId: string; subject: string; requestId: string }>(req);
    const t = store.read();
    const close = t.assets.find(a => a.id === b.closeupAssetId && a.sessionId === p.sid && a.kind === 'photo_closeup');
    const ref = t.assets.find(a => a.id === b.referencePhotoAssetId && a.sessionId === p.sid && a.kind === 'reference_photo');
    if (!close || !ref) throw new HttpError(400, 'need a photo_closeup and a reference_photo from this session');
    if (!config.anthropicConfigured) throw new HttpError(503, 'close-up comparison needs ANTHROPIC_API_KEY on the bridge');
    return enqueue(p.sid, 'closeup_compare', b.requestId, JSON.stringify({ closeupAssetId: close.id, referencePhotoAssetId: ref.id, subject: String(b.subject ?? '') }));
  });

  async function enqueue(sid: string, kind: string, requestId: string, inputJson: string) {
    if (!requestId || requestId.length < 8) throw new HttpError(400, 'requestId (8+ characters) is required so retries do not duplicate work');
    const id = newId('jb');
    await call('enqueue_job', { id, sessionId: sid, kind, idempotencyKey: `${kind}:${sid}:${requestId}`, inputHash: sha256Hex(inputJson), inputJson });
    return toWire(store.read().jobs.find(j => j.idempotencyKey === `${kind}:${sid}:${requestId}`));
  }

  route('POST', '/v1/sessions/:sid/grain', async (req, _r, p) => {
    const s = session(p.sid);
    const b = await body<{ grainProjectId?: string; createName?: string }>(req);
    let pid = b.grainProjectId;
    if (!pid) {
      if (!b.createName) throw new HttpError(400, 'send grainProjectId or createName');
      pid = (await grain.createProject(b.createName)).id;
    } else {
      await grain.project(pid);  // 404 here if it does not exist
    }
    if (!s.grainProjectId) await call('link_grain_project', { sessionId: p.sid, grainProjectId: pid });
    return snapshot(p.sid);
  });

  // The linked design's model.glb (millimetres) as this session's saved reference, when none is pinned yet.
  route('POST', '/v1/sessions/:sid/reference-from-grain', async (_q, _r, p) => {
    const s = session(p.sid);
    if (!s.grainProjectId) throw new HttpError(409, 'link a Grain project first');
    if (s.referenceMeshAssetId) throw new HttpError(409, 'this session is already pinned to a reference; start a new session to change it');
    const glb = await grain.file(s.grainProjectId, 'out/model.glb').catch((e: GrainError) => {
      throw new HttpError(e.status === 404 ? 409 : 502, e.status === 404 ? 'the Grain design has no built model yet' : e.message);
    });
    const dir = await mkdtemp(path.join(os.tmpdir(), 'lea-ref-'));
    try {
      await writeFile(path.join(dir, 'model.glb'), glb);
      const out = lastJsonLine(await run(config.geometryPython, ['-m', 'lea_geometry.glb_to_ply', path.join(dir, 'model.glb'), path.join(dir, 'reference.ply')],
        undefined, 120_000, path.join(path.dirname(config.objcap), '..')));
      if (out.error) throw new HttpError(422, out.error);
      const { sha256, bytes } = await assets.putFile(path.join(dir, 'reference.ply'));
      const id = newId('as');
      await call('register_asset', { id, sessionId: p.sid, kind: 'reference_mesh', sha256, bytes: BigInt(bytes), mediaType: 'application/x-ply', sourceAssetIds: [],
        metaJson: JSON.stringify({ extension: 'ply', unitsPerMm: '1', from: 'grain', grainProjectId: s.grainProjectId, grainPath: 'out/model.glb', glbSha256: sha256Hex(glb) }) });
      await call('pin_reference_mesh', { sessionId: p.sid, assetId: id });
      return snapshot(p.sid);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  route('GET', '/v1/sessions/:sid/grain', async (_q, _r, p) => {
    const s = session(p.sid);
    if (!s.grainProjectId) return { linked: false };
    const [project, log, files] = await Promise.all([grain.project(s.grainProjectId), grain.messages(s.grainProjectId), grain.files(s.grainProjectId)]);
    return { linked: true, project, question: project.waiting ? GrainClient.lastQuestion(log) : null, messages: log.slice(-40), files };
  });

  // Expected voltages from the linked design, offered as sourced ranges when adding a meter check.
  route('GET', '/v1/sessions/:sid/grain/electrical', async (_q, _r, p) => {
    const s = session(p.sid);
    if (!s.grainProjectId) return { nets: [] };
    const e = await grain.electrical(s.grainProjectId);
    return { nets: Object.entries(e.expected_volts ?? {}).map(([net, v]) => ({ net, min: v.min, max: v.max, source: `Grain design (${v.from || net})` })) };
  });

  route('GET', '/v1/sessions/:sid/grain/events', async (req, res, p) => {
    const s = session(p.sid);
    if (!s.grainProjectId) throw new HttpError(409, 'no Grain project linked');
    const abort = new AbortController();
    req.on('close', () => abort.abort());
    const upstream = await grain.events(s.grainProjectId, abort.signal);
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
    void (async () => {
      try { for await (const chunk of upstream as unknown as AsyncIterable<Uint8Array>) res.write(chunk); } catch { /* client left */ }
      res.end();
    })();
    return STREAMING;
  });

  route('POST', '/v1/sessions/:sid/design-commands', async (req, _r, p) => {
    // checks: dimensions the returned STEP must meet, [{axis: longest|middle|shortest, mm, tolMm}].
    const b = await body<{ text: string; origin: 'text' | 'voice'; meshReportId?: string; requestId: string; checks?: unknown[] }>(req);
    return submitDesign(p.sid, b.origin === 'voice' ? 'voice' : 'text', b.text, b.meshReportId ?? '', b.requestId, b.checks);
  });

  async function submitDesign(sid: string, origin: string, text: string, meshReportId: string, requestId: string, checks: unknown[] = []) {
    if (!requestId || requestId.length < 8) throw new HttpError(400, 'requestId is required');
    const id = newId('dc');
    const key = `${origin}:${sid}:${requestId}`;
    await call('submit_design_command', { id, sessionId: sid, origin, text, meshReportId, idempotencyKey: key, jobId: newId('jb'), inputHash: sha256Hex(text), checksJson: checks.length ? JSON.stringify(checks) : '' });
    return toWire(store.read().designCommands.find(c => c.idempotencyKey === key));
  }

  route('POST', '/v1/sessions/:sid/findings/:fid/checks', async (req, _r, p) => {
    const b = await body<{ purpose: string; instruction: string; safety: string; powerState: string; expectedMin?: number; expectedMax?: number; expectedUnit?: string; expectedSource?: string; net?: string }>(req);
    const id = newId('ck');
    await call('add_repair_check', {
      id, sessionId: p.sid, findingId: p.fid, purpose: b.purpose, instruction: String(b.instruction ?? ''), safety: String(b.safety ?? ''),
      powerState: b.powerState, expectedMin: opt(b.expectedMin), expectedMax: opt(b.expectedMax), expectedUnit: b.expectedUnit ?? '', expectedSource: b.expectedSource ?? '',
      net: String(b.net ?? ''),
    });
    return toWire(store.read().checks.find(c => c.id === id));
  });

  // Pending checks in the order to do them. Jev ranks them; when it can't, the list says so and keeps creation order.
  route('GET', '/v1/sessions/:sid/next-checks', async (_q, _r, p) => {
    session(p.sid);
    const t = store.read();
    const pending = t.checks.filter(c => c.sessionId === p.sid && c.outcome === 'pending')
      .sort((a, b) => Number(a.createdAt.microsSinceUnixEpoch - b.createdAt.microsSinceUnixEpoch));
    if (pending.length < 2) return { ranked: pending.length === 1, order: pending.map(c => c.id) };
    const about = (c: typeof pending[number]) => {
      const f = t.findings.find(x => x.id === c.findingId);
      return `${c.instruction} (checks: ${f?.subject ?? 'a finding'}, ${f?.problem ?? ''}; ${c.powerState === 'off' ? 'unplugged' : 'powered'})`;
    };
    const res = config.jevKey ? await askJev(config.jevKey, {
      findings: t.findings.filter(f => f.sessionId === p.sid).map(f => `${f.subject}: ${f.problem} (${f.state}, ${f.confidenceText})`),
    }, { next: { type: 'choice', instructions: 'Which check should the person do first: the one most likely to find or rule out the fault, safe ones first?', criteria: Object.fromEntries(pending.map(c => [c.id, about(c)])) } }) : null;
    const probs = res?.answers.next?.probabilities;
    if (!probs) return { ranked: false, reason: 'unable_to_rank', order: pending.map(c => c.id) };
    return { ranked: true, order: [...pending].sort((a, b) => (probs[b.id] ?? 0) - (probs[a.id] ?? 0)).map(c => c.id) };
  });

  route('POST', '/v1/sessions/:sid/checks/:cid/outcome', async (req, _r, p) => {
    const b = await body<{ observed: string; observationAssetId?: string; readingValue?: number; readingUnit?: string; note?: string }>(req);
    const check = store.read().checks.find(c => c.id === p.cid && c.sessionId === p.sid);
    // A voltage on a named net is also judged by Grain against its design; kept beside our verdict, never instead.
    let crossCheck = '';
    const pid = session(p.sid).grainProjectId;
    if (check?.net && pid && b.readingUnit === 'V' && typeof b.readingValue === 'number') {
      crossCheck = await grain.electricalCheck(pid, [{ net: check.net, volts: b.readingValue }])
        .then(r => JSON.stringify({ row: r.readings[0] ?? null, findings: r.findings.map(f => f.text).slice(0, 3) }))
        .catch((e: Error) => JSON.stringify({ error: e.message }));
    }
    await call('record_check_outcome', {
      checkId: p.cid, observed: b.observed, observationAssetId: b.observationAssetId ?? '', readingValue: opt(b.readingValue), readingUnit: b.readingUnit ?? '', note: b.note ?? '', crossCheck,
    });
    return snapshot(p.sid);
  });

  route('POST', '/v1/sessions/:sid/repairs', async (req, _r, p) => {
    const b = await body<{ findingId: string; action: string; verifyCheckId: string }>(req);
    await call('record_repair_attempt', { id: newId('rp'), sessionId: p.sid, findingId: b.findingId, action: String(b.action ?? ''), verifyCheckId: b.verifyCheckId });
    return snapshot(p.sid);
  });

  route('POST', '/v1/voice/token', async () => {
    try { return await mintCartesiaToken(); } catch (e) {
      if (e instanceof VoiceUnavailable) throw new HttpError(503, e.message);
      throw e;
    }
  });

  route('POST', '/v1/sessions/:sid/voice-turns', async (req, _r, p) => {
    // frame: the camera view as base64 JPEG (optional), seen by the agent for this turn only and never stored.
    const b = await body<{ id: string; mode: string; transcript: string; audioAssetId?: string; frame?: string; focus?: string }>(req, 6_000_000);
    if (!ID.test(b.id ?? '')) throw new HttpError(400, 'voice turn id required');
    const transcript = String(b.transcript ?? '');
    if (!transcript.trim()) throw new HttpError(409, 'only final, non-empty transcripts are recorded');
    const prior = store.read().voiceTurns.find(v => v.id === b.id);
    if (prior) return { turn: toWire(prior), actions: [] };
    let reply = '', commandId = '', actions: ClientAction[] = [], note = '';
    if (config.anthropicConfigured) {
      try {
        ({ reply, actions } = await runAgentTurn({ store, assets }, p.sid, b.id, transcript, b.frame ? Buffer.from(b.frame, 'base64') : undefined, b.focus));
      } catch (e) {
        if (e instanceof AgentBudgetError) note = `I've used this session's assistant budget, so I'll keep to simple commands. `;
        else console.error('[agent]', e);   // fall through to the rule-based router below
      }
    }
    if (!reply) {
      const intent = await routeTranscriptWithJev(config.jevKey, transcript);
      if (intent.kind === 'design') {
        const cmd = await submitDesign(p.sid, 'voice', intent.text, '', b.id) as { id: string };
        commandId = cmd.id;
        reply = `Sending that to Grain now: ${intent.text}. I will save the design before and after the change.`;
      } else if (intent.kind === 'status') {
        reply = statusReply(store.read(), p.sid);
      } else {
        reply = intent.question;
      }
    }
    reply = note + reply;
    await call('record_voice_turn', { id: b.id, sessionId: p.sid, mode: b.mode, transcript, replyText: reply, audioAssetId: b.audioAssetId ?? '', commandId });
    return { turn: toWire(store.read().voiceTurns.find(v => v.id === b.id)), actions };
  });

  return http.createServer(async (req, res) => {
    const url = new URL(req.url ?? '/', 'http://bridge');
    const requestId = randomUUID();
    try {
      const match = routes.find(([m, re]) => m === req.method && re.test(url.pathname));
      if (!match) throw new HttpError(404, 'no such route');
      if (url.pathname !== '/v1/health' && !auth.verify(req.headers.authorization)) throw new HttpError(401, 'pair this device first: npm run pair');
      const [, re, names, handler] = match;
      const values = re.exec(url.pathname)!.slice(1).map(decodeURIComponent);
      const out = await handler(req, res, Object.fromEntries(names.map((n, i) => [n, values[i]])), url);
      if (out === STREAMING) return;
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(out));
    } catch (e) {
      const status = e instanceof HttpError || e instanceof AssetError ? e.status : e instanceof GrainError ? (e.status === 409 ? 409 : 502)
        : e instanceof ReducerError ? 409 : e instanceof SyntaxError ? 400 : 500;
      if (status === 500) console.error(`[http ${requestId}]`, e);
      if (!res.headersSent) res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { status, message: (e as Error).message, requestId } }));
    }
  });
}

const STREAMING = Symbol('streaming');

async function body<T>(req: IncomingMessage, limit = 1_000_000): Promise<T> {
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const c of req) {
    size += c.length;
    if (size > limit) throw new HttpError(413, 'request body too large');
    chunks.push(c);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') as T;
}

async function rawBody(req: IncomingMessage, limit: number): Promise<Buffer> {
  return readStream(req, limit);
}

async function readStream(stream: AsyncIterable<Buffer | string>, limit = Number.POSITIVE_INFINITY): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const c of stream) {
    const b = typeof c === 'string' ? Buffer.from(c) : c;
    size += b.length;
    if (size > limit) throw new HttpError(413, 'body too large');
    chunks.push(b);
  }
  return Buffer.concat(chunks);
}

function header(req: IncomingMessage, name: string) {
  const v = req.headers[name];
  if (typeof v !== 'string' || !v) throw new HttpError(400, `${name} header is required`);
  return v;
}

function num(v: unknown) {
  if (typeof v !== 'number' || !Number.isFinite(v)) throw new HttpError(400, 'numbers must be finite');
  return v;
}
const opt = (v: unknown) => (v === undefined || v === null ? undefined : num(v));

