# Pinelabs UI tests (Playwright)

Drives the Frappe Desk like a user. Covers the UI-possible subset of
`pinelabs_test_cases.xlsx` (buttons, state machine, terminal dialog, conflict guard).

## Run
1. Ensure the dev server is up:  `bench serve --port 8015` (site `pine.local`, default_site).
2. Create fixtures + auth:        `cd <bench>/sites && ../env/bin/python <app>/playwright_tests/setup_fixtures.py <outdir>`
   (writes `fixtures.json` + `storageState.json` next to the spec)
3. Install runner (once):         `npm i -D @playwright/test@1.61.1`  (Chromium already cached)
4. Run:                           `PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright npx playwright test --config playwright.config.mjs`

## Notes
- Single worker; some tests share fixtures. Auth is a server-minted session cookie (no password).
- TC-SM-04 documents BUG-03 (manual PENDING→FAILED is accepted — it should be rejected).
- Backend/gateway-only cases (webhook, cron, routing, PE internals, concurrency) are NOT in this
  UI suite — see the bug-repro scripts and `pinelabs_bug_report.xlsx`.
