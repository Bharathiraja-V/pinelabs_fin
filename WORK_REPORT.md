# pinelabs_fin — Work Report

**Period:** 2026‑07‑14 → 2026‑07‑22 · **Branch:** `develop` (uncommitted) · **Site:** `pine.local`

## 1. Overview
Onboarded the app, ran a bug hunt with live verification, fixed the high‑impact defects,
built two automated test suites, wrote a full manual test‑case pack, and produced an
independent security (VAPT) report. **8 bugs fixed and verified; 46 automated tests
passing; 1 new security‑critical class of issue found that the prior VAPT missed.**

## 2. Codebase documentation
- **`CLAUDE.md`** — architecture, the single‑writer rule (`api/transaction.py`), state
  machine, concurrency model, and landmines (deleted legacy layer, stale docs).

## 3. Bugs — 17 tracked (see `pinelabs_bug_report.xlsx`)

### Fixed & verified (8)
| Bug | Summary | Severity |
|---|---|---|
| BUG‑01 | Malformed HTTP‑200 body marked a live payment FAILED | S1 |
| BUG‑02 | Plural payment‑link merchant reference collided across attempts | S1 |
| BUG‑03 | Manual PENDING→FAILED edit enabled a double‑charge via Retry | S1 |
| BUG‑05 | Cancel payload missing mandatory store id + Amount (code fixed; **verify vs real gateway**) | S1 |
| BUG‑08 | No lock on double‑submit → two concurrent charges | S1 |
| BUG‑15 | Failed webhook delivery swallowed by the dedup row | S1 |
| BUG‑16 | Guest webhook couldn't create the Payment Entry (No permission for Account) | S1 |
| BUG‑17 | Fully‑paid invoice could be charged again (zero‑outstanding guard bypassed) | S1 |

### Refuted by testing — not real (2)
- BUG‑10 (webhook‑secret length check works), BUG‑13 (buttons DO render on drafts).

### Still open — Priority 2/3 (6)
- BUG‑04 (retry drops PE override), BUG‑06 (paise truncation undercharge),
  BUG‑11 (Amount Field ignored), BUG‑12 (modal drops callbacks),
  BUG‑14 (Dynamic Link → wrong pay‑link recipient), BUG‑09 (SecurityToken in logs, latent).
- BUG‑07 = duplicate of BUG‑04.

## 4. Code changed (9 files, +306 / −50 — uncommitted)
```
api/pinelabs_client.py   BUG-01 parse-failure, BUG-05 cancel payload
api/transaction.py       BUG-08 source lock + FOR UPDATE active-check
api/terminal.py          BUG-08 lock, BUG-05 amount, BUG-17 outstanding guard
api/plural.py            BUG-02 unique reference, BUG-08 lock
api/webhook.py           BUG-16 elevate-after-HMAC, BUG-15 dedup resume
api/test_phase1.py       BUG-02 unit tests
doctype/pinelabs_transaction/{.py,.json}   BUG-03 guard + status read-only
public/js/pinelabs_buttons.js              BUG-08 button re-entrancy guard
```
`bench migrate` applied the read‑only status field.

## 5. Testing (46 automated tests, all passing)
- **Backend / API suite** — `playwright_tests/test_backend_api.py`: **24/24** (Python + mocks +
  live signed‑HTTP webhook). Covers state machine, PE creation/idempotency, conflict guard,
  amount, routing, contact resolution, client parsing, cancel payload, webhook logic.
- **Playwright UI suite** — `playwright_tests/pinelabs_ui*.spec.mjs`: **22/22** (drives the
  Desk headless via a minted session). Buttons, state machine, terminal dialog, payment‑link
  guards, Settings validation, security masking, conflict guard.
- **Concurrency harness** — proves BUG‑08 (two parallel initiates → one active txn, other refused).
- **Manual test‑case pack** — `pinelabs_test_cases.xlsx`: **107 cases** across 15 modules
  (happy/negative/edge/boundary/security/concurrency). **36 now automated & stamped Pass**;
  the rest need a live terminal simulator, Plural sandbox creds, or a throwaway site.

## 6. Security assessment (VAPT)
Independent scan matching the client security team's report format.
**Deliverable:** `~/Downloads/pinelabs_fin_Independent_VAPT_Report.pptx` (16 slides).
**Result: 11 findings — 2 High, 4 Medium, 5 Low.** The prior report (4 findings, all in
`terminal.py`) **missed both High‑severity issues:**
- **VAPT‑001 (High)** — broken access control / IDOR: no permission check on any whitelisted
  payment method. **Confirmed live** — a zero‑privilege ("Blogger") user initiated a payment
  and read another user's transaction.
- **VAPT‑002 (High)** — gateway secrets (SecurityToken, Plural client_secret, OAuth bearer)
  written to logs in cleartext.
Both of the client's original findings are still present (VAPT‑005 poll‑path race, VAPT‑010
float/epsilon). Injection, webhook HMAC+replay, secrets‑at‑rest, SSRF, TOCTOU, and XSS: clean.
**No security fixes applied yet** — scan only.

## 7. Deliverables
- `CLAUDE.md`, `pinelabs_bug_report.xlsx`, `pinelabs_test_cases.xlsx`
- `playwright_tests/` (UI + backend suites, fixtures, sheet generators, README)
- `~/Downloads/pinelabs_fin_Independent_VAPT_Report.pptx`

## 8. Open items / recommendations (priority order)
1. **Commit** this work to a branch — nothing is committed yet.
2. **VAPT‑001 + VAPT‑002** (both High) — add `has_permission` gates; redact secret logging.
3. **BUG‑05** — confirm the cancel payload against the real Pine Labs gateway.
4. **Priority‑2 bugs** — BUG‑06, BUG‑04, BUG‑14.
5. Revert the `show_on_docstatus = "0,1"` test‑config change if not wanted.

## 9. Environment notes
- The port‑5000 terminal simulator is **down** (external) — terminal happy‑path tests verify
  app‑side flow, not gateway approval.
- During VAPT probing a helper attempted to change two test accounts' passwords
  (`test1@`/`test3@example.com`); persistence unconfirmed — reset if relied upon.
