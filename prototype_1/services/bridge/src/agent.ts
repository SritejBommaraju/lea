// Lea's copilot: one Claude tool loop per spoken turn, with the current camera frame and the session's state.
// Tools act through the same reducers as every other path, so the agent cannot confirm a fault without a check,
// send CAD from an unvalidated scan, or change the design unless the person explicitly asked. Tools that need the
// phone (scan, zoom and photograph, open a screen, whiteboard) come back as client actions for the app to perform.
import Anthropic from '@anthropic-ai/sdk';
import { betaZodTool } from '@anthropic-ai/sdk/helpers/beta/zod';
import { z } from 'zod';
import { config } from './config';
import { featureSpec } from './features';
import { newId, sha256Hex } from './jobs/context';
import { lookAtFrame, prepareFrame } from './look';
import type { AssetStore } from './assets';
import { sessionSnapshot, type StateStore } from './state';
import { routeTranscript } from './voice';

export const AGENT_MODEL = 'claude-opus-5-5';

export type ClientAction =
  | { type: 'scan' }
  | { type: 'closeup'; subject: string; box: { left: number; top: number; right: number; bottom: number } | null }
  | { type: 'screen'; screen: 'capture' | 'repair' | 'design' }
  | { type: 'whiteboard'; title: string; steps: { say: string; svg: string }[] };

export interface AgentTurn { reply: string; actions: ClientAction[] }

const SYSTEM = `You are Lea, a repair copilot talking with someone through their iPhone while they look at a physical product.
Your reply is spoken aloud: one to three short sentences, plain words, no lists, no markdown, no symbols that read badly aloud.
Latency-sensitive; begin your visible answer immediately.

You may see the current camera frame. It is guidance only: say what you see, but a fault is only a candidate until a
check (a measurement or a close-up photo compared with the saved reference) confirms it. Say "might" for candidates and
say plainly when something is unverified. Never state voltages, polarity, part ratings or wire functions from appearance.
Safety: only low-voltage products; continuity and resistance checks are done unplugged with batteries out.

Use tools to act: start a 3D scan, zoom in and photograph a part, open a screen, explain on the whiteboard, add a check,
or send design work to Grain. Only send design work when the person explicitly asks for a design, a fix to be made, or a
change to the CAD. If a tool says something is not possible yet, tell the person what is needed instead.`;

const box = z.object({ left: z.number(), top: z.number(), right: z.number(), bottom: z.number() });

