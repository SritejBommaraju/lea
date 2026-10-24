// Re-hashes every stored asset. Files are named by their SHA-256, so a mismatch means the file changed on disk.
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readdir } from 'node:fs/promises';
import path from 'node:path';
import { config } from '../src/config';

const dir = path.join(config.dataDir, 'assets', 'sha256');
const names = await readdir(dir).catch(() => [] as string[]);
let bad = 0;
for (const name of names) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path.join(dir, name))) hash.update(chunk);
  if (hash.digest('hex') !== name) { bad++; console.error(`changed: ${name}`); }
}
console.log(`${names.length - bad} of ${names.length} assets verified${bad ? `; ${bad} changed on disk` : ''}`);
process.exit(bad ? 1 : 0);
