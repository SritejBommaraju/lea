// Paired-device bearer tokens. The bridge stores only SHA-256 digests; `npm run pair` prints a token once.
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { existsSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';

interface Device { name: string; sha256: string; created: string }

export class DeviceAuth {
  private readonly file: string;
  constructor(dataDir: string) { this.file = path.join(dataDir, 'devices.json'); }

  private load(): Device[] {
    return existsSync(this.file) ? JSON.parse(readFileSync(this.file, 'utf8')) : [];
  }

  pair(name: string): string {
    const token = `lea_${randomBytes(24).toString('hex')}`;
    const devices = this.load();
    devices.push({ name, sha256: digest(token), created: new Date().toISOString() });
    mkdirSync(path.dirname(this.file), { recursive: true });
    writeFileSync(this.file, JSON.stringify(devices, null, 2), { mode: 0o600 });
    return token;
  }

  /** Returns the device name for a valid `Authorization: Bearer lea_...` header, else null. */
  verify(header: string | undefined): string | null {
    const m = /^Bearer (lea_[0-9a-f]{48})$/.exec(header ?? '');
    if (!m) return null;
    const d = Buffer.from(digest(m[1]), 'hex');
    for (const device of this.load()) {
      const known = Buffer.from(device.sha256, 'hex');
      if (known.length === d.length && timingSafeEqual(known, d)) return device.name;
    }
    return null;
  }
}

const digest = (token: string) => createHash('sha256').update(token).digest('hex');
