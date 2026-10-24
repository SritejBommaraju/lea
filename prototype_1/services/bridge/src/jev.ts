// TypeSafe Jev: fast typed decisions (noul / choice / box, with probabilities) in front of the big model.
// Ported from VEL (VEL's app/jev.mjs): same endpoint, image assets, keep-alive transport and memo.
// Images: each side 280-1568 px, JPEG or PNG; the API rejects, never resizes. Box questions need jev-preview.
import { randomUUID } from 'node:crypto';
import http from 'node:http';
import https from 'node:https';

const JEV = 'https://api.typesafe.ai/v1/systemone';
const endpoint = () => process.env.LEA_JEV_URL || JEV;   // LEA_JEV_URL: a stand-in Jev, in tests only

export type JevQuestion =
  | { type: 'noul'; instructions: unknown; criteria?: { true?: string; false?: string } }
  | { type: 'choice'; instructions: unknown; criteria: Record<string, string> }
  | { type: 'box'; instructions: unknown };
export type JevAnswer = {
  noul?: number;
  choice?: string; probabilities?: Record<string, number>; confidence?: number;
  box?: { left: number; top: number; right: number; bottom: number };
};
export type JevAsset = { type: 'image'; format: 'jpg' | 'png'; data_base64: string };

export function jevImage(data: Buffer, format: 'jpg' | 'png' = 'jpg') {
  return { ref: `img-${randomUUID()}`, asset: { type: 'image', format, data_base64: data.toString('base64') } as JevAsset };
}

const agents = { 'https:': new https.Agent({ keepAlive: true, maxSockets: 8 }), 'http:': new http.Agent({ keepAlive: true }) };

function request(url: string, body: string, headers: Record<string, string>, timeout: number): Promise<{ status: number; text: string }> {
  const u = new URL(url), lib = u.protocol === 'http:' ? http : https;
  return new Promise((ok, fail) => {
    const req = lib.request(u, { method: 'POST', agent: agents[u.protocol as 'https:'], headers: { ...headers, 'content-length': String(Buffer.byteLength(body)) } }, res => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', d => (data += d));
      res.on('end', () => { clearTimeout(timer); ok({ status: res.statusCode ?? 0, text: data }); });
      res.on('error', e => { clearTimeout(timer); fail(e); });
    });
    const timer = setTimeout(() => req.destroy(new Error('timeout')), timeout);
    req.on('error', e => { clearTimeout(timer); fail(e); });
    req.end(body);
  });
}

export interface JevResult { answers: Record<string, JevAnswer>; ms: number; by: 'jev' }

/** One request. Returns null on no key, timeout, HTTP error or network failure: callers decide what that means. */
export async function askJev(key: string, state: Record<string, unknown>, questions: Record<string, JevQuestion>,
  { assets, timeout = 1500 }: { assets?: Record<string, JevAsset>; timeout?: number } = {}): Promise<JevResult | null> {
  if (!key) return null;
  const t0 = Date.now();
  const box = Object.values(questions).some(q => q.type === 'box');
  const images = !!assets && Object.keys(assets).length > 0;
  try {
    const res = await request(endpoint(), JSON.stringify({ model: box ? 'jev-preview' : 'jev-latest', state, questions, ...(images && { assets }) }), {
      authorization: `Bearer ${key}`, 'content-type': 'application/json', accept: 'application/json',
      ...((images || box) && { 'TypeSafe-Danger-Zone-Images': 'true' }),
    }, timeout);
    if (res.status < 200 || res.status >= 300) return null;
    const answers = JSON.parse(res.text).answers as Record<string, JevAnswer> | undefined;
    return answers ? { answers, ms: Date.now() - t0, by: 'jev' } : null;
  } catch {
    return null;
  }
}
