# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Authoritative docs in this repo

Before doing non-trivial work, skim these — they are kept up to date and contain context not derivable from the code:

- [PINELABS_FIN_GUIDE.txt](PINELABS_FIN_GUIDE.txt) — full app reference (architecture, doctypes, config, debugging, known issues). Section numbers are stable; cite them when explaining things to the user.
- [PHASE_1_PLAN.md](PHASE_1_PLAN.md) — the rewrite plan this branch (`phase-1-rewrite`) is implementing.
- [TESTING_PHASE_1.md](TESTING_PHASE_1.md) — manual + automated test plan for the rewrite.
- [MARKETPLACE_PLAN.md](MARKETPLACE_PLAN.md) — outstanding checklist before publishing to the Frappe Marketplace.

## What this app is

A Frappe / ERPNext app that integrates Pine Labs payments. It adds two buttons to "payable" documents (Sales Invoice, POS Invoice, Payment Request, etc.):

- **Pay on Machine** — routes payment to a physical Pine Labs Cloud terminal.
- **Send Payment Link** — generates a Plural hosted payment link (card / UPI / NB / wallet).

On completion the app creates the ERPNext Payment Entry automatically. It is designed to be marketplace-clean: **zero custom fields on ERPNext core doctypes**; the only custom fields are on Mode of Payment (a configuration doctype).

## Environment / common commands

Bench root: `~/bench-bulb/`. Site: `payment.local`. Dev web port: `8006`. Local simulator (if running): `http://127.0.0.1:5000`.

```bash
# from the bench root
bench start                                          # dev server (honcho)
bench --site payment.local migrate                   # apply schema + patches
bench --site payment.local console                   # Python REPL with frappe
bench --site payment.local tail-logs
bench --site payment.local run-tests --app pinelabs_fin
bench --site payment.local run-tests --app pinelabs_fin --module pinelabs_fin.api.test_phase1
bench --site payment.local run-tests --app pinelabs_fin --test test_initial_status_is_initiated
pkill -f "frappe serve --port 8006"                  # reload web code (honcho respawns)
```

`bench start` does not auto-reload Python. After editing `api/*.py`, `pkill` the web worker (or Ctrl+C & restart bench) — see [PINELABS_FIN_GUIDE.txt](PINELABS_FIN_GUIDE.txt) §15.9.

Lint/format (configured in `pyproject.toml` and `.pre-commit-config.yaml`):

```bash
pre-commit install                # one-time
pre-commit run --all-files
ruff check pinelabs_fin           # lint
ruff format pinelabs_fin          # format (TAB indent — see below)
```

## Hard rules when editing this codebase

These are non-obvious invariants. Violating them silently corrupts payment state or breaks the marketplace contract.

1. **Indent with TABS in Python.** `pyproject.toml` sets `[tool.ruff.format] indent-style = "tab"`. Frappe convention. Files mixing spaces and tabs will be reformatted by pre-commit, but the diff will be noisy — write tabs from the start.

2. **Never add custom fields to ERPNext core doctypes** (Sales Invoice, POS Invoice, Payment Entry, Customer, etc.). All state lives in `Pinelabs Transaction`; all config in `Pinelabs Settings`. The 4 fields on Mode of Payment are the only exception and are installed by [pinelabs_fin/patches/install_pinelabs_modes.py](pinelabs_fin/patches/install_pinelabs_modes.py).

3. **All Pinelabs Transaction status transitions must go through `transaction.py`** and take the row lock via `_lock_and_reload(name)` (which does `frappe.db.get_value(..., for_update=True)`). The webhook, the foreground poll, and the cron all race; the lock is what makes finalize idempotent. Don't `frappe.db.set_value("Pinelabs Transaction", ...)` directly to change status.

4. **Payment Entry creation has exactly three entry points**: terminal-success path in `terminal.py`, webhook PAID in `webhook.py → mark_success`, and the cron-driven status refresh in `plural.refresh_payment_link_status`. All three funnel through `transaction.py` and bail if `payment_entry` is already populated or if a PE with the same `reference_no = transaction.payment_id` exists. Don't add a fourth path.

5. **Webhook handler is fail-closed.** `pinelabs_fin/api/webhook.py` returns 401 if `Webhook Secret` is unset or `X-Verify` is missing/invalid. Never relax this — it's a public guest endpoint. HMAC is over the **raw** body, not a re-serialised dict.

6. **`Mode of Payment` is the single source of truth for which methods are PineLabs-enabled.** The Pay-on-Machine dialog filters Modes by `pinelabs_enabled=1 AND pinelabs_flow_type IN ('Terminal','Both')`. Don't hard-code Card/UPI lists anywhere else.

7. **Plural OAuth token is cached and busted in `pinelabs_settings.py:on_update`** when client_id / client_secret / base_url change. If you add a new credential field that affects auth, extend that bust list.

## Architecture in two minutes

