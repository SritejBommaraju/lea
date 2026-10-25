// Build the static page into dist/: index.html, style.css, config.js and the bundled scripts.
// SPACETIME_HOST / SPACETIME_DB set the defaults in dist/config.js; edit that file (or add ?host=&db= to the URL)
// to point a deployed copy elsewhere without rebuilding.
import { build } from 'esbuild';
import { copyFileSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';

const host = process.env.SPACETIME_HOST || 'http://127.0.0.1:3000';
const db = process.env.SPACETIME_DB || 'lea-live';

rmSync('dist', { recursive: true, force: true });
mkdirSync('dist', { recursive: true });
await build({
  entryPoints: ['src/main.ts'],
  bundle: true,
  splitting: true,
  format: 'esm',
  target: 'es2022',
  minify: true,
  sourcemap: false,
  outdir: 'dist',
  legalComments: 'linked',
  logLevel: 'warning',
});
copyFileSync('index.html', 'dist/index.html');
copyFileSync('src/style.css', 'dist/style.css');
writeFileSync('dist/config.js', `window.LEA_LIVE = ${JSON.stringify({ host, db })};\n`);
console.log(`built dist/ (SpacetimeDB ${host}, database ${db})`);
