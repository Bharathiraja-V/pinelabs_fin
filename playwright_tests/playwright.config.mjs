import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: /pinelabs_ui(_extended)?\.spec\.mjs/,
  timeout: 90000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:8015',
    storageState: 'storageState.json',
    viewport: { width: 1500, height: 900 },
    actionTimeout: 20000,
    screenshot: 'only-on-failure',
  },
});