```
ERPNext doc (Sales Invoice, ...)
        │  click Pay on Machine / Send Payment Link
        ▼
public/js/pinelabs_buttons.js   ←─ eligibility from Pinelabs Settings.payable_doctypes
        │
        ▼
api/terminal.py            api/plural.py            (both create a)
        │                        │                  Pinelabs Transaction
        ▼                        ▼                  (status state machine,
api/pinelabs_client.py     api/plural_client.py     row-locked transitions)
   Pine Labs Cloud           Plural REST + OAuth
        │                        │
   foreground poll          Plural webhook ─► api/webhook.py ─► mark_success/failed
        +                        +
   1-min cron              1-min cron (api/plural.reconcile_pending_plural_payments)
        │                        │
        └────────────┬───────────┘
                     ▼
            api/transaction.py
            └── creates ERPNext Payment Entry (idempotent)
```

Three-layer defense for "did the payment finish?": foreground UI poll → realtime push → 1-minute reconcile cron (`hooks.scheduler_events`). The cron is the backstop and must remain idempotent.

## Button render gating (Payable Doctype row → JS)

The "Pay on Machine" / "Send Payment Link" buttons are injected by [pinelabs_fin/public/js/pinelabs_buttons.js](pinelabs_fin/public/js/pinelabs_buttons.js) and are gated by **four axes** read off the matching `Pinelabs Payable Doctype` row in Settings. All four must pass — fail any and the corresponding button is suppressed.

| Axis | Source | Behavior |
|---|---|---|
| Row is enabled | `is_enabled` (Check) | If 0, the doctype is dropped from the in-memory `PAYABLE.flags` map → neither button renders |
| Docstatus | `show_on_docstatus` (Select: `"0"` / `"1"` / `"0,1"`) | Parsed into a Set of allowed docstatus values. `2` (Cancelled) is never accepted. Empty/garbage falls back to `{1}` |
| Outstanding amount | derived from the doc | JS reads `outstanding_amount → grand_total → amount → paid_amount` (first non-null wins). Buttons hide when ≤ 0 |
| Per-button toggle | `enable_pay_on_machine`, `enable_send_payment_link` (Check) | Each button is gated independently — you can show just one |

The cache shape is `Map<doctype, { pay_on_machine, send_payment_link, allowed_docstatus: Set<number> }>`. Seeded synchronously for Sales Invoice / POS Invoice / Payment Request with default flags so first-render doesn't race; the async fetch then overrides with the saved row values.

**Frappe quirk — always fetch the parent Single, never `get_list` the child table.** `frappe.db.get_list("Pinelabs Payable Doctype", { fields: [...] })` silently returns rows where Link and custom fields come back as `undefined` (only `name` is populated). The JS uses `frappe.client.get` on `Pinelabs Settings` instead; child rows arrive fully hydrated as `doc.payable_doctypes`. Don't "fix" this back to `get_list` — that's how I broke the per-row toggles the first time around.

## Pinelabs Settings layout (current)

`Basic Setup → Machines → Machine Mappings → Technical → Plural API Configuration → Payable Documents`. The Technical section sits **above** Plural because its two surviving fields (`api_endpoints`, `auto_cancel_duration_minutes`) are terminal-related; keeping all terminal config contiguous reads better.

Removed from the doctype schema (do not re-add without justification):
- `Pinelabs Settings.verify_ssl` — was Plural-only despite the label; terminal client hard-codes `verify=False`. Never toggled in production.
- `Pinelabs Settings.debug_mode` — never consumed by any code path.
- `Pinelabs Payable Doctype.button_label` — never read by the JS or transaction.py.

If a field is in the JSON but no code reads it, that's a UI trap (especially when `reqd: 1`). Audit before adding "soft" config knobs — wire them up in the same change, or don't add them.

## Where things live

- `pinelabs_fin/api/` — orchestration + HTTP clients + webhook + tests. See [PINELABS_FIN_GUIDE.txt](PINELABS_FIN_GUIDE.txt) §6 for the file map.
- `pinelabs_fin/pinelabs_fin/doctype/` — Frappe doctype definitions.
- `pinelabs_fin/patches/` — install + migration patches; register them in `pinelabs_fin/patches.txt`.
- `pinelabs_fin/public/js/pinelabs_buttons.js` — global JS that injects the two buttons. Loaded via `hooks.app_include_js`.
- `pinelabs_fin/hooks.py` — Frappe lifecycle wiring (scheduler cron lives here).

## Test suite

All tests are in [pinelabs_fin/api/test_phase1.py](pinelabs_fin/api/test_phase1.py). As of last run there are 6 known failures whose root cause is fixtures relying on a Sales Invoice that doesn't survive `frappe.db.rollback()` between methods — the fix pattern (move setup from `setUpClass` to per-test `setUp`) is already used in `TestRouting`. See [PINELABS_FIN_GUIDE.txt](PINELABS_FIN_GUIDE.txt) §15.2.

## Debugging Pay-by-Link

The bench-start terminal is the primary log channel — every Pay-by-Link API call prints a labelled block (`[PineLabs PayByLink] Initiate:`, `... Token Request:`, `... Link Request:`, `... Webhook Received:`, etc.). `frappe.logger("pinelabs_paybylink")` is unreliable in some benches — don't trust the named log file. Full list of labels and what they mean: [PINELABS_FIN_GUIDE.txt](PINELABS_FIN_GUIDE.txt) §13.

```bash
bench start 2>&1 | tee /tmp/bench-start.log
grep "PineLabs PayByLink" /tmp/bench-start.log
```

Each Pinelabs Transaction also stores `request_payload`, `response_payload`, and `error_message` on the form — open the record in the desk for an in-app view of the last API exchange.
