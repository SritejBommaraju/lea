// Immutable, content-addressed file store on the laptop's disk: <data>/assets/sha256/<digest>.
// A file is visible only after it was fully written, hashed and renamed into place; the database row
// is registered after that, so a row never points at a partial file.
import { createHash, randomUUID } from 'node:crypto';
import { createReadStream, createWriteStream, existsSync } from 'node:fs';
import { mkdir, rename, rm, stat } from 'node:fs/promises';
import path from 'node:path';
import type { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';

export const MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024;
const SHA256 = /^[0-9a-f]{64}$/;

export class AssetError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}

export class AssetStore {
  constructor(readonly root: string) {}

  pathFor(sha256: string): string {
    if (!SHA256.test(sha256)) throw new AssetError(400, 'bad digest');
    return path.join(this.root, 'sha256', sha256);
  }

  has(sha256: string) { return existsSync(this.pathFor(sha256)); }

  /** Streams `body` to staging, verifying size and (if given) the declared digest before publishing. */
  async put(body: Readable, expectedSha256?: string, maxBytes = MAX_UPLOAD_BYTES): Promise<{ sha256: string; bytes: number }> {
    if (expectedSha256 !== undefined && !SHA256.test(expectedSha256)) throw new AssetError(400, 'x-lea-sha256 must be 64 lowercase hex characters');
    const staging = path.join(this.root, 'staging');
    await mkdir(staging, { recursive: true });
    await mkdir(path.join(this.root, 'sha256'), { recursive: true });
    const tmp = path.join(staging, randomUUID());
    const hash = createHash('sha256');
    let bytes = 0;
    try {
      await pipeline(body, async function* (source) {
        for await (const chunk of source) {
          bytes += chunk.length;
          if (bytes > maxBytes) throw new AssetError(413, 'upload too large');
          hash.update(chunk);
          yield chunk;
        }
      }, createWriteStream(tmp, { flags: 'wx' }));
      const sha256 = hash.digest('hex');
      if (bytes === 0) throw new AssetError(400, 'empty upload');
      if (expectedSha256 && sha256 !== expectedSha256) throw new AssetError(422, `content hash ${sha256} does not match declared ${expectedSha256}`);
      const dest = this.pathFor(sha256);
      if (existsSync(dest)) await rm(tmp);
      else await rename(tmp, dest);
      return { sha256, bytes };
    } catch (e) {
      await rm(tmp, { force: true });
      throw e;
    }
  }

  async putFile(file: string, expectedSha256?: string) {
    return this.put(createReadStream(file), expectedSha256);
  }

  async putBuffer(buf: Buffer) {
    const { Readable } = await import('node:stream');
    return this.put(Readable.from([buf]));
  }

  async size(sha256: string) { return (await stat(this.pathFor(sha256))).size; }

  open(sha256: string) { return createReadStream(this.pathFor(sha256)); }
}
