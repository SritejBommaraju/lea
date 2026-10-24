// lea-bridge: the only SpacetimeDB client. Serves the phone, runs jobs, talks to Grain.
import os from 'node:os';
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
new JobRunner({ store, assets, workerId: config.workerId }, grain).start();
createApi({ store, assets, auth, grain }).listen(config.port, config.host, () => {
  const lan = Object.values(os.networkInterfaces()).flat().filter(i => i && i.family === 'IPv4' && !i.internal).map(i => i!.address);
  console.log(`[http] listening on ${config.host}:${config.port}; phone URL: ${lan.map(a => `http://${a}:${config.port}`).join(', ')}`);
});
