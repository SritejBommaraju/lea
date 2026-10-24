// Copies the bridge's data (assets, device tokens, its database identity) and SpacetimeDB's data together, so a
// restore never has rows without files or files without rows. Stop `spacetime start` first for a consistent copy.
// Usage: npm run backup -- <destination folder>   (LEA_STDB_DATA_DIR overrides SpacetimeDB's data folder)
import { cp, mkdir, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { config } from '../src/config';

const dest = process.argv[2];
if (!dest) { console.error('usage: npm run backup -- <destination folder>'); process.exit(64); }
const stdbData = process.env.LEA_STDB_DATA_DIR ?? path.join(os.homedir(), '.local/share/spacetime/data');
const out = path.join(dest, `lea-backup-${new Date().toISOString().replace(/[:.]/g, '-')}`);
await mkdir(out, { recursive: true });
await cp(config.dataDir, path.join(out, 'bridge-data'), { recursive: true });
if (existsSync(stdbData)) await cp(stdbData, path.join(out, 'spacetimedb-data'), { recursive: true });
else console.warn(`SpacetimeDB data folder not found at ${stdbData}; set LEA_STDB_DATA_DIR`);
await writeFile(path.join(out, 'manifest.json'), JSON.stringify({ takenAt: new Date().toISOString(), bridgeData: config.dataDir, stdbData, database: config.stdbDatabase }, null, 2));
console.log(`backed up to ${out}`);
