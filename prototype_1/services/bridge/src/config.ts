// All configuration comes from the environment (or a local .env the operator writes). Service keys
// live here on the laptop only; the phone receives short-lived tokens, never these values.
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
export const SERVICES_DIR = path.resolve(here, '../..');

function loadDotEnv(file: string) {
  if (!existsSync(file)) return;
  for (const line of readFileSync(file, 'utf8').split('\n')) {
    const m = /^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/.exec(line);
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2].replace(/^['"]|['"]$/g, '');
  }
}
loadDotEnv(path.join(SERVICES_DIR, '.env'));

const env = (name: string, fallback = '') => process.env[name] ?? fallback;

export const config = {
  host: env('LEA_HOST', '0.0.0.0'),
  port: Number(env('LEA_PORT', '8787')),
  dataDir: path.resolve(env('LEA_DATA_DIR', path.join(SERVICES_DIR, 'data'))),
  stdbUri: env('LEA_STDB_URI', 'ws://127.0.0.1:3000'),
  stdbDatabase: env('LEA_STDB_DATABASE', 'lea'),
  grainUrl: env('GRAIN_URL', 'http://127.0.0.1:8000').replace(/\/$/, ''),
  grainKey: env('GRAIN_KEY'),
  cartesiaKey: env('CARTESIA_API_KEY'),
  cartesiaVersion: env('CARTESIA_VERSION', '2026-08-14'),
  cartesiaVoiceId: env('CARTESIA_VOICE_ID'),
  anthropicConfigured: Boolean(env('ANTHROPIC_API_KEY') || env('ANTHROPIC_AUTH_TOKEN')),
  jevKey: env('JEV_API_KEY'),
  agentSessionUsd: Number(env('LEA_AGENT_SESSION_USD', '0')),   // 0 = no cap; the owner sets the budget
  geometryPython: env('LEA_GEOMETRY_PYTHON', path.join(SERVICES_DIR, 'reconstruction/.venv/bin/python')),
  stepPython: env('LEA_STEP_PYTHON', path.join(SERVICES_DIR, '.cad-venv/bin/python')),   // build123d, for STEP re-import
  objcap: env('LEA_OBJCAP', path.join(SERVICES_DIR, 'reconstruction/objcap/lea-objcap')),
  objcapDetail: env('LEA_OBJCAP_DETAIL', 'medium'),
  workerId: env('LEA_WORKER_ID', `bridge-${process.pid}`),
};
