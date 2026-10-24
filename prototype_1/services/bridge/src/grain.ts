// Typed client for the Grain HTTP endpoints Lea calls (API surface only; Grain itself is never modified).
// Only routes RepairOS needs: health, projects, files, messages, events, chat.

export class GrainError extends Error {
  constructor(readonly status: number, message: string, readonly ambiguous = false) { super(message); }
}

export interface GrainHealth {
  ok: boolean; agent_ready: boolean; model: string | null; keys_required: boolean; accounts: boolean;
  services: Record<string, boolean>; tools: Record<string, string | null>;
}
export interface GrainProject {
  id: string; name: string; created: number | string | null; updated: number | null; turns: number;
  usage: { usd?: number } | null; busy: boolean; waiting: boolean;
  last_build: { ok: boolean; errors: number; seconds: number | null; artifacts: unknown } | null;
}
export interface GrainFile { path: string; size: number; mtime: number }
export interface GrainQuestion { type: 'question'; id?: string; why?: string; questions: { id?: string; text: string; options?: string[]; free_text?: boolean }[] }
export type GrainLogEntry = { type?: string; [k: string]: unknown };

export class GrainClient {
  constructor(readonly baseUrl: string, private readonly key: string, private readonly timeoutMs = 60_000) {}

  private headers(extra: Record<string, string> = {}) {
    return { ...(this.key ? { Authorization: `Bearer ${this.key}` } : {}), ...extra };
  }

  private async request(method: string, path: string, body?: unknown, timeoutMs = this.timeoutMs): Promise<Response> {
    let res: Response;
    try {
      res = await fetch(`${this.baseUrl}${path}`, {
        method, headers: this.headers(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (e) {
      // The request may or may not have reached Grain; callers must not blindly retry side effects.
      throw new GrainError(0, `Grain unreachable at ${this.baseUrl}: ${(e as Error).message}`, true);
    }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = ((await res.json()) as { detail?: string }).detail ?? detail; } catch { /* not JSON */ }
      throw new GrainError(res.status, `Grain ${method} ${path} -> ${res.status}: ${detail}`);
    }
    return res;
  }

  private async json<T>(method: string, path: string, body?: unknown): Promise<T> {
    return (await this.request(method, path, body)).json() as Promise<T>;
  }

  health() { return this.json<GrainHealth>('GET', '/health'); }
  createProject(name: string) { return this.json<GrainProject>('POST', '/projects', { name }); }
  project(pid: string) { return this.json<GrainProject>('GET', `/projects/${enc(pid)}`); }
  files(pid: string) { return this.json<GrainFile[]>('GET', `/projects/${enc(pid)}/files`); }
  messages(pid: string) { return this.json<GrainLogEntry[]>('GET', `/projects/${enc(pid)}/messages`); }

  async file(pid: string, filePath: string): Promise<Buffer> {
    const safe = filePath.split('/').map(encodeURIComponent).join('/');
    return Buffer.from(await (await this.request('GET', `/projects/${enc(pid)}/files/${safe}`, undefined, 120_000)).arrayBuffer());
  }

  /** Starts a detached design turn. 409 means another turn is running (nothing was started). */
  chat(pid: string, message: string) {
    return this.json<{ started: boolean; id: string; model: string }>('POST', `/projects/${enc(pid)}/chat`, { message, detach: true });
  }

  /** Grain's live event stream for a project, as raw SSE bytes. */
  async events(pid: string, signal: AbortSignal): Promise<ReadableStream<Uint8Array>> {
    const res = await fetch(`${this.baseUrl}/projects/${enc(pid)}/events`, { headers: this.headers(), signal });
    if (!res.ok || !res.body) throw new GrainError(res.status, `Grain events -> ${res.status}`);
    return res.body;
  }

  static lastQuestion(log: GrainLogEntry[]): GrainQuestion | null {
    for (let i = log.length - 1; i >= 0; i--) {
      if (log[i].type === 'question') return log[i] as unknown as GrainQuestion;
      if (log[i].type === 'user_message' || log[i].role === 'user') return null;
    }
    return null;
  }
}

const enc = (s: string) => {
  if (!/^[0-9a-f]{12}$/.test(s)) throw new GrainError(400, 'Grain project ids are 12 hex characters');
  return s;
};
