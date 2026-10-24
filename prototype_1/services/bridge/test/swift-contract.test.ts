// Runs the iOS app's own BridgeClient (compiled for macOS in ../contract-swift) against this bridge's
// HTTP API, backed by the module's reducers in SpacetimeDB's wasm datastore and the real local Grain.
import { execFile, execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync } from 'node:fs';
import type { AddressInfo } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { expect, it } from 'vitest';
import { AssetStore } from '../src/assets';
import { DeviceAuth } from '../src/auth';
import { config } from '../src/config';
import { GrainClient } from '../src/grain';
import { createApi } from '../src/http';
import { HarnessStore } from './harness-store';
import http from 'node:http';

const binary = path.resolve('../contract-swift/swift-contract');

it.skipIf(!existsSync(binary))('the Swift client decodes every bridge response it uses', async () => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), 'lea-swift-'));
  // STAND-IN Jev (no key here): fixed answers, so this checks the Swift side decodes the bridge's look result.
  const jev = http.createServer((req, res) => { req.resume(); req.on('end', () => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ answers: {
    usable: { noul: 0.9 }, problem: { choice: 'ok' }, part: { choice: 'connector', confidence: 0.8 }, focus: { box: { left: 0.2, top: 0.2, right: 0.6, bottom: 0.5 }, confidence: 0.65 } } })); }); });
  await new Promise<void>(r => jev.listen(0, '127.0.0.1', () => r()));
  process.env.LEA_JEV_URL = `http://127.0.0.1:${(jev.address() as AddressInfo).port}/v1/systemone`;
  config.jevKey = 'stand-in';
  const frame = path.join(tmp, 'frame.jpg');
  execFileSync(config.geometryPython, ['-c', `import numpy as np, open3d as o3d; o3d.io.write_image(${JSON.stringify(frame)}, o3d.geometry.Image((np.random.default_rng(2).random((900,700,3))*255).astype(np.uint8)))`]);
  const auth = new DeviceAuth(tmp);
  const token = auth.pair('swift contract');
  const server = createApi({ store: new HarnessStore(), assets: new AssetStore(path.join(tmp, 'assets')), auth, grain: new GrainClient(config.grainUrl, config.grainKey) });
  await new Promise<void>(r => server.listen(0, '127.0.0.1', () => r()));
  try {
    const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    const { stdout } = await promisify(execFile)(binary, [], { env: { ...process.env, LEA_URL: url, LEA_TOKEN: token, LEA_SESSION: 'se_swift_contract', LEA_FRAME: frame } }).catch(e => ({ stdout: `${e.stdout}\n${e.stderr}` }));
    if (process.env.LEA_CONTRACT_LOG) (await import('node:fs')).writeFileSync(process.env.LEA_CONTRACT_LOG, stdout);
    expect(stdout).toContain('ALL PASS');
  } finally {
    server.close();
    jev.close();
    config.jevKey = '';
    delete process.env.LEA_JEV_URL;
  }
});
