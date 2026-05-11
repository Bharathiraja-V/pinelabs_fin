# Phase 1 — Manual Testing Guide

End-to-end manual test plan for the Phase 1 rewrite. Work through the
sections in order — later tests assume earlier setup is in place.

> **Convention**
> ✅ = pass criteria (what you should observe)
> ❌ = fail criteria (what indicates the build is broken)
> 🔧 = fix actions if the test fails

---

## 0. Preflight

### 0.1 Restart bench so `hooks.py` reloads

`hooks.py` is imported once at process start. The `app_include_js` entry
that loads our buttons script won't show up in pages until the worker
restarts.

```bash
# In the terminal running `bench start`: Ctrl+C to stop, then re-run
bench start
```

If you use supervisor/production: `bench restart`.

### 0.2 Clear browser cache

Hard-refresh the desk page (`Ctrl + Shift + R` / `Cmd + Shift + R`) or
open DevTools → Network → tick **Disable cache** → reload.

### 0.3 Confirm the JS loaded

Open DevTools → Console. ✅ You see:

```
[Pinelabs] pinelabs_buttons.js loaded
[Pinelabs] registering form handler for Sales Invoice
[Pinelabs] registering form handler for POS Invoice
[Pinelabs] registering form handler for Payment Request
[Pinelabs] payable doctypes from Settings: [...]
```

❌ Nothing in console → 🔧 step 0.1 / 0.2.

### 0.4 Confirm the doctypes migrated

In the desk awesomebar, open each of these — they should exist:
- `Pinelabs Settings` (singleton)
- `Pinelabs Transaction` (list)

In awesomebar → `Mode of Payment List` → filter `Pinelabs Enabled = 1`.
✅ exactly 4 rows: PineLabs - Card / UPI / NetBanking / Wallet.

---

## 1. Settings Configuration

### 1.1 Open Pinelabs Settings

