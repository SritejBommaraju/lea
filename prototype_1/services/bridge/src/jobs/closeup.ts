// closeup_compare: a close-up photo against the saved reference photo of the same area, judged by
// Claude. The model describes and proposes; code records only candidates with evidence, and only
// lets it propose power-off visual or continuity checks. Voltage checks need a sourced range from a person.
import Anthropic from '@anthropic-ai/sdk';
import { betaZodOutputFormat } from '@anthropic-ai/sdk/helpers/beta/zod';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { z } from 'zod';
import type { AssetRow } from '../state';
import { type JobDeps, Lease, newId, run } from './context';

export const MODEL = 'claude-opus-5-5';

export interface CloseupInput { closeupAssetId: string; referencePhotoAssetId: string; subject: string }

const Candidate = z.object({
  part: z.string().describe('the part or area, in plain words, as visible in the close-up'),
  observation: z.enum(['looks_same_as_reference', 'differs_possible_damage', 'cannot_tell']),
  problem: z.string().describe('what differs, e.g. "crack across the mounting tab"; empty if it looks the same'),
  evidence: z.string().describe('what in the two photos shows this, specific enough for a person to find it'),
  confidence: z.enum(['low', 'medium', 'high']),
  check: z.object({
    kind: z.enum(['visual_power_off', 'continuity_should_conduct', 'continuity_should_be_open'])
      .describe('for continuity: what a healthy part reads (an intact wire conducts; two pins that must not touch are open)'),
    instruction: z.string().describe('one safe, concrete step a person can do to confirm or rule this out, naming the two probe points for continuity'),
  }).nullable(),
});
const Comparison = z.object({
  same_object_and_area: z.boolean(),
  image_quality_problem: z.string().describe('blur, glare, framing or lighting that limits the judgement; empty if none'),
  candidates: z.array(Candidate),
});

const SYSTEM = `You compare a close-up photo of a physical product with a saved reference photo of the same area in good condition.
Report only differences you can actually see. A difference is a candidate for a person to check, never a confirmed fault.
If glare, blur, framing or occlusion stops you judging an area, say "cannot_tell" for it rather than guessing.
Never state electrical values, polarity, part ratings or wire functions from appearance alone.
Proposed checks must be safe with the product unpowered and unplugged: a visual inspection or a continuity test.`;

async function jpegForModel(deps: JobDeps, a: AssetRow, dir: string) {
  // Claude reads images up to ~1568 px on the long side well; the original stays untouched.
  const out = path.join(dir, `${a.id}.jpg`);
  await run('/usr/bin/sips', ['-s', 'format', 'jpeg', '-Z', '1568', deps.assets.pathFor(a.sha256), '--out', out]);
  return (await readFile(out)).toString('base64');
}

export async function runCloseupCompare(deps: JobDeps, lease: Lease, client = new Anthropic()) {
  const input = JSON.parse(lease.job.inputJson) as CloseupInput;
  const t = deps.store.read();
  const closeup = t.assets.find(a => a.id === input.closeupAssetId)!;
  const reference = t.assets.find(a => a.id === input.referencePhotoAssetId)!;
  const dir = await mkdtemp(path.join(os.tmpdir(), 'lea-closeup-'));
  let parsed: z.infer<typeof Comparison>;
  try {
    await lease.progress(`asking ${MODEL}`);
    const [ref64, close64] = await Promise.all([jpegForModel(deps, reference, dir), jpegForModel(deps, closeup, dir)]);
    const response = await client.beta.messages.parse({
      model: MODEL,
      max_tokens: 16000,
      betas: ['server-side-fallback-2026-07-01'],
      fallbacks: 'default',
      output_config: { effort: 'high', format: betaZodOutputFormat(Comparison) },
      system: SYSTEM,
      messages: [{
        role: 'user',
        content: [
          { type: 'text', text: 'Reference photo (saved, known good):' },
          { type: 'image', source: { type: 'base64', media_type: 'image/jpeg', data: ref64 } },
          { type: 'text', text: 'Close-up photo taken now:' },
          { type: 'image', source: { type: 'base64', media_type: 'image/jpeg', data: close64 } },
          { type: 'text', text: `Area being inspected: ${input.subject || 'not named'}. Compare them.` },
        ],
      }],
    });
    if (response.stop_reason === 'refusal') return lease.finish('needs_user', `${MODEL} declined to compare these photos`);
    if (response.stop_reason === 'max_tokens') return lease.finish('uncertain', 'the comparison was cut off before it finished');
    if (!response.parsed_output) return lease.finish('uncertain', 'the model output did not match the comparison schema');
    parsed = response.parsed_output;
  } catch (e) {
    if (e instanceof Anthropic.AuthenticationError) return lease.finish('needs_user', 'Anthropic credentials are missing or invalid on the bridge');
    if (e instanceof Anthropic.RateLimitError) return lease.finish('failed', 'Anthropic rate limit; try again shortly');
    if (e instanceof Anthropic.APIError) return lease.finish('failed', `Anthropic API error ${e.status}: ${e.message}`);
    throw e;
  } finally {
    await rm(dir, { recursive: true, force: true });
  }

  if (!parsed.same_object_and_area) {
    return lease.finish('needs_user', 'the close-up does not show the same object or area as the reference; retake it', parsed);
  }
  let recorded = 0;
  for (const c of parsed.candidates) {
    if (c.observation === 'looks_same_as_reference') continue;
    const findingId = newId('fd');
    await deps.store.call('record_finding', {
      id: findingId, sessionId: lease.job.sessionId, subject: c.part,
      problem: c.observation === 'cannot_tell' ? 'could not be judged from this photo' : c.problem,
      source: 'vision', evidenceAssetIds: [closeup.id], referenceAssetId: reference.id, meshReportId: '', regionJson: '{}',
      confidenceKind: 'model_word', confidenceText: `${c.confidence} (${MODEL})`,
      detail: [c.evidence, parsed.image_quality_problem && `Photo limits: ${parsed.image_quality_problem}`].filter(Boolean).join('\n'),
    });
    recorded++;
    if (c.check) {
      // A healthy reading is "as expected" and rules the fault out, so the range must say what healthy is:
      // under 5 ohm conducts; 5 ohm or more is open.
      const continuity = c.check.kind !== 'visual_power_off';
      const conducts = c.check.kind === 'continuity_should_conduct';
      await deps.store.call('add_repair_check', {
        id: newId('ck'), sessionId: lease.job.sessionId, findingId, purpose: 'diagnose', instruction: c.check.instruction,
        safety: 'Unplug the product and remove batteries before touching it.', powerState: 'off',
        expectedMin: continuity ? (conducts ? 0 : 5) : undefined,
        expectedMax: continuity && conducts ? 4.99 : undefined,
        expectedUnit: continuity ? 'ohm' : '',
        expectedSource: continuity ? (conducts ? 'healthy connection conducts: under 5 ohm' : 'healthy gap is open: 5 ohm or more') : '',
        net: '',
      });
    }
  }
  return lease.finish('succeeded', '', { candidates: recorded, imageQuality: parsed.image_quality_problem, model: MODEL });
}
