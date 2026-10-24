// reconstruction: original photos -> Object Capture mesh -> Open3D QA against the pinned reference ->
// mesh report (verdict computed by the module) -> geometry candidates -> at most one automatic design turn.
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, readdir, readFile, rm, symlink } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { config } from '../config';
import type { AssetRow, MeshReportRow } from '../state';
import { type JobDeps, Lease, lastJsonLine, newId, run, sha256Hex } from './context';

export interface ReconstructionInput {
  photoAssetIds: string[];
  policyId: string;
  referenceUnitsPerMm: number;      // 1 for a reference in mm, 0.001 for metres; declared by the owner
  autoDesignObjective?: string;     // owner-frozen bracket/enclosure objective; absent = no automatic CAD
  meshAssetId?: string;             // a model the phone already built from these photos; skips building it here
}

interface QAReport {
  engine: string; scale_factor: number; measured_holdout_mm: number; box_extents_mm: number[]; coverage: number;
  fit_rms_mm: number; fit_inlier_share: number; correspondence_mm: number; reference_present: boolean; warnings: string[];
  scale_trusted: boolean; holdout_trusted: boolean;
  regions: { kind: string; centroid_mm: number[]; extent_mm: number[]; points: number; share_of_reference: number; max_deviation_mm: number }[];
}

export async function runReconstruction(deps: JobDeps, lease: Lease, objcap: (images: string, out: string, onLine: (l: string) => void) => Promise<string> = defaultObjcap) {
  const input = JSON.parse(lease.job.inputJson) as ReconstructionInput;
  const t = deps.store.read();
  const session = t.sessions.find(s => s.id === lease.job.sessionId)!;
  const policy = t.policies.find(p => p.id === input.policyId)!;
  const reference = t.assets.find(a => a.id === session.referenceMeshAssetId);
  const work = await mkdtemp(path.join(os.tmpdir(), 'lea-recon-'));
  try {
    // 1-2. The mesh: built on the phone (guided Object Capture, uploaded as recon_mesh) or here from the photos.
    const { meshAsset, scanFile, engine } = input.meshAssetId
      ? await phoneMesh(deps, t.assets.find(a => a.id === input.meshAssetId)!, work)
      : await buildOnMac(deps, lease, input, t.assets, work, objcap);

    // 3. Geometry QA (Open3D).
    await lease.progress('checking scale, coverage and fit');
    const qaOut = path.join(work, 'qa');
    const qaArgs = ['-m', 'lea_geometry.cli', '--scan', scanFile, '--anchor', policy.scaleAnchorLabel,
      '--anchor-mm', String(policy.scaleAnchorMm), '--holdout', policy.holdoutLabel, '--max-fit-rms-mm', String(policy.maxFitRmsMm),
      '--engine', engine, '--out', qaOut];
    if (reference) {
      const refExt = (JSON.parse(reference.metaJson || '{}').extension as string | undefined) ?? 'ply';
      const refLink = path.join(work, `reference.${refExt}`);
      await symlink(deps.assets.pathFor(reference.sha256), refLink);
      qaArgs.push('--reference', refLink, '--reference-units-per-mm', String(input.referenceUnitsPerMm));
    }
    const qa = lastJsonLine(await runGeometry(qaArgs));
    if (qa.error) throw new Error(`geometry QA: ${qa.error}`);
    const reportBytes = await readFile(qa.report);
    const report = JSON.parse(reportBytes.toString()) as QAReport;
    const reportAsset = await lease.registerBuffer('qa_report', reportBytes, 'application/json', [meshAsset.id, ...(reference ? [reference.id] : [])]);
    await lease.registerFile('derived_ply', qa.aligned_mesh, 'application/x-ply', [meshAsset.id], { frame: 'reference', units: 'mm' });
    await lease.registerFile('derived_ply', qa.grain_ply, 'application/x-ply', [meshAsset.id], { frame: 'reference', units: 'm', purpose: 'Grain scan upload' });

    // 4. The module decides pass/fail from these numbers and the frozen policy.
    const reportId = newId('mr');
    lease.release();
    await deps.store.call('record_mesh_report', {
      id: reportId, jobId: lease.job.id, attempt: lease.attempt, meshAssetId: meshAsset.id, reportAssetId: reportAsset.id, engine,
      // An untrusted side is sent as NaN, so the module fails that check instead of trusting a guess.
      scaleFactor: report.scale_trusted ? report.scale_factor : Number.NaN,
      measuredHoldoutMm: report.holdout_trusted ? report.measured_holdout_mm : Number.NaN, coverage: report.coverage,
      fitRmsMm: report.reference_present ? report.fit_rms_mm : Number.POSITIVE_INFINITY, fitInlierShare: report.fit_inlier_share,
      referenceMeshAssetId: report.reference_present && reference ? reference.id : '',
    });
    const mr = deps.store.read().meshReports.find(r => r.id === reportId)!;
    if (mr.verdict === 'pass') await afterPassingScan(deps, mr, report, input);
  } finally {
    await rm(work, { recursive: true, force: true });
  }
}

async function phoneMesh(deps: JobDeps, mesh: AssetRow, work: string) {
  // Open3D picks the reader from the extension, so the content-addressed file is linked under its real one.
  const scanFile = path.join(work, 'scan.obj');
  await symlink(deps.assets.pathFor(mesh.sha256), scanFile);
  const engine = String(JSON.parse(mesh.metaJson || '{}').engine ?? 'phone');
  return { meshAsset: mesh, scanFile, engine };
}

