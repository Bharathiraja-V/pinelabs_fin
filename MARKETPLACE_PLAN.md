# Marketplace Publish Plan — pinelabs_fin

Captured 2026-05-09. Saved here so we can pick this up later.

## Status

**Not ready to publish.** Code refactor + standards pass is complete (Phases 1-3 done on `phase-1-rewrite`), but the listing-facing surface and a few real bugs need attention before this can go to the Frappe Marketplace.

---

## Blockers (must fix before submit)

### 1. Failing tests — `test_phase1.py`

5 errors + 1 failure as of last run:

- `TestTransactionStateMachine.test_initial_status_is_initiated`
- `TestTransactionStateMachine.test_illegal_transition_initiated_to_initiated_silent`
- `TestTransactionStateMachine.test_manual_status_to_success_is_blocked`
- `TestTransactionStateMachine.test_pending_to_initiated_is_rejected`
- `TestTerminalInitiate.test_pending_on_response_code_zero`
- `TestPaymentEntryGuards.test_existing_pe_with_same_reference_no_is_reused`

Root cause for most: `setUp` fixtures rely on a Sales Invoice that doesn't survive `frappe.db.rollback()` between methods. Same pattern as we already fixed for `TestRouting` (move setup from `setUpClass` to per-test `setUp`).

**Acceptance:** `bench --site payment.local run-tests --app pinelabs_fin --module pinelabs_fin.api.test_phase1` is green (15/15 pass).

### 2. Real Plural API URL mismatch

`api/plural_client.py` calls:

- `POST /api/pay/v1/payment-links`           (`create_payment_link`)
- `GET  /api/pay/v1/payment-links/merchant/…` (`get_payment_link_status`)

But the official Plural docs show:

- `POST /api/pay/v1/paymentlink`             (no hyphen, singular)
- `GET  /api/pay/v1/paymentlink/{payment_link_id}`

The current paths work against our local simulator (we wrote it to match) but will likely 404 against the real Plural sandbox. There's an explicit `NOTE` comment about this in `get_payment_link_status`.

**Acceptance:** verified in Postman against the real sandbox, paths in `plural_client.py` updated to whichever Plural actually serves, comment removed.

### 3. App metadata is placeholder

Files affected:

- `pinelabs_fin/hooks.py`:
  - `app_publisher = "pine"` → real publisher name
  - `app_description = "pine"` → one-line product description
  - `app_email = "pinelab12@gmail.com"` → support email
- `pyproject.toml`:
  - `description = "pine"` → same one-liner
  - `authors[].name = "pine"` → real author/team name

**Acceptance:** marketplace listing preview matches the real product (we can preview by running `bench publish` locally before submitting).

### 4. README is boilerplate

Currently 41 lines, single-line "pine" description, no screenshots.

The marketplace listing reads heavily from this file. Needs:

- One-paragraph **what it does**.
- **Supported flows** (Pay on Machine / Send Payment Link / Quick Net Banking) with what each does.
- **Prerequisites**: ERPNext, a Plural merchant account + Pine Labs Cloud terminal credentials.
- **Setup walkthrough**: enable in Settings → add machines → configure Payable Doctypes → Customer Contact Field Mapping → Test Connection.
- **3-5 screenshots**: Settings form (Plural section), Payable Doctypes inline grid, "Pay on Machine" dialog, "Send Payment Link" dialog, Pinelabs Transaction record.
- **Webhook setup** for Plural (URL + HMAC secret).
- **Troubleshooting**: realtime listener, scheduler must be enabled, simulator vs real Plural.
- **License** + contributor link.

**Acceptance:** README renders correctly on GitHub and the marketplace draft.

### 5. `required_apps` not declared

`hooks.py` line 11 has `# required_apps = []` (commented out).

Should be:

```python
required_apps = ["erpnext"]
```

ERPNext is a real dependency — `transaction.py:_create_payment_entry_idempotent` imports `erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry`.

**Acceptance:** declared, and a fresh-bench install pulls ERPNext automatically.

### 6. Branch + uncommitted changes

23 modified files on `phase-1-rewrite`. Marketplace points at `main`'s latest tag.

**Acceptance:**

- All cleanup committed in coherent commits.
- `phase-1-rewrite` merged to `main`.
- Tagged `v0.1.0` (or whatever we pick).
- Pushed to GitHub remote.

---

## Nice-to-haves (won't block submit, do soon after)

### License file naming

`license.txt` exists. GitHub auto-renders `LICENSE` (no extension) in the repo header. Rename for cosmetic polish.

### CI workflow

No `.github/workflows/` exists. Frappe core convention is a workflow that runs `bench run-tests` on push. Without CI, marketplace reviewers can't see test status — slows approval.

Minimal workflow:

- Trigger on `push` and `pull_request`.
- Use Frappe's official test action (`frappe/setup-bench-action`).
- Run `bench --site test_site run-tests --app pinelabs_fin`.

### CHANGELOG

`CHANGELOG.md` at the repo root. One section per version. Marketplace release notes pull from this.

### Screenshots directory

`docs/screenshots/` with the 3-5 PNGs referenced in the README.

### Phase 3.2 — also split the Cloud terminal client

`api/pinelabs_client.py` is still ~1100 lines, all Cloud-terminal. Splitting into `api/cloud_client.py` would mirror what we did for Plural and shrink the surface area further. Not blocking publication but would help long-term maintenance.

---

## Recommended execution order

1. **Tests** — fix the 6 failing test cases. Mostly fixture cleanup. ~1 hour.
2. **Plural URL** — confirm via Postman against real sandbox; update paths if needed; remove the TODO comment. ~30 min once we have Plural sandbox creds.
3. **Metadata** — update `hooks.py` + `pyproject.toml`. ~5 min.
4. **`required_apps`** — one-line change in `hooks.py`. ~2 min.
5. **README** — write proper one with screenshots. ~2 hours including screenshots.
6. **License rename** — `mv license.txt LICENSE`. ~10 sec.
7. **Commit + merge + tag**. ~15 min.
8. **CI workflow** (optional but recommended). ~30 min.
9. **Submit** via [https://frappecloud.com/marketplace/publish](https://frappecloud.com/marketplace/publish).

Total: ~half a day of focused work, plus screenshot/copy time.

---

## What's already in good shape

For context (so we don't re-do this work):

- ✅ Three-phase standards refactor done: tabs everywhere, `unicode_literals` removed, ruff clean, module docstrings, type hints on public surface, function ordering, logger consistency.
- ✅ Plural client extracted to its own module (`api/plural_client.py`).
- ✅ Migrate runs cleanly on `payment.local`; all schema patches idempotent.
- ✅ Routing tests (`TestRouting`) all pass.
- ✅ Test Connection works end-to-end against the local simulator.
- ✅ Settings form: cascading Customer Contact Field Mapping, inline Machine Mappings grid, editable Plural Base URL, Test Plural Connection button.
- ✅ Pinelabs Transaction form auto-reloads via realtime when status changes; per-minute cron reconciles missed webhooks.
- ✅ No dead files, no orphan `__pycache__`, no leaked smoke-test scripts.
