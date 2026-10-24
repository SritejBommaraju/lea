// Shared plumbing for jobs: leases, child processes, asset registration.
import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import type { AssetStore } from '../assets';
import type { AssetRow, JobRow, StateStore } from '../state';

export interface JobDeps {
  store: StateStore;
  assets: AssetStore;
  workerId: string;
}

export const newId = (prefix: string) => `${prefix}_${randomUUID().replace(/-/g, '').slice(0, 24)}`;
export const sha256Hex = (data: string | Buffer) => createHash('sha256').update(data).digest('hex');

/** A claimed job. Every write echoes the attempt, so a worker whose lease was taken over is fenced off. */
export class Lease {
  private timer: NodeJS.Timeout | undefined;
  private lastProgress = 'claimed';

  constructor(readonly deps: JobDeps, readonly job: JobRow, readonly attempt: number, private readonly leaseSeconds: number) {
    this.timer = setInterval(() => void this.progress(this.lastProgress).catch(() => {}), (leaseSeconds * 1000) / 3);
  }

  static async claim(deps: JobDeps, job: JobRow, leaseSeconds: number): Promise<Lease | null> {
    try {
      await deps.store.call('claim_job', { jobId: job.id, worker: deps.workerId, leaseSeconds });
    } catch {
      return null;  // another worker has it, or it is no longer queued
    }
    const fresh = deps.store.read().jobs.find(j => j.id === job.id)!;
    return new Lease(deps, fresh, fresh.attempt, leaseSeconds);
  }

  progress(text: string) {
    this.lastProgress = text;
    return this.deps.store.call('report_progress', { jobId: this.job.id, attempt: this.attempt, progress: text, leaseSeconds: this.leaseSeconds });
  }

  async finish(status: 'failed' | 'needs_user' | 'uncertain' | 'succeeded', error = '', result: unknown = {}) {
    this.release();
    await this.deps.store.call('finish_job', { jobId: this.job.id, attempt: this.attempt, status, resultJson: JSON.stringify(result), error });
  }

  release() {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
  }

  async registerFile(kind: string, file: string, mediaType: string, sourceAssetIds: string[], meta: object = {}): Promise<AssetRow> {
    const { sha256, bytes } = await this.deps.assets.putFile(file);
    return this.register(kind, sha256, bytes, mediaType, sourceAssetIds, meta);
  }

  async registerBuffer(kind: string, buf: Buffer, mediaType: string, sourceAssetIds: string[], meta: object = {}, id?: string): Promise<AssetRow> {
    const { sha256, bytes } = await this.deps.assets.putBuffer(buf);
    return this.register(kind, sha256, bytes, mediaType, sourceAssetIds, meta, id);
  }

  private async register(kind: string, sha256: string, bytes: number, mediaType: string, sourceAssetIds: string[], meta: object, id = newId('as')) {
    await this.deps.store.call('register_asset', {
      id, sessionId: this.job.sessionId, kind, sha256, bytes: BigInt(bytes), mediaType, sourceAssetIds, metaJson: JSON.stringify(meta),
    });
    return this.deps.store.read().assets.find(a => a.id === id)!;
  }
}

/** Runs a command, streaming stdout lines to `onLine`; resolves with collected stdout or rejects with stderr. */
export function run(cmd: string, args: string[], onLine?: (line: string) => void, timeoutMs = 4 * 3600_000, cwd?: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'], cwd });
    let out = '', err = '', partial = '';
    const timer = setTimeout(() => child.kill('SIGTERM'), timeoutMs);
    child.stdout.on('data', (b: Buffer) => {
      out += b;
      partial += b;
      const lines = partial.split('\n');
      partial = lines.pop()!;
      for (const l of lines) if (l.trim()) onLine?.(l);
    });
    child.stderr.on('data', (b: Buffer) => { err = (err + b).slice(-8000); });
    child.on('error', reject);
    child.on('close', code => {
      clearTimeout(timer);
      if (partial.trim()) onLine?.(partial);
      code === 0 ? resolve(out) : reject(new Error(`${cmd} exited ${code}: ${err.trim().split('\n').slice(-5).join(' | ') || out.slice(-500)}`));
    });
  });
}

export function lastJsonLine(out: string): any {
  const lines = out.trim().split('\n').filter(l => l.startsWith('{'));
  return JSON.parse(lines[lines.length - 1]);
}
