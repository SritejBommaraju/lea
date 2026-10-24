// Authorizes this bridge's SpacetimeDB identity. Run once, as the database owner (the CLI identity
// that published the module), after the bridge has connected at least once.
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { config } from '../src/config';

const file = path.join(config.dataDir, 'stdb-token.json');
if (!existsSync(file)) throw new Error('start the bridge once first so it creates its identity');
const { identity } = JSON.parse(readFileSync(file, 'utf8'));
execFileSync('spacetime', ['call', '--server', 'local', config.stdbDatabase, 'authorize_bridge', `0x${identity}`, '"lea-bridge"'], { stdio: 'inherit' });
console.log(`authorized bridge identity ${identity}`);
