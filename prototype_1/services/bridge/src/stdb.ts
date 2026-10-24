// StateStore backed by a live SpacetimeDB through the official TypeScript SDK and the bindings that
// `npm run generate` (spacetime generate) writes to src/module_bindings. The bridge keeps its identity
// token on disk; an admin authorizes that identity once with `npm run authorize`.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { config } from './config';
import { type EventRow, ReducerError, type ReducerArgs, type ReducerName, type StateStore, type Tables } from './state';

const VIEWS: Record<keyof Tables, string> = {
  sessions: 'bridge_sessions', assets: 'bridge_assets', policies: 'bridge_policies', jobs: 'bridge_jobs',
  meshReports: 'bridge_mesh_reports', findings: 'bridge_findings', checks: 'bridge_checks', repairs: 'bridge_repairs',
  voiceTurns: 'bridge_voice_turns', designCommands: 'bridge_design_commands', designVersions: 'bridge_design_versions', events: 'bridge_events',
};
const camel = (s: string) => s.replace(/_(\w)/g, (_, c: string) => c.toUpperCase());

export class SpacetimeStore implements StateStore {
  connected = false;
  identityHex = '';
  private conn: any;
  private readonly listeners = new Set<(e: EventRow) => void>();
  private readonly tokenFile = path.join(config.dataDir, 'stdb-token.json');

  static async connect(): Promise<SpacetimeStore> {
    const s = new SpacetimeStore();
    await s.open();
    return s;
  }

  private async open() {
    const bindings = './module_bindings/index';
    const { DbConnection } = await import(bindings).catch(() => {
      throw new Error('SpacetimeDB bindings are missing: run `npm run generate` after installing the spacetime CLI');
    });
    const saved = existsSync(this.tokenFile) ? JSON.parse(readFileSync(this.tokenFile, 'utf8')).token : undefined;
    await new Promise<void>((resolve, reject) => {
      let builder = DbConnection.builder()
        .withUri(config.stdbUri)
        .withDatabaseName(config.stdbDatabase)
        .withConfirmedReads(true)  // acknowledge the phone only after durable commit
        .withAutomaticReconnect()
        .onConnect((conn: any, identity: any, token: string) => {
          this.identityHex = identity.toHexString();
          mkdirSync(config.dataDir, { recursive: true });
          writeFileSync(this.tokenFile, JSON.stringify({ identity: this.identityHex, token }), { mode: 0o600 });
          conn.db[camel(VIEWS.events)].onInsert((_ctx: unknown, row: EventRow) => this.listeners.forEach(l => l(row)));
          conn.subscriptionBuilder()
            .onApplied(() => { this.connected = true; resolve(); })
            .onError((_ctx: unknown, err: unknown) => reject(err))
            .subscribe(Object.values(VIEWS).map(v => `SELECT * FROM ${v}`));
        })
        .onAutomaticReconnect((_conn: any, _identity: any, token: string) => {
          writeFileSync(this.tokenFile, JSON.stringify({ identity: this.identityHex, token }), { mode: 0o600 });
        })
        .onDisconnect(() => { this.connected = false; })
        .onConnectError((_ctx: unknown, err: unknown) => reject(err));
      if (saved) builder = builder.withToken(saved);
      this.conn = builder.build();
    });
  }

  async call<R extends ReducerName>(name: R, args: ReducerArgs[R]): Promise<void> {
    const fn = this.conn.reducers[camel(name)];
    if (typeof fn !== 'function') throw new ReducerError(`reducer ${name} is not in the generated bindings`);
    try {
      await fn.call(this.conn.reducers, args);
    } catch (e) {
      throw new ReducerError((e as Error).message);
    }
  }

  read(): Tables {
    const out = {} as Tables;
    for (const [key, view] of Object.entries(VIEWS)) (out as any)[key] = [...this.conn.db[camel(view)].iter()];
    return out;
  }

  onEvent(listener: (e: EventRow) => void) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}
