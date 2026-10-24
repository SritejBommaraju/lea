import { defineConfig } from 'vitest/config';
import { spacetimedbModuleTestPlugin } from 'spacetimedb/server/test-utils/vitest';

// The plugin lets tests run the real module logic in SpacetimeDB's portable datastore.
export default defineConfig({ plugins: [spacetimedbModuleTestPlugin()], test: { testTimeout: 30_000 } });