- ✅ Form renders without errors (no 404 / 500)
- ✅ Sections visible: Basic Setup → Plural Configuration → Payable Documents → Technical
- ✅ NO `Payment Modes` section anywhere (it's been removed; Mode of Payment is the source of truth)

### 1.2 Configure Basic Setup

- Tick **Enabled**
- In **Machines**, add a row:
  - Machine Name: `Test Machine`
  - Merchant ID: from your simulator config (e.g. `MID20241001`)
  - Client ID: e.g. `37639`
  - Security Token: e.g. `SIM_TOKEN_DEMO`
  - Store ID: e.g. `1221255000`
  - Enabled: ✓
  - **Is Default: ✓**

### 1.3 Validation: at most one default machine

- Add a second machine row, also tick **Is Default**.
- Save.
- ✅ Throws: *"Exactly one machine may be marked Is Default. Found 2..."*
- 🔧 Untick Is Default on one row, save again — succeeds.

### 1.4 Configure Plural

- Tick **Enable Payment Links**.
- Tick **Use Plural Sandbox**.
- Plural Client ID: `SIM_CLIENT_ID` (or your real sandbox creds)
- Plural Client Secret: `SIM_CLIENT_SECRET`
- Plural Base URL Override: `http://127.0.0.1:5000` (only if testing against the simulator)
- Payment Link Webhook Secret: any string ≥16 characters

### 1.5 Validation: webhook secret length

- Set the secret to `short`, save.
- ✅ Throws: *"Payment Link Webhook Secret must be at least 16 characters..."*
- 🔧 Use a longer secret.

### 1.6 Validation: mandatory Plural creds when enabled

- With **Enable Payment Links** ticked, blank the Client ID, save.
- ✅ Frappe enforces *"Plural Client ID is required"* (via `mandatory_depends_on`).

### 1.7 Auto-fill webhook URL

- After saving with Plural enabled, ✅ the **Webhook URL** field auto-fills with `https://<your-site>/api/method/pinelabs_fin.api.webhook.handle_webhook`.

### 1.8 Configure Payable Documents

- In **Payable Doctypes** child table, add row:
  - DocType: `Sales Invoice`
  - Button label: `Pay via Pinelabs` (default)
  - Show on docstatus: `1`
  - Enabled: ✓
  - Amount Field: `outstanding_amount`
  - Status Field: leave blank (default PE creation handles it)
  - Paid Status Value: leave blank
  - Transaction Reference Field: leave blank
  - Create Payment Entry: ✓

- ✅ Save succeeds.

### 1.9 Validation: amount_field required

- Add a second row with DocType but no Amount Field.
- Save.
- ✅ Throws: *"Payable Doctype X requires an Amount Field."*

### 1.10 Validation: mapped finalization needs status fields

- Add a row, set DocType, Amount Field, **untick** Create Payment Entry, leave Status Field blank.
- Save.
- ✅ Throws: *"... with Create Payment Entry unchecked, both Status Field and Paid Status Value are required."*

### 1.11 Validation: duplicate row

- Add two rows for the same DocType.
- Save.
- ✅ Throws: *"Duplicate Payable Doctype row for X."*

### 1.12 Validation: Payment Entry is allowed (no longer forbidden)

- Add a row with DocType = `Payment Entry`, Amount Field = `paid_amount`, untick Create Payment Entry, set Status Field = `status`, Paid Status Value = `Submitted`.
- Save.
- ✅ Save succeeds (the previous hard-block was removed).

---

## 2. Mode of Payment Sanity Check

For each of the 4 PineLabs modes, open the Mode of Payment record:

| Mode | Flow Type | Terminal Code | Plural Method |
|---|---|---|---|
| PineLabs - Card | Both | 1 | CARD |
| PineLabs - UPI | Both | 11 | UPI |
| PineLabs - NetBanking | Plural | 0 | NB |
| PineLabs - Wallet | Plural | 0 | WALLET |

✅ All four show `Pinelabs Enabled = 1`.

No further per-mode setup needed — the install patch ships them ready
to use.

---

## 3. Terminal Flow (Card)

### 3.1 Setup

- Start the Pine Labs simulator: `cd /home/finstein-emp/pinelab/pinelab_simulator/server && python3 app.py`
- Confirm Pinelabs Settings → Machine row has the simulator's merchant credentials.
- Optionally: in Pinelabs Settings → API Endpoints, add overrides pointing at `http://127.0.0.1:5000`.

### 3.2 Open a submitted Sales Invoice with outstanding > 0

- Customer: `Palmer Productions Ltd.` (or any test customer)
- Items: any line item; total > 0
- Submit.

### 3.3 Buttons render

- ✅ Browser console logs `[Pinelabs] rendering Pine Labs buttons on Sales Invoice ACC-SINV-... amount= NN`.
- ✅ In the form toolbar, click the **actions kebab** (three dots top-right next to Cancel) → see a **Pine Labs** group with `Pay on Machine` and `Send Payment Link`.

### 3.4 Pay on Machine — happy path

- Click `Pay on Machine`.
- ✅ Dialog opens with a Mode of Payment Link field that lists ONLY `PineLabs - Card` and `PineLabs - UPI` (Plural-only modes are filtered out).
- Pick `PineLabs - Card` → Submit.
- ✅ A polling modal opens: *"Waiting for terminal..."* with a spinner.
- In the simulator UI, approve the transaction.
- Within 3-6 seconds:
  - ✅ Modal closes.
  - ✅ Toast: *"Payment SUCCESS"* (green).
  - ✅ Sales Invoice reloads; Status flips to `Paid`.
- Open `Pinelabs Transaction List` → ✅ a new `PLT-2026-#####` row, status `SUCCESS`, flow_type `Terminal`, payment_method `CARD`, mode_of_payment `PineLabs - Card`, payment_entry populated.
- Open the linked Payment Entry → ✅ submitted, mode_of_payment = `PineLabs - Card`, reference_no = the PlutusTransactionReferenceID, paid_amount matches.

### 3.5 Pay on Machine — cancel from modal

- Repeat 3.4 up to the polling modal step.
- Click **Cancel** in the polling modal.
- ✅ Modal closes; Sales Invoice reloads.
- ✅ Pinelabs Transaction status = `CANCELLED`.
- ✅ No Payment Entry created.

### 3.6 Pay on Machine — simulator declines

- Configure simulator to auto-decline (depends on simulator UI).
- Run a payment.
- ✅ Transaction goes to `FAILED`; error_message populated.
- ✅ Toast: *"Payment FAILED"* (red).
- ✅ No Payment Entry created.

### 3.7 Idempotency: cannot run two terminal payments at once

- After step 3.4, while modal is open, attempt to click `Pay on Machine` again on the same SI in another tab/window.
- ✅ Backend rejects: *"A Terminal payment (PLT-...) is already in progress for this document."*

---

## 4. Plural Pay-by-Link Flow (All Methods)

### 4.1 Setup

- Plural sandbox credentials configured (see 1.4).
- Plural webhook URL from Settings is reachable from your simulator/Plural account.
- Customer on the SI has either an email or mobile (otherwise the backend rejects with *"Customer email or mobile is required..."*).

### 4.2 Generate link

- Open a fresh submitted Sales Invoice.
- Click `Send Payment Link` → dialog opens with Flow selector defaulted to `all_methods` and a hidden Bank field.
- Click **Continue**.
- ✅ Toast/msgprint: *"Payment Link Generated"* with a clickable URL.
- ✅ Pinelabs Transaction List shows a new row, status `PENDING`, flow_type `Plural`, order_id populated, payment_entry blank.

### 4.3 Customer pays (simulator)

- Open the link. The simulator presents a payment selection page.
- Pick UPI / Card / Net Banking — pay.
- The simulator fires a webhook to your site.
- Within ~5 seconds: ✅ Pinelabs Transaction flips to `SUCCESS`, payment_method populated (e.g. `UPI`), payment_entry linked.
- ✅ Sales Invoice status → `Paid`.

### 4.4 Webhook idempotency

- From the simulator, manually re-fire the same `ORDER_PROCESSED` webhook for the same order.
- ✅ Server returns 200 immediately; no second Payment Entry created.
- ✅ Pinelabs Transaction stays in `SUCCESS`.

### 4.5 Method-restricted flows

- Repeat 4.2 with Flow = `card_only`. The Plural request payload should include `allowed_payment_methods: ["CARD"]`. The customer's link page should only show Card.
- Repeat with `upi_only` / `nb_only`.
- ✅ Pinelabs Transaction's `payment_method` field is pre-filled (CARD / UPI / NB) on initiate, before the customer pays.

---

## 5. Quick Net Banking (seamless flow)

### 5.1 Generate quick-NB request

- On a submitted SI: `Send Payment Link` → Flow = `quick_nb`.
- A free-form **Bank Code** field appears with a description listing common NB codes (NB1004 Axis / NB1007 HDFC / NB1016 ICICI / NB1531 SBI / NB1565 Kotak).
- Type `NB1007` → Continue.
- ✅ Pinelabs Transaction created with flow_type `Direct NB`, bank_code `NB1007`, payment_method `NB`, status `PENDING`.
- ✅ A new tab opens to the bank's auth URL (returned by Plural seamless API).

### 5.2 Customer authenticates

- In the auth tab, the bank/simulator authenticates the user.
- The bank redirects to `/api/method/pinelabs_fin.api.plural.handle_quick_nb_callback?...`.
- ✅ Browser lands back on the Sales Invoice with `?pinelabs_status=PENDING` (or SUCCESS once webhook arrives).
- Webhook arrives → ✅ Pinelabs Transaction → `SUCCESS`.

### 5.3 Sandbox-only note

- The local Pine Labs simulator does NOT implement the seamless `POST /api/pay/v1/payments` endpoint at the time of writing. Test 5.1/5.2 against Plural sandbox or extend the simulator first. If you must test locally, override the path in `site_config.json`:

```json
{
  "pine_plural_seamless_payments_path": "/api/pay/v1/your-stub-path"
}
```

---

## 6. Webhook Signature Validation

These tests are most easily run with `curl` against your dev site.

### 6.1 Missing signature → 401

```bash
curl -i -X POST http://your-site/api/method/pinelabs_fin.api.webhook.handle_webhook \
  -H 'Content-Type: application/json' \
  -d '{"event_type":"ORDER_PROCESSED","data":{"order_id":"x"}}'
```

✅ HTTP 401 with `"Missing X-Verify header"`.

### 6.2 Bad signature → 401

```bash
curl -i -X POST http://your-site/api/method/pinelabs_fin.api.webhook.handle_webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Verify: 0000000000000000000000000000000000000000000000000000000000000000' \
  -d '{"event_type":"ORDER_PROCESSED","data":{"order_id":"x"}}'
```

✅ HTTP 401 with `"Invalid signature"`.

### 6.3 Valid signature, unknown order → 404

```bash
SECRET="your-actual-webhook-secret-from-settings"
BODY='{"event_type":"ORDER_PROCESSED","data":{"order_id":"no-such-order"}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -i -X POST http://your-site/api/method/pinelabs_fin.api.webhook.handle_webhook \
  -H 'Content-Type: application/json' \
  -H "X-Verify: $SIG" \
  -d "$BODY"
```

✅ HTTP 404 with `"No Pinelabs Transaction matches no-such-order"`.

### 6.4 Secret unconfigured → fail closed

- Temporarily blank `payment_link_webhook_secret` in Settings (just for this test).
- Hit the endpoint with any signature.
- ✅ HTTP 401 with `"Webhook secret not configured; refusing webhook."`
- 🔧 Restore the secret.

---

## 7. Mapped-Field Finalization (Create Payment Entry = 0)

### 7.1 Configure a custom doctype with mapped fields

- In Pinelabs Settings → Payable Documents, add a row:
  - DocType: `Pinelabs Test Order` (or any custom doctype with a `payment_status` Data field)
  - Amount Field: `total_amount` (or whatever your doctype uses)
  - Create Payment Entry: ❌ (uncheck)
  - Status Field: `payment_status`
  - Paid Status Value: `Paid`
  - Transaction Reference Field: `pinelabs_transaction_reference` (optional)

### 7.2 Run a terminal payment against this doctype

- Submit a Pinelabs Test Order, click `Pay on Machine`, complete the flow.
- ✅ On success, the Test Order's `payment_status` field updates to `Paid`.
- ✅ If you set a Transaction Reference Field, it now contains the `PLT-...` id.
- ✅ NO Payment Entry created.
- ✅ Pinelabs Transaction status is `SUCCESS`, `payment_entry` field is empty.

### 7.3 Submittable doctype: paid_status_value = "Submitted"

- For a draft submittable doctype (e.g. one of your custom ones), set Paid Status Value = `Submitted`.
- Run a terminal payment on a draft instance.
- ✅ On success, the source document gets submitted (`docstatus = 1`) automatically.

---

## 8. State Machine Enforcement

These verify the controller and service-layer guards in `transaction.py`
and `pinelabs_transaction.py`.

### 8.1 Cannot manually edit status to SUCCESS from the form

- Open any `Pinelabs Transaction` in `INITIATED` or `PENDING` state.
- Change `Status` to `SUCCESS` and save.
- ✅ Throws: *"Status SUCCESS may only be set by the Pinelabs transaction service, not manually."*

### 8.2 Cannot edit flow_type after creation

- Open any Pinelabs Transaction.
- Try to change `Flow Type` (e.g. Plural → Terminal) and save.
- ✅ Throws: *"flow_type is locked after creation..."*

### 8.3 Cannot delete a transaction with linked Payment Entry

- Find a transaction with `payment_entry` populated.
- Try to delete it.
- ✅ Throws: *"Cannot delete Pinelabs Transaction PLT-...: linked to Payment Entry ..."*

### 8.4 Illegal status transitions rejected

- Set up a transaction in `PENDING`.
- Manually set status to `INITIATED` (via console: `frappe.set_value('Pinelabs Transaction', 'PLT-xxxx', 'status', 'INITIATED')`).
- ✅ Throws: *"Illegal status transition PENDING → INITIATED..."* (the controller's transition map disallows it).

---

## 9. Pinelabs Transaction Record UI

### 9.1 Status pill

- Open a Pinelabs Transaction in the form view.
- ✅ Headline shows a colored pill matching the status (gray INITIATED / orange PENDING / green SUCCESS / red FAILED / darkgray CANCELLED) plus the flow_type and payment_method.

### 9.2 Buttons appear conditionally

| State | Buttons that should appear |
|---|---|
| Any | View Source Document |
| `payment_entry` set | View Payment Entry |
| Plural/Direct NB + (PENDING or INITIATED) | Refresh Status |
| Terminal + (INITIATED or PENDING) | Cancel Terminal |
| FAILED + created less than 24h ago | Retry on Source Document |

### 9.3 Refresh Status (Plural)

- Open a Plural transaction in `PENDING`.
- Click **Refresh Status**.
- ✅ Backend hits Plural's `get_payment_link_status`, status updates if Plural reports a new state.

### 9.4 Cancel Terminal

- Open a Terminal transaction in `PENDING` (you may need to use the simulator to create one and not approve it).
- Click **Cancel Terminal** → confirm.
- ✅ Backend calls Pine Labs `CancelTransaction`, transaction flips to `CANCELLED`.

---

## 10. Edge Cases & Errors

### 10.1 No default machine

- In Settings, untick Is Default on every machine. Save (it'll throw at validate or pass if you have no machines).
- Try a terminal payment on a SI.
- ✅ Throws: *"No default Pinelabs machine configured. Mark exactly one machine as Is Default..."*

### 10.2 Customer missing email AND mobile

- Create a Customer with no email/mobile, no contacts.
- Submit a SI for that customer.
- Click `Send Payment Link`.
- ✅ Throws: *"Customer email or mobile is required to send a payment link."*

### 10.3 Mode of Payment with wrong flow_type

- Pick `PineLabs - NetBanking` (flow_type = Plural) for the terminal "Pay on Machine" dialog.
- The dropdown should NOT show this mode (filtered by the dialog's `get_query`). If you bypass the filter:
- ✅ Backend throws: *"Mode of Payment X cannot be used for this button..."*

### 10.4 Mode of Payment with terminal_code = 0

- Pick `PineLabs - NetBanking` for terminal flow (terminal_code = 0).
- ✅ Backend throws: *"Mode of Payment X has invalid terminal code 0..."*

### 10.5 Plural credentials wrong

- Set Plural Client Secret to `wrong-value`, save.
- Try `Send Payment Link`.
- ✅ Returns: *"Plural credentials missing in Pinelabs Settings."* or auth API returns 401, surfaced as the transaction's error_message.
- ✅ Pinelabs Transaction goes to `FAILED`, no link generated.

---

## 11. Tests (automated)

```bash
bench --site payment.local run-tests --app pinelabs_fin --module pinelabs_fin.api.test_phase1
```

Expected: 11+ tests pass covering state machine, PE guards, signature
validation, terminal initiate paths.

---

## 12. Quick smoke-test script (post-checklist)

If you've worked through sections 1–4 and everything is green, this
is a 60-second smoke check you can run any time:

1. Open a fresh submitted Sales Invoice.
2. `Pay on Machine` → Card → simulator approve → ✅ Payment Entry created, SI status = Paid.
3. Open a different fresh SI.
4. `Send Payment Link` → all_methods → simulator pay → ✅ webhook flips transaction to SUCCESS, PE created.
5. Replay the same webhook from the simulator → ✅ no second PE.
6. ✅ All three buttons under Pine Labs group still appear on a third fresh SI.

If all six pass, the Phase 1 build is healthy.

---

## Recovery / rollback

If anything goes irrecoverably wrong on `payment.local`, restore from
the stash that captured the prior working tree:

```bash
git stash list                       # confirm stash@{0} is still there
git switch feature-MOP
git stash pop                        # restores the prior state
bench --site payment.local migrate
```

The stash named `phase-1-rewrite-snapshot` carries everything we threw
away when we cut this branch from `develop`.
