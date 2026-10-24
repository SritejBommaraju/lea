// Voice: Cartesia access tokens for the phone, and what Lea does with a final transcript.
// Speech-to-text and text-to-speech stream directly between the phone and Cartesia with a token
// minted here; the Cartesia API key never leaves the laptop.
import { config } from './config';
import { askJev } from './jev';
import type { Tables } from './state';

export class VoiceUnavailable extends Error {}

export async function mintCartesiaToken(seconds = 300): Promise<{ token: string; expiresInSeconds: number; version: string; voiceId: string }> {
  if (!config.cartesiaKey) throw new VoiceUnavailable('CARTESIA_API_KEY is not set on the bridge');
  if (!config.cartesiaVoiceId) throw new VoiceUnavailable('CARTESIA_VOICE_ID is not set on the bridge');
  const res = await fetch('https://api.cartesia.ai/access-token', {
    method: 'POST',
    headers: { 'Cartesia-Version': config.cartesiaVersion, 'Content-Type': 'application/json', Authorization: `Bearer ${config.cartesiaKey}` },
    body: JSON.stringify({ grants: { stt: true, tts: true }, expires_in: seconds }),
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new VoiceUnavailable(`Cartesia refused the token request: ${res.status} ${await res.text()}`);
  const body = (await res.json()) as { token: string; expires_in?: number };
  return { token: body.token, expiresInSeconds: body.expires_in ?? seconds, version: config.cartesiaVersion, voiceId: config.cartesiaVoiceId };
}

export type Intent =
  | { kind: 'design'; text: string }
  | { kind: 'status' }
  | { kind: 'clarify'; question: string };

const DESIGN_VERB = /\b(make|change|increase|decrease|reduce|widen|narrow|enlarge|shrink|add|remove|move|set|thicken|thin|lengthen|shorten|round|chamfer|fillet|design|create|redesign)\b/i;
const CAD_NOUN = /\b(hole|holes|slot|bracket|mount|enclosure|wall|walls|lid|tab|boss|fillet|chamfer|clearance|thickness|width|height|depth|diameter|radius|screw|standoff|cad|model|part|design)\b/i;
const STATUS = /\b(what did you find|what's wrong|status|findings|summary|what next|next step|did it work)\b/i;

/** Final transcripts only. A design change is sent only when the instruction is explicit; anything
 *  ambiguous gets a question back, never a guessed edit. */
export function routeTranscript(transcript: string): Intent {
  const text = transcript.replace(/^\s*(hey|hi|ok)?\s*,?\s*(leah|lea)\b[\s,.:]*/i, '').trim();
  if (!text) return { kind: 'clarify', question: 'Yes? What would you like me to do?' };
  if (STATUS.test(text)) return { kind: 'status' };
  if (DESIGN_VERB.test(text) && CAD_NOUN.test(text)) {
    if (/\b(it|that|this)\b/i.test(text) && !CAD_NOUN.test(text.replace(/\b(it|that|this)\b/gi, ''))) {
      return { kind: 'clarify', question: 'Which part of the design should I change?' };
    }
    return { kind: 'design', text };
  }
  return { kind: 'clarify', question: 'I can report findings, guide a check, or send a design change to Grain. Which do you want?' };
}

const INTENTS = {
  design_change: 'Asks to change, add to or make a part of the CAD design (a size, hole, slot, bracket, wall)',
  status: 'Asks what was found, how the repair is going, or what to do next',
  capture_help: 'Asks how to photograph or scan the object, or whether a photo is good',
  check_result: 'Reports what they measured or saw during a check ("it reads 4.9 volts", "the clip is cracked")',
  other: 'Anything else: chit-chat, thanks, a general question',
};

/** Like VEL's route.mjs: Jev answers what the person meant, patterns decide when Jev does not answer.
 *  Jev never sends a design change on its own: the words must also be an explicit CAD instruction. */
export async function routeTranscriptWithJev(key: string, transcript: string): Promise<Intent & { by: 'jev' | 'patterns' }> {
  const byPatterns = routeTranscript(transcript);
  if (!key) return { ...byPatterns, by: 'patterns' };
  const res = await askJev(key, { person_said: transcript }, {
    intent: { type: 'choice', instructions: 'What does `person_said` ask a repair assistant to do?', criteria: INTENTS },
  }, { timeout: 900 });
  const choice = res?.answers.intent?.choice, confidence = res?.answers.intent?.confidence ?? 0;
  if (!choice || confidence < 0.5) return { ...byPatterns, by: 'patterns' };
  if (choice === 'design_change') {
    return byPatterns.kind === 'design' ? { ...byPatterns, by: 'jev' } : { kind: 'clarify', question: 'Which part of the design should I change, and by how much?', by: 'jev' };
  }
  if (choice === 'status') return { kind: 'status', by: 'jev' };
  if (choice === 'capture_help') return { kind: 'clarify', question: 'Turn on Coach in Capture and I will tell you when the view is sharp and close enough.', by: 'jev' };
  if (choice === 'check_result') return { kind: 'clarify', question: 'Record that on the check in Repair, with the reading or a photo, so I can judge it.', by: 'jev' };
  return { ...byPatterns, by: 'patterns' };
}

/** A short spoken summary of where the session stands; uncertain findings are said to be unverified. */
export function statusReply(t: Tables, sessionId: string): string {
  const findings = t.findings.filter(f => f.sessionId === sessionId);
  const reports = t.meshReports.filter(r => r.sessionId === sessionId).sort((a, b) => Number(b.createdAt.microsSinceUnixEpoch - a.createdAt.microsSinceUnixEpoch));
  const parts: string[] = [];
  if (reports[0]) {
    parts.push(reports[0].verdict === 'pass'
      ? `The latest scan passed its scale, coverage and fit checks.`
      : `The latest scan failed ${reports[0].failedChecks.join(', ').replace(/_/g, ' ')}, so I am not using its geometry.`);
  }
  const by = (s: string) => findings.filter(f => f.state === s);
  if (by('confirmed').length) parts.push(`Confirmed by a check: ${by('confirmed').map(f => `${f.subject}, ${f.problem}`).join('; ')}.`);
  if (by('candidate').length) parts.push(`${by('candidate').length} unverified candidate${by('candidate').length > 1 ? 's' : ''} still need a check, starting with ${by('candidate')[0].subject}.`);
  if (by('unverified').length) parts.push(`${by('unverified').length} could not be verified.`);
  const repairs = t.repairs.filter(r => r.sessionId === sessionId);
  const last = repairs[repairs.length - 1];
  if (last) parts.push(last.result === 'fixed_verified' ? 'The last repair was verified as working.' : last.result === 'not_fixed' ? 'The last repair did not fix it.' : 'The last repair is not verified yet.');
  return parts.join(' ') || 'There are no findings yet. Capture the object, then take close-ups of anything that looks wrong.';
}
