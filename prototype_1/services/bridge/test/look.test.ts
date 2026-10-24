// Live look: frame resizing, the request Lea sends to Jev, the tip rules, and Jev-based intent routing.
// Jev itself is a LOCAL STAND-IN here (no key on this machine): it checks the request shape VEL uses with the real API
// and returns fixed answers, so this proves Lea's plumbing and rules, not Jev's judgement.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { config } from '../src/config';
import { lookAtFrame, prepareFrame, tipFor } from '../src/look';
import { routeTranscriptWithJev } from '../src/voice';

const tmp = mkdtempSync(path.join(os.tmpdir(), 'lea-look-'));
const jpeg = (side: number, name: string) => {
  const file = path.join(tmp, name);
  execFileSync(config.geometryPython, ['-c', `import numpy as np, open3d as o3d; a=(np.random.default_rng(1).random((${side},${Math.round(side * 0.75)},3))*255).astype(np.uint8); o3d.io.write_image(${JSON.stringify(file)}, o3d.geometry.Image(a))`]);
  return readFileSync(file);
};

let answers: Record<string, unknown> = {};
const seen: any[] = [];
const jev = http.createServer((req, res) => {
  let body = '';
  req.on('data', d => (body += d));
  req.on('end', () => {
    const r = JSON.parse(body);
    seen.push({ headers: req.headers, body: r });
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ answers: Object.fromEntries(Object.keys(r.questions).map(k => [k, answers[k]]).filter(([, v]) => v)) }));
  });
});

beforeAll(async () => {
  await new Promise<void>(r => jev.listen(0, '127.0.0.1', () => r()));
  process.env.LEA_JEV_URL = `http://127.0.0.1:${(jev.address() as AddressInfo).port}/v1/systemone`;
});
afterAll(() => { jev.close(); delete process.env.LEA_JEV_URL; });

describe('frames', () => {
  it('keeps frames inside Jev limits: shrinks large ones, refuses tiny ones', async () => {
    const big = await prepareFrame(jpeg(2400, 'big.jpg'));
    writeFileSync(path.join(tmp, 'big-out.jpg'), big);
    const dims = execFileSync('/usr/bin/sips', ['-g', 'pixelWidth', '-g', 'pixelHeight', path.join(tmp, 'big-out.jpg')]).toString();
    expect(Number(/pixelWidth: (\d+)/.exec(dims)![1])).toBeLessThanOrEqual(1568);
    await expect(prepareFrame(jpeg(200, 'tiny.jpg'))).rejects.toThrow(/too small/);
  });
});

describe('look', () => {
  it('sends the frame and reference as assets named in the state, with box questions on jev-preview', async () => {
    answers = {
      usable: { noul: 0.91 }, problem: { choice: 'ok', confidence: 0.8 }, part: { choice: 'connector', confidence: 0.7 },
      focus: { box: { left: 0.2, top: 0.3, right: 0.5, bottom: 0.6 }, confidence: 0.65 }, same_area: { noul: 0.88 }, looks_damaged: { noul: 0.74 },
    };
    const r = await lookAtFrame('test-key', jpeg(1000, 'f.jpg'), 'fan connector', jpeg(1000, 'r.jpg'));
    const req = seen.at(-1);
    expect(req.headers.authorization).toBe('Bearer test-key');
    expect(req.headers['typesafe-danger-zone-images']).toBe('true');
    expect(req.body.model).toBe('jev-preview');
    expect(Object.keys(req.body.assets)).toEqual([req.body.state.frame, req.body.state.reference]);
    expect(req.body.assets[req.body.state.frame].type).toBe('image');
    expect(r!.box!.confidence).toBe(0.65);
    expect(r!.tip).toBe('The connector looks different from the reference. Take a close-up so I can compare it properly.');
  });

  it('coaches on the photo before judging the part', async () => {
    answers = { usable: { noul: 0.2 }, problem: { choice: 'glare' }, part: { choice: 'circuit_board' } };
    const r = await lookAtFrame('test-key', jpeg(800, 'g.jpg'), '');
    expect(r!.tip).toBe('There’s glare. Tilt the phone a little.');
    expect(r!.looksDamaged).toBeNull();   // no reference: no damage question asked
    expect(Object.keys(seen.at(-1).body.questions)).not.toContain('looks_damaged');
  });

  it('says nothing is decided when Jev does not answer', async () => {
    process.env.LEA_JEV_URL = 'http://127.0.0.1:9/v1/systemone';
    expect(await lookAtFrame('test-key', jpeg(800, 'h.jpg'), '')).toBeNull();
    process.env.LEA_JEV_URL = `http://127.0.0.1:${(jev.address() as AddressInfo).port}/v1/systemone`;
  });

  it('tip rules', () => {
    const base = { usable: 0.9, problem: 'ok', part: 'wire', partConfidence: 0.9, box: null, sameArea: 0.1, looksDamaged: 0.9 };
    expect(tipFor(base, '')).toMatch(/same area/);   // a mismatched reference outranks a damage call
    expect(tipFor({ ...base, problem: 'nothing_relevant' }, '')).toMatch(/can’t see the product/);
  });
});

describe('intent routing (VEL route.mjs pattern)', () => {
  it('uses Jev when it answers and patterns when it does not; never sends a vague design change', async () => {
    answers = { intent: { choice: 'design_change', confidence: 0.9 } };
    expect((await routeTranscriptWithJev('k', 'make it bigger')).kind).toBe('clarify');
    expect(await routeTranscriptWithJev('k', 'widen the slot by 2 mm')).toMatchObject({ kind: 'design', by: 'jev' });
    answers = { intent: { choice: 'capture_help', confidence: 0.8 } };
    expect((await routeTranscriptWithJev('k', 'is this photo good enough')).kind).toBe('clarify');
    expect(await routeTranscriptWithJev('', 'what did you find')).toMatchObject({ kind: 'status', by: 'patterns' });
  });
});
