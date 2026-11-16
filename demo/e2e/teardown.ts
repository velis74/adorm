import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { DATABASE } from './settings';

/** Drops the e2e run's database while the API still runs; `WITH (FORCE)` ends the API's connections. */
export default function teardown(): void {
  execFileSync('uv', ['run', 'python', 'demo.py', '--database', DATABASE, '--drop'], {
    cwd: fileURLToPath(new URL('../..', import.meta.url)),
    stdio: 'inherit',
  });
}
