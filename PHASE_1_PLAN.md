# Phase 1 — Pine Labs Integration Rewrite

## Context

This is a clean-slate rewrite of `pinelabs_fin` per the Phase 1 architecture brief.
Goals:

- Single source of truth for all payment attempts via a new `Pine Labs Transaction` doctype.
- **Zero custom fields on `Sales Invoice`** (or any other ERPNext core doctype).
- `Mode of Payment` becomes the source of truth for which Pine Labs payment methods exist and how they route (Terminal vs Plural vs Both).
- Two buttons on configured documents: **Pay on Machine** (terminal) and **Send Payment Link** (Plural). Direct Net Banking lives as an option inside the Send Payment Link flow.
- Payment Entry is created **only** on confirmed success — never on click, never on link generation.

No backward compatibility. Development-only environment. Existing data is acceptable to lose.

This file supersedes the prior Direct-Net-Banking plan at
`~/.claude/plans/besed-in-this-create-jazzy-spring.md`.

---

## Sign-off needed before any code is touched

### 1. Kill list

Confirm that the table below is correct. Anything in the **DELETE** column is removed during Step 1; anything in **REPLACE** is rewritten in place; **KEEP** stays as-is.

| Item | Action | Notes |
|---|---|---|
| `Pinelabs Transaction Log` doctype | DELETE | Replaced by `Pine Labs Transaction`. |
| `Pinelabs Payment Link Log` doctype | DELETE | Same. |
| `Pinelabs Netbanking Log` doctype | DELETE | Created in the prior NB pass; superseded. |
| `Pinelabs Payment Mode` child doctype | DELETE | `Mode of Payment` is the new source. |
| `Pinelabs Bank` doctype | DELETE | No bank master; Quick NB takes a free-form NB#### code on the dialog. |
| `pinelabs_fin/patches/add_payment_link_fields.py` | DELETE | Adds Sales Invoice custom fields — banned by the new architecture. |
| `pinelabs_fin/patches/setup_pinelabs_payment_modes.py` | REPLACE | Old logic seeded the deleted child table; new patch seeds `Mode of Payment` with the 4 PineLabs modes. |
| `pinelabs_fin/patches/dedupe_payment_link_logs.py` | DELETE | Pre-empts a unique index on a doctype that's going away. |
| `pinelabs_fin/api/payment_link.py` | DELETE | Logic moves into new modules. |
| `pinelabs_fin/api/netbanking.py` | DELETE → MERGE | Direct NB merges into the unified webhook + plural service. |
| `pinelabs_fin/api/services.py` | KEEP, REWRITE | Trim to new shape. |
| `pinelabs_fin/api/pinelabs_client.py` | KEEP, MOSTLY UNCHANGED | Plural + Cloud HTTP calls; recent helpers (`_plural_headers`, `_plural_request_timestamp`, `create_netbanking_payment`) stay. |
| `pinelabs_fin/api/host_adapter.py` | KEEP, REWRITE | Updated for `Pine Labs Transaction`. |
| `pinelabs_fin/api/webhooks.py` | REPLACE → `webhook.py` | Plan §6 wants `pinelabs_fin.api.webhook.handle_webhook`. |
| `pinelabs_fin/public/js/payment_link_button.js` | REPLACE | New two-button + dialog model. |
| `pinelabs_fin/public/js/form_button_injector.js` | KEEP, REVISIT | Generic injector probably stays. |

### 2. Branch strategy

Pick one:

- **(a)** Continue on `feature-MOP`. First commit of the rewrite is a big destructive cleanup, then build on top.
- **(b)** New branch `phase-1-rewrite` cut from `main`. Cherry-pick only the bug fixes worth keeping (signature validation, idempotency, customer extraction). Recommended — matches "no backward compat, build clean".
- **(c)** New branch from current HEAD, do the rewrite there.

### 3. Validation timing

Plan §13 says *"at least one machine must have Is Default checked"*. On a fresh install with zero machines, that would block saving Settings.

