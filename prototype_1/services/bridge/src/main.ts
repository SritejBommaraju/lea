// lea-bridge: the only SpacetimeDB client. Serves the phone, runs jobs, talks to Grain.
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import { Timestamp } from 'spacetimedb';
import path from 'node:path';
import { AssetStore } from './assets';
import { DeviceAuth } from './auth';
import { config } from './config';
import { GrainClient } from './grain';
import { createApi } from './http';
import { JobRunner } from './jobs/runner';
import { SpacetimeStore } from './stdb';

const store = await SpacetimeStore.connect();
console.log(`[stdb] connected to ${config.stdbUri}/${config.stdbDatabase} as ${store.identityHex}`);
if (store.read().sessions.length === 0) console.log('[stdb] no sessions visible; if this is a fresh bridge identity, run `npm run authorize`');

const assets = new AssetStore(path.join(config.dataDir, 'assets'));
const grain = new GrainClient(config.grainUrl, config.grainKey);
const auth = new DeviceAuth(config.dataDir);
// Evidence is stored unencrypted by the bridge; disk encryption is what protects it at rest.
try {
  if (!/FileVault is On/.test(execFileSync('/usr/bin/fdesetup', ['status']).toString())) console.warn('[security] FileVault is off: stored photos and scans are not encrypted at rest');
} catch { /* not macOS */ }
const cleaned = await assets.cleanStaging();
// Keep the event log bounded: replay after a longer gap refetches the whole snapshot anyway.
const trim = () => store.call('trim_events', { before: new Timestamp(BigInt(Date.now() - 7 * 86_400_000) * 1000n) }).catch(e => console.warn('[stdb] trim_events:', e.message));
void trim();
setInterval(trim, 6 * 3_600_000);
if (cleaned) console.log(`[assets] removed ${cleaned} partial upload(s) left by an earlier crash`);
new JobRunner({ store, assets, workerId: config.workerId }, grain).start();
createApi({ store, assets, auth, grain }).listen(config.port, config.host, () => {
  const lan = Object.values(os.networkInterfaces()).flat().filter(i => i && i.family === 'IPv4' && !i.internal).map(i => i!.address);
  console.log(`[http] listening on ${config.host}:${config.port}; phone URL: ${lan.map(a => `http://${a}:${config.port}`).join(', ')}`);
});
