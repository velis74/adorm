import { defineConfig, devices } from '@playwright/test';

import { API_PORT, DATABASE } from './settings';

/**
 * End-to-end tests of the demo: `demo.py --frontend built` serves the API on every available driver and the built
 * frontend (`npm run demo:build`). The API runs on a database of its own (`adorm_demo_e2e` on the server of
 * ADORM_DEMO_DATABASE_URL), set up with the fixed data set and dropped afterwards, and on a port of its own.
 */
export default defineConfig({
  testDir: '.',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'list',
  outputDir: '../test-results',
  timeout: 60_000,
  forbidOnly: !!process.env.CI,
  globalTeardown: './teardown.ts',

  use: {
    baseURL: `http://127.0.0.1:${API_PORT}`,
    trace: 'retain-on-failure',
  },

  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1600, height: 1000 } } }],

  webServer: {
    command: `uv run python demo.py --database ${DATABASE} --reset --frontend built --port ${API_PORT}`,
    url: `http://127.0.0.1:${API_PORT}/api/health`,
    cwd: '../..',
    reuseExistingServer: false,
    timeout: 180_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
