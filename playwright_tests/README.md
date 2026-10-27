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

## v16 end-to-end run (`test_app_e2e_simulator.py`)

Constants at the top (`SITE`, `SITES_PATH`, `COMPANY`, `CUSTOMER`, `ITEM`, accounts) match the `pine.local` v16 bench.
Run from `<bench>/sites`: `../env/bin/python ../apps/pinelabs_fin/playwright_tests/test_app_e2e_simulator.py`.

Result on v16: 33 run, 33 pass. The payment-link cases tick `Enable Payment Links` for the run and restore it,
and give the Plural client the simulator's demo credentials in memory only (nothing saved to Settings).
The v15 baseline had 4 failures; on v16 they are fixed (concurrent polling, part-payment, over-charge).
Needs a customer named `No Contact Customer` (no email/mobile) for the contact guard test.
