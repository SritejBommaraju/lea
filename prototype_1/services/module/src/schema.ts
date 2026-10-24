// Lea state module (SpacetimeDB 2.13). Every base table is private; only identities an admin
// authorized as a bridge may call reducers or read the bridge_* views. Gates that decide whether
// evidence may drive diagnosis or CAD live here, in code, so no client can skip them.
import { schema, table, t } from 'spacetimedb/server';
import { rowShapes, TABLE_NAMES } from './shapes';

export const MAX_ASSET_BYTES = 2n * 1024n * 1024n * 1024n;
export const SHA256 = /^[0-9a-f]{64}$/;
export const ID = /^[A-Za-z0-9_-]{1,64}$/;

export const ASSET_KINDS = [
  'photo_orbit', 'photo_closeup', 'photo_observation', 'voice', 'reference_mesh', 'reference_photo',
  'recon_mesh', 'recon_bundle', 'derived_ply', 'qa_report', 'design_snapshot', 'design_file',
] as const;
export const JOB_KINDS = ['reconstruction', 'geometry_compare', 'closeup_compare', 'design_turn'] as const;
// queued -> running -> succeeded | failed | needs_user | uncertain (handoff §3 job states)
export const TERMINAL = ['succeeded', 'failed', 'needs_user', 'uncertain'] as const;
export const FINDING_SOURCES = ['geometry', 'vision', 'meter', 'user'] as const;
export const VOICE_MODES = ['wake', 'ptt'] as const;
export const COMMAND_ORIGINS = ['voice', 'text', 'auto'] as const;

const R = rowShapes(t);
const admin = table({ name: TABLE_NAMES.admin }, R.admin);
const bridgeIdentity = table({ name: TABLE_NAMES.bridgeIdentity }, R.bridgeIdentity);
const repairSession = table({ name: TABLE_NAMES.repairSession }, R.repairSession);
const asset = table({ name: TABLE_NAMES.asset }, R.asset);
const qaPolicy = table({ name: TABLE_NAMES.qaPolicy }, R.qaPolicy);
const job = table({ name: TABLE_NAMES.job }, R.job);
const meshReport = table({ name: TABLE_NAMES.meshReport }, R.meshReport);
const finding = table({ name: TABLE_NAMES.finding }, R.finding);
const repairCheck = table({ name: TABLE_NAMES.repairCheck }, R.repairCheck);
const repairAttempt = table({ name: TABLE_NAMES.repairAttempt }, R.repairAttempt);
const voiceTurn = table({ name: TABLE_NAMES.voiceTurn }, R.voiceTurn);
const designCommand = table({ name: TABLE_NAMES.designCommand }, R.designCommand);
const designVersion = table({ name: TABLE_NAMES.designVersion }, R.designVersion);
const sessionEvent = table({ name: TABLE_NAMES.sessionEvent }, R.sessionEvent);

const spacetimedb = schema({
  admin, bridgeIdentity, repairSession, asset, qaPolicy, job, meshReport, finding, repairCheck,
  repairAttempt, voiceTurn, designCommand, designVersion, sessionEvent,
});
export default spacetimedb;
export { repairSession, asset, qaPolicy, job, meshReport, finding, repairCheck, repairAttempt, voiceTurn, designCommand, designVersion, sessionEvent };