async function buildOnMac(deps: JobDeps, lease: Lease, input: ReconstructionInput, assets: AssetRow[], work: string,
  objcap: (images: string, out: string, onLine: (l: string) => void) => Promise<string>) {
  // Originals stay where they are; Object Capture reads them through links.
  const images = path.join(work, 'images');
  await mkdir(images);
  for (const id of input.photoAssetIds) {
    const a = assets.find(x => x.id === id)!;
    await symlink(deps.assets.pathFor(a.sha256), path.join(images, `${id}.${a.mediaType === 'image/heic' ? 'heic' : 'jpg'}`));
  }
  await lease.progress('reconstructing: 0%');
  let lastPct = -1;
  const done = lastJsonLine(await objcap(images, path.join(work, 'objcap'), line => {
    try {
      const m = JSON.parse(line);
      const pct = typeof m.progress === 'number' ? Math.floor(m.progress * 100) : -1;
      if (pct >= lastPct + 5) { lastPct = pct; void lease.progress(`reconstructing: ${pct}%`).catch(() => {}); }
    } catch { /* not JSON */ }
  }));
  if (!done.done) throw new Error(`Object Capture failed: ${(done.errors ?? [done.error]).join('; ')}`);
  const objFile = (await readdir(done.objDir)).find(f => f.toLowerCase().endsWith('.obj'));
  if (!objFile) throw new Error('Object Capture produced no OBJ');
  const engine = `apple-object-capture/${config.objcapDetail} (macOS ${os.release()})`;
  const scanFile = path.join(done.objDir, objFile);
  const meshAsset = await lease.registerFile('recon_mesh', scanFile, 'model/obj', input.photoAssetIds, { engine, seconds: done.seconds, unitsVerified: false });
  if (existsSync(done.usdz)) await lease.registerFile('recon_bundle', done.usdz, 'model/vnd.usdz+zip', [meshAsset.id], { engine, purpose: 'visual preview only' });
  return { meshAsset, scanFile, engine };
}

async function afterPassingScan(deps: JobDeps, mr: MeshReportRow, report: QAReport, input: ReconstructionInput) {
  // Every region where scan and reference differ becomes a candidate: it says where, not why.
  for (const [i, r] of report.regions.entries()) {
    const what = r.kind === 'missing_material' ? 'Reference surface not found in scan' : 'Scan material not in reference';
    await deps.store.call('record_finding', {
      id: `${mr.id.slice(0, 50)}_g${i}`, sessionId: mr.sessionId, subject: `region near (${r.centroid_mm.map(v => v.toFixed(0)).join(', ')}) mm`,
      problem: r.kind === 'missing_material' ? 'possibly broken or missing material' : 'possibly deformed or extra material',
      source: 'geometry', evidenceAssetIds: [mr.meshAssetId, mr.reportAssetId], referenceAssetId: mr.referenceMeshAssetId, meshReportId: mr.id,
      regionJson: JSON.stringify(r), confidenceKind: 'measured', confidenceText: `max deviation ${r.max_deviation_mm} mm over ${(r.share_of_reference * 100).toFixed(1)}% of the reference`,
      detail: `${what}: ${r.extent_mm.join(' × ')} mm region. An unseen side or reflective surface produces the same signal, so photograph this area close up before acting.`,
    });
  }
  const session = deps.store.read().sessions.find(s => s.id === mr.sessionId)!;
  if (!input.autoDesignObjective || !session.grainProjectId) return;
  const packet = designPacket(mr, report, input.autoDesignObjective);
  await deps.store.call('submit_design_command', {
    id: newId('dc'), sessionId: mr.sessionId, origin: 'auto', text: packet, meshReportId: mr.id,
    idempotencyKey: `auto:${mr.sessionId}:${mr.id}:${sha256Hex(input.autoDesignObjective).slice(0, 16)}:${session.referenceMeshAssetId}`,
    jobId: newId('jb'), inputHash: sha256Hex(packet),
  });
}

export function designPacket(mr: MeshReportRow, report: QAReport, objective: string) {
  const [l, m, s] = report.box_extents_mm.map(v => v.toFixed(2));
  return [
    `RepairOS measured-geometry design request (mesh report ${mr.id}).`,
    `Objective (owner-frozen): ${objective}`,
    `Measured object envelope, minimal oriented box: ${l} × ${m} × ${s} mm.`,
    `Scale: set from one owner-measured dimension and checked on a second; holdout error ${mr.holdoutErrorMm.toFixed(2)} mm.`,
    `Fit against the saved reference: RMS ${mr.fitRmsMm.toFixed(2)} mm, inlier share ${(mr.fitInlierShare * 100).toFixed(1)}%, coverage ${(mr.coverage * 100).toFixed(1)}%.`,
    report.regions.length ? `Regions differing from the reference (reference frame, mm): ${report.regions.map(r => `${r.kind} at [${r.centroid_mm.join(', ')}] size [${r.extent_mm.join(', ')}]`).join('; ')}.` : 'No regions differ from the reference beyond tolerance.',
    'Unknowns: internal geometry and anything outside the scanned surface. Do not invent dimensions; ask if a required dimension is missing.',
    `Evidence ids: mesh ${mr.meshAssetId}, QA report ${mr.reportAssetId}, reference ${mr.referenceMeshAssetId}.`,
  ].join('\n');
}

function defaultObjcap(images: string, out: string, onLine: (l: string) => void) {
  return run(config.objcap, [images, out, config.objcapDetail], onLine);
}

const GEOMETRY_DIR = path.join(path.dirname(config.objcap), '..');

function runGeometry(args: string[]) {
  return run(config.geometryPython, args, undefined, 30 * 60_000, GEOMETRY_DIR);
}
