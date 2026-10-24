// Claims queued jobs and runs them outside any database transaction. One job per kind at a time, and
// design turns serialized per Grain project (Grain allows one chat turn per project).
import type { GrainClient } from '../grain';
import type { JobRow } from '../state';
import { runCloseupCompare } from './closeup';
import { type JobDeps, Lease } from './context';
import { runDesignTurn } from './design';
import { runReconstruction } from './reconstruction';

const LEASE_SECONDS: Record<string, number> = { reconstruction: 300, design_turn: 300, closeup_compare: 180 };

export class JobRunner {
  private readonly busyKinds = new Set<string>();
  private readonly busyGrainProjects = new Set<string>();
  private timer: NodeJS.Timeout | undefined;

  constructor(private readonly deps: JobDeps, private readonly grain: GrainClient, private readonly log = console) {}

  start(intervalMs = 2000) {
    this.timer = setInterval(() => this.tick(), intervalMs);
    this.deps.store.onEvent(e => { if (e.kind === 'job_queued') this.tick(); });
  }

  stop() { if (this.timer) clearInterval(this.timer); }

  tick() {
    if (!this.deps.store.connected) return;
    const now = BigInt(Date.now()) * 1000n;
    for (const job of this.deps.store.read().jobs) {
      const expired = job.status === 'running' && job.leaseExpiresAt.microsSinceUnixEpoch < now;
      if ((job.status === 'queued' || expired) && !this.busyKinds.has(job.kind)) void this.runOne(job);
    }
  }

  private async runOne(job: JobRow) {
    let grainProject = '';
    if (job.kind === 'design_turn') {
      grainProject = this.commandFor(job)?.grainProjectId ?? '';
      if (this.busyGrainProjects.has(grainProject)) return;
      this.busyGrainProjects.add(grainProject);
    } else {
      this.busyKinds.add(job.kind);
    }
    try {
      const lease = await Lease.claim(this.deps, job, LEASE_SECONDS[job.kind] ?? 120);
      if (!lease) return;
      this.log.info(`[job] ${job.kind} ${job.id} attempt ${lease.attempt}`);
      try {
        await this.dispatch(lease);
      } catch (e) {
        this.log.error(`[job] ${job.id} failed:`, e);
        await lease.finish('failed', (e as Error).message).catch(() => {});
      } finally {
        lease.release();
      }
    } finally {
      this.busyKinds.delete(job.kind);
      this.busyGrainProjects.delete(grainProject);
    }
  }

  private async dispatch(lease: Lease) {
    switch (lease.job.kind) {
      case 'reconstruction':
        return runReconstruction(this.deps, lease);
      case 'closeup_compare':
        return runCloseupCompare(this.deps, lease);
      case 'design_turn': {
        const cmd = this.commandFor(lease.job);
        if (!cmd) return lease.finish('failed', 'no design command for this job');
        await runDesignTurn(this.deps, this.grain, lease, cmd);
        return;
      }
      default:
        return lease.finish('failed', `no worker for ${lease.job.kind}`);
    }
  }

  private commandFor(job: JobRow) {
    return this.deps.store.read().designCommands.find(c => c.jobId === job.id);
  }
}
