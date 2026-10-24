// design_turn: one RepairOS design change through Grain's existing chat, bracketed by full snapshots
// of the project's src/ and out/ so every version stays openable in Lea even if Grain later changes.
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { config } from '../config';
import { GrainClient, GrainError, type GrainProject } from '../grain';
import type { DesignCommandRow } from '../state';
import { type JobDeps, Lease, lastJsonLine, newId, run, sha256Hex } from './context';

const POLL_MS = 5_000;
const TURN_LIMIT_MS = 45 * 60_000;
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

export interface SnapshotManifest {
  schema: 'lea.design_snapshot/1';
  grainProjectId: string; phase: string; takenAt: string; project: GrainProject; messageCount: number;
  files: { path: string; size: number; mtime?: number; sha256: string; assetId: string }[];
  stepCheck?: unknown;
}

export interface DimensionCheck { axis: 'longest' | 'middle' | 'shortest'; mm: number; tolMm: number }

/** stepChecks: re-import out/model.step and check these dimensions (false: no STEP check). */
export async function snapshot(grain: GrainClient, lease: Lease, pid: string, phase: string, stepChecks: DimensionCheck[] | false = false) {
  const [project, files, messages] = await Promise.all([grain.project(pid), grain.files(pid), grain.messages(pid)]);
  const previous = await lastManifest(lease, pid);
  const entries: SnapshotManifest['files'] = [];
  let step: unknown;
  for (const f of files) {
    // Unchanged since the last snapshot (same path, size and modification time): reuse it, don't download it again.
    const same = previous?.files.find(e => e.path === f.path && e.size === f.size && e.mtime === f.mtime);
    let digest: string, id: string;
    if (same) {
      ({ sha256: digest, assetId: id } = same);
    } else {
      const data = await grain.file(pid, f.path);
      digest = sha256Hex(data);
      // One row per distinct file content per session, shared by every snapshot that contains it.
      id = `df_${digest.slice(0, 40)}_${lease.job.sessionId.slice(0, 20)}`;
      if (!lease.deps.store.read().assets.find(a => a.id === id)) await lease.registerBuffer('design_file', data, mediaTypeFor(f.path), [], { grainPath: f.path }, id);
    }
    entries.push({ path: f.path, size: f.size, mtime: f.mtime, sha256: digest, assetId: id });
    if (stepChecks && f.path === 'out/model.step') step = await checkStep(lease.deps.assets.pathFor(digest), stepChecks);
  }
  const manifest: SnapshotManifest = {
    schema: 'lea.design_snapshot/1', grainProjectId: pid, phase, takenAt: new Date().toISOString(), project,
    messageCount: messages.length, files: entries, ...(step ? { stepCheck: step } : {}),
  };
  const asset = await lease.registerBuffer('design_snapshot', Buffer.from(JSON.stringify(manifest, null, 2)), 'application/json', [], { phase, grainProjectId: pid });
  return { manifest, asset };
}

async function checkStep(file: string, checks: DimensionCheck[]) {
  try {
    const expect = checks.flatMap(c => ['--expect', `${c.axis}=${c.mm}:${c.tolMm}`]);
    return lastJsonLine(await run(config.stepPython, [path.join(path.dirname(config.objcap), '../step/step_check.py'), file, ...expect]));
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
}

/** The newest snapshot this session took of this Grain project, or null. */
async function lastManifest(lease: Lease, pid: string): Promise<SnapshotManifest | null> {
  const latest = lease.deps.store.read().assets
    .filter(a => a.sessionId === lease.job.sessionId && a.kind === 'design_snapshot' && JSON.parse(a.metaJson || '{}').grainProjectId === pid)
    .sort((a, b) => Number(b.createdAt.microsSinceUnixEpoch - a.createdAt.microsSinceUnixEpoch))[0];
  if (!latest) return null;
  try { return JSON.parse(await readFile(lease.deps.assets.pathFor(latest.sha256), 'utf8')); } catch { return null; }
}

function mediaTypeFor(p: string) {
  const ext = path.extname(p).toLowerCase();
  return ({ '.json': 'application/json', '.md': 'text/markdown', '.py': 'text/x-python', '.csv': 'text/csv', '.svg': 'image/svg+xml',
    '.glb': 'model/gltf-binary', '.step': 'model/step', '.stl': 'model/stl', '.png': 'image/png' } as Record<string, string>)[ext] ?? 'application/octet-stream';
}

const reportDigest = (m: SnapshotManifest) => m.files.find(f => f.path === 'out/report.json')?.sha256 ?? '';

export async function runDesignTurn(deps: JobDeps, grain: GrainClient, lease: Lease, cmd: DesignCommandRow) {
  const pid = cmd.grainProjectId;
  // Wait for any turn already running in this Grain project (Grain allows one at a time).
  for (let waited = 0; ; waited += POLL_MS) {
    const p = await grain.project(pid);
    if (!p.busy) break;
    if (waited > TURN_LIMIT_MS) return lease.finish('failed', 'Grain project stayed busy for 45 minutes; nothing was sent');
    await lease.progress('waiting for the current Grain turn to finish');
    await sleep(POLL_MS);
  }

  const pre = await snapshot(grain, lease, pid, 'pre');
  const preId = newId('dv');
  await deps.store.call('record_design_version', {
    id: preId, sessionId: cmd.sessionId, commandId: cmd.id, phase: 'pre', manifestAssetId: pre.asset.id,
    fileCount: pre.manifest.files.length, buildOk: pre.manifest.project.last_build?.ok, jobId: lease.job.id, attempt: lease.attempt,
  });

  await lease.progress('dispatched to Grain');
  try {
    await grain.chat(pid, cmd.text);
  } catch (e) {
    const err = e as GrainError;
    if (err.ambiguous) {
      // Grain may have started the turn; do not resend. The operator reconciles from Grain's messages.
      return lease.finish('uncertain', `${err.message}. The turn may have started; check Grain before resending.`);
    }
    return lease.finish('failed', err.message);
  }

  const started = Date.now();
  let project: GrainProject;
  for (;;) {
    await sleep(POLL_MS);
    project = await grain.project(pid);
    if (!project.busy) break;
    await lease.progress('Grain is working');
    if (Date.now() - started > TURN_LIMIT_MS) return lease.finish('uncertain', 'Grain turn still running after 45 minutes');
  }

  const checks = (JSON.parse(lease.job.inputJson).checks ?? []) as DimensionCheck[];
  const post = await snapshot(grain, lease, pid, 'post', checks);
  const rebuilt = reportDigest(post.manifest) !== '' && reportDigest(post.manifest) !== reportDigest(pre.manifest);
  // Requested dimensions must be met by a real, re-imported solid; a build that misses them never becomes active.
  const dimensionsMet = !checks.length || (post.manifest.stepCheck as { ok?: boolean } | undefined)?.ok === true;
  const question = project.waiting ? GrainClient.lastQuestion(await grain.messages(pid)) : null;
  await deps.store.call('record_design_version', {
    id: newId('dv'), sessionId: cmd.sessionId, commandId: cmd.id, phase: 'post', manifestAssetId: post.asset.id,
    fileCount: post.manifest.files.length, buildOk: project.waiting ? undefined : rebuilt && project.last_build?.ok === true && dimensionsMet, jobId: lease.job.id, attempt: lease.attempt,
  });
  lease.release();
  return { rebuilt, question, stepCheck: post.manifest.stepCheck };
}
