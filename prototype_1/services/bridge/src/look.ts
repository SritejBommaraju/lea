// Live look at a camera frame, the way VEL looks at a screen (VEL's app/look.mjs): one fast Jev call answers
// typed questions (usable? what's wrong? what part? where? same area as the reference? looks damaged?), and code turns
// the answers into at most one short spoken tip. Frames are guidance only: never stored, never evidence. A photo the
// person takes is the evidence, and only the close-up comparison and checks can turn it into a finding.
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { run } from './jobs/context';
import { askJev, jevImage, type JevAnswer, type JevAsset, type JevQuestion } from './jev';

export const PROBLEMS = {
  ok: 'Sharp, well lit, the part fills a good share of the picture',
  blurry: 'Motion blur or out of focus, so small details are smeared',
  too_dark: 'Too dark to see small details',
  glare: 'A reflection or glare hides part of the subject',
  too_far: 'The subject is small in the picture; small details cannot be seen',
  too_close: 'So close that the subject is cut off or out of focus',
  nothing_relevant: 'No product or part is visible (a floor, a hand, the room)',
};
export const PARTS = {
  connector: 'A plug, socket or connector housing',
  wire: 'Wires or a cable',
  circuit_board: 'A printed circuit board with components',
  fastener: 'A screw, bolt, nut, rivet or clip',
  motor: 'An electric motor or fan',
  battery: 'A battery or battery holder',
  switch: 'A switch or button',
  housing: 'A plastic or metal housing, bracket, mount or enclosure',
  mechanism: 'Gears, a hinge, a spring or another moving mechanism',
  label: 'A label, marking or printed text',
  other: 'Another kind of part',
};

export interface LookResult {
  usable: number | null;
  problem: string | null;
  part: string | null;
  partConfidence: number | null;
  box: { left: number; top: number; right: number; bottom: number; confidence: number } | null;
  sameArea: number | null;
  looksDamaged: number | null;
  tip: string;
  ms: number;
}

export function lookQuestions(subject: string, withReference: boolean): Record<string, JevQuestion> {
  const about = subject ? { person_is_inspecting: subject } : {};
  return {
    usable: { type: 'noul', instructions: 'Is `frame` sharp and well lit enough to inspect small parts such as connector pins, solder joints or hairline cracks?' },
    problem: { type: 'choice', instructions: 'What most limits `frame` as an inspection photo?', criteria: PROBLEMS },
    part: { type: 'choice', instructions: { ...about, question: 'What kind of part is mainly shown in `frame`?' }, criteria: PARTS },
    focus: { type: 'box', instructions: { ...about, question: subject ? 'the part of `frame` showing what the person is inspecting' : 'the part in `frame` most worth a close look for damage' } },
    ...(withReference && {
      same_area: { type: 'noul', instructions: 'Do `frame` and `reference` show the same part or area of the same kind of product, from a similar side?' },
      looks_damaged: { type: 'noul', instructions: 'Compared with `reference` (known good), does the part in `frame` look broken, cracked, bent, burnt, melted, loose, unplugged or missing? Dirt or a different angle alone is no.' },
    }),
  };
}

/** One short tip from the answers; code decides, so the same answers always give the same words. */
export function tipFor(r: Omit<LookResult, 'tip' | 'ms'>, subject: string): string {
  const what = r.part && r.part !== 'other' ? r.part.replace('_', ' ') : 'part';
  if (r.problem === 'nothing_relevant') return 'I can’t see the product. Point the camera at it.';
  if (r.problem && r.problem !== 'ok' && (r.usable ?? 0) < 0.5) {
    return ({ blurry: 'Hold still, it’s blurry.', too_dark: 'It’s too dark. Add light or turn on the torch.', glare: 'There’s glare. Tilt the phone a little.',
      too_far: `Move closer to the ${subject || what}.`, too_close: 'Back off a little so it’s in focus.' } as Record<string, string>)[r.problem] ?? 'Try another angle.';
  }
  if (r.sameArea !== null && r.sameArea < 0.3) return 'This doesn’t look like the same area as the reference photo.';
  if (r.looksDamaged !== null && r.looksDamaged >= 0.6) return `The ${what} looks different from the reference. Take a close-up so I can compare it properly.`;
  if ((r.usable ?? 0) >= 0.5) return subject ? `Good view of the ${subject}. Take the photo.` : `Clear view of the ${what}.`;
  return '';
}

/** Jev images must be 280-1568 px per side; returns a JPEG inside that range or throws. */
export async function prepareFrame(jpeg: Buffer): Promise<Buffer> {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'lea-look-'));
  try {
    const src = path.join(dir, 'in.jpg'), out = path.join(dir, 'out.jpg');
    await writeFile(src, jpeg);
    const dims = await run('/usr/bin/sips', ['-g', 'pixelWidth', '-g', 'pixelHeight', src]);
    const w = Number(/pixelWidth: (\d+)/.exec(dims)?.[1]), h = Number(/pixelHeight: (\d+)/.exec(dims)?.[1]);
    if (!w || !h) throw new Error('not an image');
    if (Math.min(w, h) < 280) throw new Error('frame is too small; send at least 280 px on each side');
    if (Math.max(w, h) <= 1568) return jpeg;
    await run('/usr/bin/sips', ['-s', 'format', 'jpeg', '-Z', '1568', src, '--out', out]);
    return await readFile(out);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

const p = (a?: JevAnswer) => (typeof a?.noul === 'number' ? a.noul : null);

/** Returns null when Jev did not answer (no key, timeout, error): the caller says so instead of guessing. */
export async function lookAtFrame(key: string, frame: Buffer, subject: string, reference?: Buffer): Promise<LookResult | null> {
  const f = jevImage(await prepareFrame(frame));
  const r = reference ? jevImage(await prepareFrame(reference)) : null;
  const assets: Record<string, JevAsset> = { [f.ref]: f.asset, ...(r && { [r.ref]: r.asset }) };
  // Jev reads the image where its reference string appears; `frame` / `reference` in the questions name these fields.
  const state = { frame: f.ref, ...(r && { reference: r.ref }) };
  const res = await askJev(key, state, lookQuestions(subject, !!r), { assets, timeout: 2000 });
  if (!res) return null;
  const a = res.answers;
  const box = a.focus?.box ? { ...a.focus.box, confidence: a.focus.confidence ?? 0 } : null;
  const base = {
    usable: p(a.usable), problem: a.problem?.choice ?? null, part: a.part?.choice ?? null, partConfidence: a.part?.confidence ?? null,
    box, sameArea: r ? p(a.same_area) : null, looksDamaged: r ? p(a.looks_damaged) : null,
  };
  return { ...base, tip: tipFor(base, subject), ms: res.ms };
}