Confirm the rule reads as: **if any machines exist, exactly one must be default.** Empty machines table is allowed (integration just isn't usable yet — same as today).

---

## Architecture summary

### Doctypes

#### `Pine Labs Transaction` (new — single source of truth)

| Field | Type | Notes |
|---|---|---|
| `reference_doctype` | Link → DocType | Polymorphic source. |
| `reference_name` | Dynamic Link | Keyed on `reference_doctype`. |
| `transaction_id` | Data, unique | App-generated reference (`PLT-…`). |
| `flow_type` | Select: `Terminal` / `Plural` / `Direct NB` | Set on creation, not editable. |
| `payment_method` | Data | `CARD` / `UPI` / `NB` / `WALLET`. Filled when known. |
| `amount` | Currency | |
| `status` | Select: `INITIATED` / `PENDING` / `SUCCESS` / `FAILED` / `CANCELLED` | |
| `machine` | Link → Pinelabs Machine Config | Terminal only. |
| `bank_code` | Data | Direct NB only — free-form NB#### code (e.g. NB1007). |
| `order_id` | Data | Plural's `order_id` / `payment_link_id`. |
| `payment_id` | Data | Pine Labs payment reference; written to Payment Entry's `reference_no`. |
| `mode_of_payment` | Link → Mode of Payment | Resolved at Payment Entry creation. |
| `payment_entry` | Link → Payment Entry | Read-only; populated after success. |
| `request_payload` | Code (JSON) | |
| `response_payload` | Code (JSON) | |
| `error_message` | Small Text | |
| `created_at` | Datetime | |
| `completed_at` | Datetime | |

#### `Pinelabs Machine Config` (modified)

Add: `is_default` (Check). Validation: at most one row may have `is_default = 1`. Auto-default the only row when count = 1.

#### `Mode of Payment` (custom fields)

| Fieldname | Type | Notes |
|---|---|---|
| `pinelabs_enabled` | Check | Master toggle. |
| `pinelabs_flow_type` | Select: `Terminal` / `Plural` / `Both` | |
| `pinelabs_terminal_code` | Int | `1` (Card) / `11` (UPI) / null. Only for Terminal/Both. |
| `pinelabs_plural_method` | Data | `CARD` / `UPI` / `NB` / `WALLET`. Only for Plural/Both. |

Install patch seeds 4 records:

| Name | Flow | Terminal Code | Plural Method |
|---|---|---|---|
| PineLabs - Card | Both | 1 | CARD |
| PineLabs - UPI | Both | 11 | UPI |
| PineLabs - NetBanking | Plural | — | NB |
| PineLabs - Wallet | Plural | — | WALLET |

#### `Pinelabs Settings` (rewrite)

Single page, collapsible sections (no tabs in Phase 1):

- **Basic Setup** (default open) — master enabled toggle, machine table with `is_default`, terminal selection method (locked to "Single Machine").
- **Plural Configuration** (collapsed) — enable_payment_links, plural_client_id, plural_client_secret, plural_use_sandbox, plural_base_url, payment_link_webhook_secret. Read-only webhook URL with a copy button.
- **Payable Documents** (collapsed) — existing `payable_doctypes` table. Validation: `Payment Entry` cannot be added.
- **Technical** (collapsed) — API endpoint overrides, `verify_ssl`, debug toggle, auto-cancel duration for stuck transactions.

The old `payment_modes` child table is gone.

### Buttons

#### Pay on Machine
- Visible iff: `docstatus == 1`, outstanding > 0, doctype is in Payable DocTypes, doctype's `allow_terminal == 1`.
- Click → dialog listing Mode of Payment rows where `pinelabs_enabled = 1` AND `pinelabs_flow_type IN ('Terminal', 'Both')`.
- Submit → `terminal.initiate(transaction_id, mode_of_payment, machine)`.
- Modal polls every 3s until `SUCCESS` / `FAILED` / `CANCELLED`. Cancel button calls `terminal.cancel`.

#### Send Payment Link
- Visible iff: `docstatus == 1`, outstanding > 0, doctype's `allow_payment_link == 1`, customer has email or mobile.
- Click → 4-option dialog with Flow selector:
  - **All Methods** — Plural hosted page (default), all 4 methods enabled.
  - **Quick Net Banking** — free-form NB#### bank code → seamless Direct NB API → redirect.
  - **UPI Only** — Plural link with `allowed_payment_methods = ['UPI']`.
  - **Card Only** — Plural link with `allowed_payment_methods = ['CARD']`.
- If no active banks, default to All Methods (no dialog).
- Submit → `plural.initiate(transaction_id, flow)` or `direct_nb.initiate(transaction_id, bank_code)`.
- No Payment Entry created at this stage. Webhook handles completion.

### Webhook

`POST /api/method/pinelabs_fin.api.webhook.handle_webhook` (guest-allowed):

1. Read `X-Verify` header **before** parsing JSON.
2. HMAC-SHA256 the raw body with the configured secret. `hmac.compare_digest`. Refuse if secret missing — never silently bypass.
3. Parse JSON, find `Pine Labs Transaction` by `order_id`.
4. If transaction `status == SUCCESS`, return 200 immediately (idempotency on retries).
5. Resolve event:
   - `PROCESSED` / `SUCCESS` / `PAID` → `transaction.mark_success(payment_id, payment_method)`.
   - `FAILED` / `CANCELLED` / `EXPIRED` → `transaction.mark_failed(reason)`.
6. HTTP semantics:
   - 200 on success, idempotent, or known-failure.
   - 401 on signature mismatch / missing secret.
   - 404 on unknown order_id (Plural won't retry).
   - 500 on system error (Plural will retry).

### Payment Entry creation rules

Enforced in `transaction.create_payment_entry(transaction_doc)`:

1. Only callable from terminal-success and webhook-success paths.
2. Bail if `Payment Entry.reference_no = transaction.payment_id` already exists.
3. Bail if `transaction.payment_entry` is already populated.
4. Use `erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry`.
5. Set `mode_of_payment` from the resolved Mode of Payment, `reference_no` to `payment_id`, `reference_date` to `completed_at`, remarks include flow type.
6. Write `payment_entry` back to the transaction; flip status to `SUCCESS`.
7. Whole step is one DB transaction; rollback on any failure and leave status `PENDING` so retries work.

---

## Build order

Each step ends with a working state. Steps 1–3 are destructive; 4–11 are additive.

| # | Step | End state |
|---|---|---|
| 1 | Delete the kill list. Strip `services.py` and `host_adapter.py` of references to gone doctypes. | App boots, no payment buttons anywhere. Clean slate. |
| 2 | Create `Pine Labs Transaction` doctype + Mode of Payment custom fields + install patch seeding the 4 modes. | Transactions can be created and queried via Frappe UI. |
| 3 | Rewrite `Pinelabs Settings` and `Pinelabs Machine Config`: drop `payment_modes`, add `is_default`, add Plural section, add validations. | Settings page renders the new shape; admin can configure. |
| 4 | New `pinelabs_fin/api/transaction.py` — core logic: create/update transaction, status transitions, Payment Entry creation guard rails. Pure functions. | Unit-testable in isolation. |
| 5 | New `pinelabs_fin/api/webhook.py` — Plural webhook handler. Hits `transaction.py`. | Webhooks flow end-to-end (terminal not yet wired). |
| 6 | New `pinelabs_fin/api/terminal.py` — terminal initiate / poll / cancel. Hits `transaction.py`. | Terminal flow works server-side. |
| 7 | New `pinelabs_fin/api/plural.py` — Plural link initiate (all-methods + per-method override). Hits `transaction.py`. | Plural flow works server-side. |
| 8 | Quick Net Banking option in `plural.py`: when `flow=quick_nb`, calls the seamless API + `bank_code` (free-form NB####). | All three button-paths work server-side. |
| 9 | Frontend: rewrite `payment_link_button.js` as `pinelabs_buttons.js` driven by Payable Doctype config + Mode of Payment query. Two buttons + dialogs. | Full E2E from button click to Payment Entry. |
| 10 | Validations + Transaction record UI niceties (color indicators, refresh button, retry button) + error-message catalog. | Polished. |
| 11 | Tests: unit (transitions, signature, dedup) + integration (full terminal, full plural, webhook). | Green. |

---

## Things explicitly out of Phase 1

- Tabbed Settings UI (deferred to Phase 2).
- Per-machine Plural credentials (Phase 2).
- Warehouse / user / cost-center routing (Phase 2 / 3).
- Auto-poll of Plural status (only manual refresh).
- Migration patches preserving old data (greenfield only).

---

## Verification (post-implementation)

1. **Migration**: `bench --site <site> migrate` runs the new install patch; the 4 PineLabs Modes of Payment exist; `Pine Labs Transaction` doctype exists; old `*_log` doctypes are gone.
2. **Settings UI**: open Pinelabs Settings → no `payment_modes` section; machines table has `Is Default` column; Plural section visible; Payable Documents validation rejects `Payment Entry`.
3. **Terminal flow** (against simulator): click Pay on Machine on a submitted Sales Invoice → method dialog shows Card and UPI only → choose Card → modal polls → simulator approves → Payment Entry created with `reference_no = <transaction_id>`, mode `PineLabs - Card`, status Submitted.
4. **Plural flow — All Methods**: Send Payment Link → Plural returns URL → transaction in `PENDING` → simulator fires webhook with `event_type = ORDER_PROCESSED`, method `UPI` → Payment Entry created with mode `PineLabs - UPI`.
5. **Plural flow — Quick NB**: Send Payment Link → Quick Net Banking dialog → type `NB1007` → redirect → callback → Payment Entry created with mode `PineLabs - NetBanking`.
6. **Idempotency**: replay the same webhook twice → second call returns 200 with no second Payment Entry.
7. **Tests**: `bench --site <site> run-tests --app pinelabs_fin` is green.

---

## What I will not do without explicit confirmation

- Delete files on the kill list.
- Pick a branch (a / b / c).
- Draft the new doctype JSONs without confirming `request_payload` is `Code` (with JSON content), section break order, and whether `flow_type` should be locked after creation.

Once those three are decided, Step 1 starts.