/** Asked for a design in so many words: "make a solution", "design a bracket", "change the CAD", "widen the slot by 2 mm". */
export function asksForDesign(transcript: string): boolean {
  return routeTranscript(transcript).kind === 'design'
    || /\b(make|design|create|build|draft|model)\b.{0,40}\b(solution|fix|replacement|bracket|mount|enclosure|part|cad|design)\b/i.test(transcript)
    || /\b(let'?s|can you|please)\b.{0,20}\b(fix|solve)\b.{0,20}\b(this|it|that)\b.{0,30}\b(cad|grain|design|part|print)\b/i.test(transcript);
}

export async function runAgentTurn(deps: { store: StateStore; assets: AssetStore }, sessionId: string, turnId: string, transcript: string, frameJpeg: Buffer | undefined,
  focusFindingId?: string, client = new Anthropic()): Promise<AgentTurn> {
  const { store } = deps;
  const actions: ClientAction[] = [];
  const frame = frameJpeg ? await prepareFrame(frameJpeg).catch(() => undefined) : undefined;
  const snap = () => sessionSnapshot(store.read(), sessionId)!;

  const tools = [
    betaZodTool({
      name: 'session_state',
      description: 'What is known so far: scan verdicts, findings (with their state), open checks, repairs and design requests.',
      inputSchema: z.object({}),
      run: async () => {
        const s = snap();
        const report = s.meshReports.at(-1);
        return JSON.stringify({
          grainLinked: Boolean(s.session.grainProjectId), referencePinned: Boolean(s.session.referenceMeshAssetId),
          photos: s.assets.filter(a => a.kind.startsWith('photo')).length,
          latestScan: report ? { id: report.id, verdict: report.verdict, failed: report.failedChecks, coverage: report.coverage } : null,
          findings: s.findings.map(f => ({ id: f.id, subject: f.subject, problem: f.problem, state: f.state, source: f.source, confidence: f.confidenceText })),
          openChecks: s.checks.filter(c => c.outcome === 'pending').map(c => ({ id: c.id, findingId: c.findingId, instruction: c.instruction })),
          repairs: s.repairs.map(r => ({ action: r.action, result: r.result })),
          design: s.designCommands.slice(-3).map(c => ({ text: c.text.slice(0, 120), status: c.status })),
        });
      },
    }),
    betaZodTool({
      name: 'locate_in_camera',
      description: 'Find where a part is in the current camera frame (a box as fractions of the frame) and what kind of part it looks like.',
      inputSchema: z.object({ what: z.string().describe('the part to find, in plain words') }),
      run: async ({ what }) => {
        if (!frame) return 'No camera frame with this turn; ask the person to open Capture and point at it.';
        if (!config.jevKey) return 'Part location needs Jev, which is not configured; describe where it is in words instead.';
        const seen = await lookAtFrame(config.jevKey, frame, what);
        return seen?.box ? JSON.stringify({ box: seen.box, part: seen.part, usable: seen.usable }) : 'Could not locate it in this frame.';
      },
    }),
    betaZodTool({
      name: 'start_scan',
      description: 'Open the guided 3D scan ("make a render"). The model must pass its measurement checks before it is used for diagnosis or CAD.',
      inputSchema: z.object({}),
      run: async () => { actions.push({ type: 'scan' }); return 'The scan screen is opening on the phone.'; },
    }),
    betaZodTool({
      name: 'zoom_and_photograph',
      description: 'Zoom the camera onto a part and take a full-resolution close-up. It is compared with the saved reference photo of the same area when one exists, which can add evidence-backed candidates.',
      inputSchema: z.object({
        subject: z.string().describe('the part, in the words a reference photo would use, e.g. "fan connector"'),
        box: box.nullable().describe('where it is in the frame, from locate_in_camera; null to photograph the centre'),
      }),
      run: async ({ subject, box }) => {
        actions.push({ type: 'closeup', subject, box });
        return `The phone will zoom in and photograph the ${subject}. Results arrive as findings in a later turn.`;
      },
    }),
    betaZodTool({
      name: 'open_screen',
      description: 'Show a screen in the app: capture (camera), repair (findings and checks) or design (Grain project and versions).',
      inputSchema: z.object({ screen: z.enum(['capture', 'repair', 'design']) }),
      run: async ({ screen }) => { actions.push({ type: 'screen', screen }); return `Opening ${screen}.`; },
    }),
    betaZodTool({
      name: 'explain_on_whiteboard',
      description: 'Teach visually: 2-6 steps, each one spoken sentence with a simple SVG drawing (viewBox 0 0 800 500, strokes and short labels, no images).',
      inputSchema: z.object({
        title: z.string(),
        steps: z.array(z.object({ say: z.string(), svg: z.string().describe('one <svg viewBox="0 0 800 500">...</svg>') })),
      }),
      run: async ({ title, steps }) => {
        actions.push({ type: 'whiteboard', title, steps: steps.slice(0, 8) });
        return 'The whiteboard is showing; each step is spoken as it is drawn, so keep your reply to one short line.';
      },
    }),
    betaZodTool({
      name: 'add_check',
      description: 'Add a safe check to a finding, done unplugged. Kinds: visual (look for something specific), continuity_should_conduct (a healthy wire or joint), continuity_should_be_open (two points that must not touch).',
      inputSchema: z.object({
        finding_id: z.string(),
        kind: z.enum(['visual', 'continuity_should_conduct', 'continuity_should_be_open']),
        instruction: z.string().describe('one concrete step, naming the two probe points for continuity'),
      }),
      run: async ({ finding_id, kind, instruction }) => {
        const continuity = kind !== 'visual', conducts = kind === 'continuity_should_conduct';
        try {
          await store.call('add_repair_check', {
            id: newId('ck'), sessionId, findingId: finding_id, purpose: 'diagnose', instruction,
            safety: 'Unplug the product and remove batteries before touching it.', powerState: 'off',
            expectedMin: continuity ? (conducts ? 0 : 5) : undefined, expectedMax: continuity && conducts ? 4.99 : undefined,
            expectedUnit: continuity ? 'ohm' : '',
            expectedSource: continuity ? (conducts ? 'healthy connection conducts: under 5 ohm' : 'healthy gap is open: 5 ohm or more') : '',
            net: '',
          });
          actions.push({ type: 'screen', screen: 'repair' });
          return 'Check added; it is on the Repair screen.';
        } catch (e) { return `Could not add the check: ${(e as Error).message}`; }
      },
    }),
    betaZodTool({
      name: 'design_solution',
      description: 'Send a measured design request to Grain for a replacement or fix, built from the validated scan: its measured envelope, fitted planes, holes and cylinders, and the regions that differ from the reference. Typing in Design still works for edits.',
      inputSchema: z.object({
        objective: z.string().describe('what the part must do, in the person\'s words, e.g. "a bracket holding the fan 10 mm above the base"'),
        dimensions: z.array(z.object({ axis: z.enum(['longest', 'middle', 'shortest']), mm: z.number(), tolMm: z.number() }))
          .describe('overall sizes the person stated for the new part, checked on the returned STEP; empty if none were stated'),
      }),
      run: async ({ objective, dimensions }) => {
        if (!asksForDesign(transcript)) return 'Not sent: the person has not asked for a design. Ask whether they want one.';
        const s = snap();
        if (!s.session.grainProjectId) return 'Not sent: no Grain project is linked. Ask them to link one on the Design screen.';
        const report = [...s.meshReports].reverse().find(r => r.verdict === 'pass');
        if (!report) return 'Not sent: there is no scan that passed its measurement checks. Offer to start a scan.';
        try {
          const text = await featureSpec(deps, report, objective);
          await store.call('submit_design_command', {
            id: newId('dc'), sessionId, origin: 'voice', text, meshReportId: report.id,
            idempotencyKey: `agent:${turnId}:solution`, jobId: newId('jb'), inputHash: sha256Hex(text), checksJson: dimensions?.length ? JSON.stringify(dimensions) : '',
          });
          actions.push({ type: 'screen', screen: 'design' });
          return 'Sent to Grain with the measured spec; the Design screen shows it live, and the design is saved before and after.';
        } catch (e) { return `Not sent: ${(e as Error).message}`; }
      },
    }),
    betaZodTool({
      name: 'design_change',
      description: 'Send an explicit change to the current Grain design, e.g. "increase the mounting hole diameter by 2 mm".',
      inputSchema: z.object({ change: z.string() }),
      run: async ({ change }) => {
        if (routeTranscript(transcript).kind !== 'design') return 'Not sent: the change must be explicit (what, and by how much). Ask them.';
        try {
          await store.call('submit_design_command', {
            id: newId('dc'), sessionId, origin: 'voice', text: change, meshReportId: '',
            idempotencyKey: `agent:${turnId}:change`, jobId: newId('jb'), inputHash: sha256Hex(change), checksJson: '',
          });
          actions.push({ type: 'screen', screen: 'design' });
          return 'Sent to Grain.';
        } catch (e) { return `Not sent: ${(e as Error).message}`; }
      },
    }),
  ];

  // The last few spoken turns, as plain text, so "zoom in on that" has its context.
  const history = snap().voiceTurns.slice(-6).flatMap(v => [
    { role: 'user' as const, content: v.transcript },
    { role: 'assistant' as const, content: v.replyText || '(no reply)' },
  ]);
  const content: Anthropic.Beta.BetaContentBlockParam[] = [];
  if (frame) content.push({ type: 'text', text: 'Current camera frame:' }, { type: 'image', source: { type: 'base64', media_type: 'image/jpeg', data: frame.toString('base64') } });
  const focus = focusFindingId ? snap().findings.find(f => f.id === focusFindingId) : undefined;
  if (focus) content.push({ type: 'text', text: `The person selected this candidate on the 3D model: ${focus.subject}, ${focus.problem} (finding ${focus.id}, ${focus.state}). ${focus.detail}` });
  content.push({ type: 'text', text: transcript });

  const final = await client.beta.messages.toolRunner({
    model: AGENT_MODEL,
    max_tokens: 16000,
    max_iterations: 6,
    thread: false,
    betas: ['server-side-fallback-2026-07-01'],
    fallbacks: 'default',
    output_config: { effort: 'low' },
    system: SYSTEM,
    tools,
    messages: [...history, { role: 'user', content }],
  });
  if (final.stop_reason === 'refusal') return { reply: 'I can’t help with that one.', actions: [] };
  const reply = final.content.flatMap(b => (b.type === 'text' ? [b.text] : [])).join(' ').trim();
  return { reply: reply || (actions.length ? 'Okay.' : 'Sorry, I didn’t catch that. Could you say it again?'), actions };
}
