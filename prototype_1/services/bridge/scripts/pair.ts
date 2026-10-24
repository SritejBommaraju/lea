// Prints a new device token once. Enter it (and the bridge URL) in Lea's Settings.
import { DeviceAuth } from '../src/auth';
import { config } from '../src/config';

const name = process.argv[2] ?? 'iPhone';
console.log(`Device "${name}" token (shown once):\n${new DeviceAuth(config.dataDir).pair(name)}`);
