// StateStore backed by a live SpacetimeDB through the official TypeScript SDK. The client schema is built from the
// module's own shapes (module/src/shapes.ts) the same way generated bindings are, so table names, view names and
// reducer arguments match the module by construction. The bridge keeps its identity token on disk; an admin
// authorizes that identity once with `npm run authorize`.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { DbConnectionBuilder, DbConnectionImpl, procedures, reducerSchema, reducers, schema, t, table } from 'spacetimedb';
import { reducerParams, rowShapes } from '../../module/src/shapes';
import { config } from './config';
import { type EventRow, ReducerError, type ReducerArgs, type ReducerName, type StateStore, type Tables } from './state';

const R = rowShapes(t);
// Each bridge_* view returns rows of one base table; the client sees the view as a table of that row shape.
const VIEWS = {
  sessions: ['bridge_sessions', R.repairSession], assets: ['bridge_assets', R.asset], policies: ['bridge_policies', R.qaPolicy],
  jobs: ['bridge_jobs', R.job], meshReports: ['bridge_mesh_reports', R.meshReport], findings: ['bridge_findings', R.finding],
  checks: ['bridge_checks', R.repairCheck], repairs: ['bridge_repairs', R.repairAttempt], voiceTurns: ['bridge_voice_turns', R.voiceTurn],
  designCommands: ['bridge_design_commands', R.designCommand], designVersions: ['bridge_design_versions', R.designVersion],
  events: ['bridge_events', R.sessionEvent],
} as const;

export function remoteModule() {
  const tables = schema(Object.fromEntries(Object.entries(VIEWS).map(([key, [name, row]]) => [key, table({ name }, row as any)])) as any);
  const reducerDefs = reducers(...Object.entries(reducerParams(t)).map(([name, params]) => reducerSchema(name, params as any)));
  return {
    versionInfo: { cliVersion: '2.13.0' as const },
    tables: tables.schemaType.tables,
    reducers: reducerDefs.reducersType.reducers,
    ...procedures(),
  };
}

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
    const saved = existsSync(this.tokenFile) ? JSON.parse(readFileSync(this.tokenFile, 'utf8')).token : undefined;
    const save = (token: string) => {
      mkdirSync(config.dataDir, { recursive: true });
      writeFileSync(this.tokenFile, JSON.stringify({ identity: this.identityHex, token }), { mode: 0o600 });
    };
    await new Promise<void>((resolve, reject) => {
      let builder: any = new DbConnectionBuilder(remoteModule() as any, (c: any) => new DbConnectionImpl(c))
        .withUri(config.stdbUri)
        .withDatabaseName(config.stdbDatabase)
        .withConfirmedReads(true)  // acknowledge the phone only after durable commit
        .withAutomaticReconnect()
        .onConnect((conn: any, identity: any, token: string) => {
          this.identityHex = identity.toHexString();
          save(token);
          conn.db.events.onInsert((_ctx: unknown, row: EventRow) => this.listeners.forEach(l => l(row)));
          conn.subscriptionBuilder()
            .onApplied(() => { this.connected = true; resolve(); })
            .onError((_ctx: unknown, err: unknown) => reject(err))
            .subscribe(Object.values(VIEWS).map(([name]) => `SELECT * FROM ${name}`));
        })
        .onAutomaticReconnect((_conn: any, _identity: any, token: string) => save(token))
        .onDisconnect(() => { this.connected = false; })
        .onConnectError((_ctx: unknown, err: unknown) => reject(err));
      if (saved) builder = builder.withToken(saved);
      this.conn = builder.build();
    });
  }

  async call<R extends ReducerName>(name: R, args: ReducerArgs[R]): Promise<void> {
    const fn = this.conn.reducers[camel(name)];
    if (typeof fn !== 'function') throw new ReducerError(`reducer ${name} is not in the client schema`);
    try {
      await fn.call(this.conn.reducers, args);
    } catch (e) {
      throw new ReducerError((e as Error).message);
    }
  }

  read(): Tables {
    const out = {} as Tables;
    for (const key of Object.keys(VIEWS)) (out as any)[key] = [...this.conn.db[key].iter()];
    return out;
  }

  onEvent(listener: (e: EventRow) => void) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

const camel = (s: string) => s.replace(/_(\w)/g, (_, c: string) => c.toUpperCase());
