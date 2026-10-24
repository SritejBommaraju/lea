// The measured spec behind "let's make a solution for this": the validated scan's envelope and fit (from QA), plus
// planes, wall thicknesses and holes fitted by Open3D (reconstruction/lea_geometry/features.py). Grain's chat reads
// text only, so recognition happens here and Grain's own agent models the shapes. Nothing unmeasured is stated.
import { mkdtemp, readFile, rm, symlink } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import type { AssetStore } from './assets';
import { config } from './config';
import { lastJsonLine, run } from './jobs/context';
import { designPacket } from './jobs/reconstruction';
import type { MeshReportRow, StateStore } from './state';

interface Features {
  spacing_mm: number;
  planes: { id: number; normal: number[]; offset_mm: number; size_mm: number[] }[];
  holes: { diameter_mm: number; round: boolean; extent_mm: number[]; centre_mm: number[]; plane: number }[];
  walls: { planes: number[]; thickness_mm: number }[];
  error?: string;
}

export async function measuredFeatures(deps: { store: StateStore; assets: AssetStore }, report: MeshReportRow): Promise<Features> {
  const aligned = deps.store.read().assets.find(a => a.kind === 'derived_ply' && a.sourceAssetIds.includes(report.meshAssetId)
    && JSON.parse(a.metaJson || '{}').units === 'mm');
  if (!aligned) throw new Error('the validated scan has no aligned millimetre mesh');
  const dir = await mkdtemp(path.join(os.tmpdir(), 'lea-features-'));
  try {
    const file = path.join(dir, 'aligned.ply');
    await symlink(deps.assets.pathFor(aligned.sha256), file);
    const out = lastJsonLine(await run(config.geometryPython, ['-m', 'lea_geometry.features', file], undefined, 10 * 60_000, path.join(path.dirname(config.objcap), '..'))) as Features;
    if (out.error) throw new Error(`feature fitting failed: ${out.error}`);
    return out;
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

export async function featureSpec(deps: { store: StateStore; assets: AssetStore }, report: MeshReportRow, objective: string): Promise<string> {
  const qaAsset = deps.store.read().assets.find(a => a.id === report.reportAssetId)!;
  const qa = JSON.parse(await readFile(deps.assets.pathFor(qaAsset.sha256), 'utf8'));
  const f = await measuredFeatures(deps, report);
  const r1 = (v: number) => v.toFixed(1);
  const lines = [designPacket(report, qa, objective), '', `Measured features (reference frame, mm; precision about ±${r1(Math.max(report.fitRmsMm, f.spacing_mm))} mm):`];
  for (const p of f.planes) lines.push(`- Plane ${p.id}: ${p.size_mm.map(r1).join(' × ')} mm, normal [${p.normal.join(', ')}], offset ${r1(p.offset_mm)}`);
  for (const w of f.walls) lines.push(`- Wall between planes ${w.planes.join(' and ')}: ${r1(w.thickness_mm)} mm thick`);
  for (const h of f.holes) {
    lines.push(`- ${h.round ? 'Round hole' : 'Opening'} in plane ${h.plane}: ${h.round ? `Ø${r1(h.diameter_mm)}` : `${h.extent_mm.map(r1).join(' × ')}`} mm at [${h.centre_mm.map(r1).join(', ')}]`);
  }
  if (!f.holes.length) lines.push('- No holes larger than 2 mm were seen.');
  lines.push('Features smaller than about 2 mm, internal geometry and unseen faces are not in this list.');
  return lines.join('\n');
}
