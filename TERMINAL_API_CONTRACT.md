# Pine Labs Terminal API — Request & Response Contract

Complete I/O reference for the three whitelisted terminal endpoints in
[pinelabs_fin/api/terminal.py](pinelabs_fin/api/terminal.py). For every endpoint, in order:

1. **Caller → this app** — the request the API caller sends in
2. **This app → Pine Labs machine** — the payload this app posts to the Pine Labs Cloud API
3. **Pine Labs machine → this app** — every response shape the machine can return
4. **This app → caller** — the response this app sends back

---

## Conventions

| Term | Meaning |
|---|---|
| `success: true` | "We know the outcome." Does **not** mean "your action succeeded." Always branch on `status`. |
| `status` | One of `INITIATED`, `PENDING`, `SUCCESS`, `FAILED`, `CANCELLED`. |
| `response_code` | Raw `ResponseCode` from Pine Labs (`int` or stringified). `null` when no Pine Labs call was made (e.g. cancel of a txn with no `payment_id`). |
| `transaction_name` | The `Pinelabs Transaction` row id, format `PLT-YYYY-NNNNN`. |
| `payment_id` | Pine Labs' `PlutusTransactionReferenceID` — what this app polls/cancels with. |
| Amount unit | Pine Labs expects amounts in **paisa** (integer). ₹1.00 → `100`. |
| Allowed payment modes | `1` = Card, `11` = UPI (see note below). |

> **Note on UPI mode code (`AllowedPaymentMode`)** — Pine Labs Cloud Integration Guide v1.37 defines `10` as **UPI Sale** (standard "scan and pay") and `11` as **UPI Bharat QR** (static merchant QR — a different flow). This app currently sends **`11`** for `"PineLabs - UPI"` because existing merchants' terminals are provisioned around that value. Behaviour on Pine Labs firmware:
>
> - Many Plutus terminal firmwares accept `11` and present the customer-friendly scan-QR flow regardless — which is why `11` "just works" in practice.
> - Strict spec-compliant terminals will instead present a Bharat QR screen for `11`. If you see UPI flows behaving oddly (wrong screen, merchant-not-recognised, freeze waiting for settlement), this is the cause and the fix is to send `10` for UPI Sale.
>
> The constant is in [`PINELABS_TERMINAL_MODES`](pinelabs_fin/api/transaction.py#L55).

---

## Endpoint 1 — `initiate_terminal_payment`

Method: `pinelabs_fin.api.terminal.initiate_terminal_payment`
Whitelisted: yes.

### 1.1 Caller → this app

```python
initiate_terminal_payment(
    reference_doctype: str,            # required — e.g. "Sales Invoice"
    reference_name: str,               # required — e.g. "SINV-2026-00054"
    mode_of_payment: str,              # required — "PineLabs - Card" or "PineLabs - UPI"
    amount: float = None,              # optional — defaults to source doc outstanding
    create_payment_entry: int = None,  # optional — 0 / 1 override of Payable Documents config
)
```

REST equivalent:

```http
POST /api/method/pinelabs_fin.api.terminal.initiate_terminal_payment
Content-Type: application/json

{
  "reference_doctype": "Sales Invoice",
  "reference_name": "SINV-2026-00054",
  "mode_of_payment": "PineLabs - Card",
  "amount": 159.00
}
```

### 1.2 This app → Pine Labs machine

`POST {base_url}/API/CloudBasedIntegration/V1/UploadBilledTransaction`

Headers: `Content-Type: application/json`

Body:

```json
{
  "MerchantID": "987654321012345",
  "SecurityToken": "<from Pinelabs Machine Config>",
  "ClientID": "<from Pinelabs Machine Config>",
  "StoreID": "<from Pinelabs Machine Config>",
  "TransactionNumber": "PLT-2026-00007",
  "SequenceNumber": 1,
  "AllowedPaymentMode": 1,
  "Amount": 15900,
  "TotalInvoiceAmount": 15900,
  "UserID": "operator@example.com",
  "AutoCancelDurationInMinutes": 3,
  "ForceCancelOnBack": false,
  "IsMQTTDisabled": false
}
```

Field notes:
- `TransactionNumber` is our `Pinelabs Transaction` row name (PLT-...).
- `AllowedPaymentMode`: `1` for Card, `11` for UPI — read from the Mode of Payment.
- `Amount` and `TotalInvoiceAmount`: integer paisa (rupees × 100).
- `AutoCancelDurationInMinutes`: read from `Pinelabs Machine Config.auto_cancel_duration_in_minutes`, falls back to `Pinelabs Settings.auto_cancel_duration_minutes`, then to `3`.
- `UserID`: the Frappe session user that triggered the initiate.

### 1.3 Pine Labs machine → this app

#### Accepted — terminal will process

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "APPROVED",
  "PlutusTransactionReferenceID": 1234567890
}
```

#### Rejected synchronously

```json
{ "ResponseCode": 1, "ResponseMessage": "INVALID REQUEST" }
{ "ResponseCode": 2, "ResponseMessage": "Declined" }
```

#### HTTP layer failure (timeout / connection / 5xx)

`requests` raises an exception → caught by `_handle_request_exception` →
this app sees `{"success": false, "error": "Request timeout - ...", "status_code": 0, "data": {}}`.

### 1.4 This app → caller

Every return goes through [`_shape()`](pinelabs_fin/api/terminal.py#L416) → 5-key envelope:

```json
{
  "success":          true,
  "transaction_name": "PLT-2026-00007",
  "status":           "PENDING",
  "response_code":    0,
  "message":          "APPROVED"
}
```

#### All concrete outcomes

| Trigger | `success` | `status` | `response_code` | `message` |
|---|---|---|---|---|
| Accepted, terminal processing | `true` | `"PENDING"` | `0` | Pine Labs `ResponseMessage` |
| Accepted but no `PlutusTransactionReferenceID` (Gap 4) | `false` | `"FAILED"` | `0` | `"Pine Labs accepted the transaction but returned no PlutusTransactionReferenceID..."` |
| Sync decline | `false` | `"FAILED"` | `1` or `2` | Pine Labs `ResponseMessage` |
| Unknown ResponseCode (`3`, `999`, ...) | `false` | `"FAILED"` | the unknown code | `"Terminal declined the request"` (or the `ResponseMessage` if present) |
| HTTP timeout / connection error | `false` | `"FAILED"` | `null` | The error string from `_handle_request_exception` |

---

## Endpoint 2 — `check_terminal_status`

Method: `pinelabs_fin.api.terminal.check_terminal_status`
Whitelisted: yes.

### 2.1 Caller → this app

```python
check_terminal_status(
    transaction_name: str,             # required — the PLT-... from initiate
    create_payment_entry: int = None,  # optional — late override
)
```

REST equivalent:

```http
POST /api/method/pinelabs_fin.api.terminal.check_terminal_status
Content-Type: application/json

{
  "transaction_name": "PLT-2026-00007"
}
```

### 2.2 This app → Pine Labs machine

`POST {base_url}/API/CloudBasedIntegration/V1/GetCloudBasedTxnStatus`

Body:

```json
{
  "MerchantID": "987654321012345",
  "SecurityToken": "<from Machine Config>",
  "ClientID": "<from Machine Config>",
  "StoreID": "<from Machine Config>",
  "PlutusTransactionReferenceID": 1234567890
}
```

### 2.3 Pine Labs machine → this app

#### Approved (customer paid) — the rich payload

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "APPROVED",
  "PlutusTransactionReferenceID": 1234567890,
  "TransactionData": [
    { "Tag": "PaymentMode",    "Value": "Card" },
    { "Tag": "Amount",         "Value": "15900" },
    { "Tag": "CardNumber",     "Value": "XXXXXXXXXXXX1234" },
    { "Tag": "CardType",       "Value": "VISA" },
    { "Tag": "CardHolderName", "Value": "JOHN DOE" },
    { "Tag": "BankName",       "Value": "HDFC BANK" },
    { "Tag": "RRN",            "Value": "534212345678" },
    { "Tag": "ApprovalCode",   "Value": "123456" },
    { "Tag": "TID",            "Value": "12345678" },
    { "Tag": "MID",            "Value": "987654321012345" },
    { "Tag": "InvoiceNumber",  "Value": "PLT-2026-00007" }
  ]
}
```

After parsing, [`_parse_transaction_data_array`](pinelabs_fin/api/pinelabs_client.py#L203) flattens the array onto the response dict so `response_data.RRN`, `response_data.ApprovalCode`, etc. are also top-level keys.

#### Still waiting

```json
{ "ResponseCode": 1001, "ResponseMessage": "TXN UPLOADED" }
```

#### Declined

```json
{ "ResponseCode": 1, "ResponseMessage": "Declined by issuer" }
{ "ResponseCode": 2, "ResponseMessage": "Customer cancelled" }
```

#### Unknown / undocumented

Pine Labs returned a code outside the documented set. Captured on the txn row; aged out to FAILED via `_attempt_age_out_settlement` if older than `auto_cancel_duration_in_minutes`.

### 2.4 This app → caller

Smaller envelope than initiate (no `transaction_name` — caller passed it in):

```json
{
  "success":       true,
  "status":        "PENDING",
  "response_code": 1001,
  "message":       "Waiting on terminal"
}
```

#### All concrete outcomes

| Trigger | `success` | `status` | `response_code` | `message` |
|---|---|---|---|---|
| Already terminal on entry | `true` | `txn.status` | `null` | `"terminal state"` |
| `payment_id` missing on doc | `false` | `txn.status` | `null` | `"PlutusTransactionReferenceID missing"` |
| `ResponseCode 0` — APPROVED | `true` | `"SUCCESS"` | `0` | `"Approved"` |
| `ResponseCode 1001` (within cutoff) | `true` | `"PENDING"` | `1001` | `"Waiting on terminal"` |
| `ResponseCode 1001`, **other worker raced to terminal** | `true` | `"SUCCESS"`/`"FAILED"`/`"CANCELLED"` | `1001` | `"already finalized"` |
| `ResponseCode 1` or `2` — declined | `true` | `"FAILED"` | `1` or `2` | Pine Labs `ResponseMessage` |
| **Age-out catches paid race** (Gap 2) | `true` | `"SUCCESS"` | `0` | `"Approved (caught at age-out)"` |
| **Age-out, no race** (Gap 1+2) | `true` | `"FAILED"` | (last code seen, e.g. 1001) | `"Aged out"` |
| Unknown code (within cutoff) | `true` | `txn.status` (stays `"PENDING"`) | the unknown code | Pine Labs `ResponseMessage` OR underlying error (e.g. `"Request timeout - ..."`) OR `"Unknown response code"` |

The age-out path is implemented in [`_attempt_age_out_settlement`](pinelabs_fin/api/terminal.py#L429): it sends a cancel to Pine Labs first (to stop further taps), then polls one last time. If that final poll returns `0`, the txn is marked SUCCESS; otherwise FAILED with `"Aged out"`.

**Force Cancel fallback (v1.36+).** If the regular cancel call returns a non-zero `ResponseCode` (e.g. terminal frozen, already processed), the helper additionally invokes [`force_cancel_raw`](pinelabs_fin/api/pinelabs_client.py#L440). This hits a separate Pine Labs Cloud endpoint configured via `Pinelabs API Endpoint.force_cancel_transaction_endpoint` (default `/API/CloudBasedIntegration/V1/ForceCancelTransaction`). Leave the field blank on the active endpoint row to disable the fallback. Same request shape as the regular cancel.

---

## Endpoint 3 — `cancel_terminal_payment`

Method: `pinelabs_fin.api.terminal.cancel_terminal_payment`
Whitelisted: yes.

### 3.1 Caller → this app

```python
cancel_terminal_payment(
    transaction_name: str,    # required — the PLT-...
    reason: str = None,       # optional — free-text reason, stored on the txn
)
```

REST equivalent:

```http
POST /api/method/pinelabs_fin.api.terminal.cancel_terminal_payment
Content-Type: application/json

{
  "transaction_name": "PLT-2026-00007",
  "reason": "user cancelled from modal"
}
```

### 3.2 This app → Pine Labs machine

`POST {base_url}/API/CloudBasedIntegration/V1/CancelTransaction`

Body:

```json
{
  "MerchantID": "987654321012345",
  "SecurityToken": "<from Machine Config>",
  "PlutusTransactionReferenceID": 1234567890,
  "UserID": "operator@example.com"
}
```

If the local txn has no `payment_id`, this app **skips** the Pine Labs call and just marks the row CANCELLED.

### 3.3 Pine Labs machine → this app

#### Cancel succeeded

```json
{ "ResponseCode": 0, "ResponseMessage": "CANCELLED" }
```

#### Cancel rejected (e.g. customer already paid)

```json
{ "ResponseCode": 1, "ResponseMessage": "Transaction already processed" }
{ "ResponseCode": 2, "ResponseMessage": "Cannot cancel" }
```

### 3.4 This app → caller

Full 5-key envelope (standardised — see Inconsistency 2 fix):

```json
{
  "success":          true,
  "transaction_name": "PLT-2026-00007",
  "status":           "CANCELLED",
  "response_code":    0,
  "message":          "CANCELLED"
}
```

#### All concrete outcomes

| Trigger | `success` | `status` | `response_code` | `message` |
|---|---|---|---|---|
| Txn already terminal on entry | `true` | the current status (`SUCCESS` / `FAILED` / `CANCELLED`) | `null` | `"Already terminal"` |
| Cancel succeeded | `true` | `"CANCELLED"` | `0` | Pine Labs `ResponseMessage` or `"Cancelled"` |
| Cancel rejected, status poll finds APPROVED (Gap 3) | `true` | `"SUCCESS"` | `0` | `"Cancel rejected by gateway: customer already paid. Marked SUCCESS."` |
| Cancel rejected, status NOT approved | `true` | `"CANCELLED"` | `1` or `2` (the cancel code) | Pine Labs `ResponseMessage` or `"Cancelled"` |
| No `payment_id` on the txn | `true` | `"CANCELLED"` | `null` | the supplied `reason` or `"user cancelled"` |

---

## Reference — Pine Labs `ResponseCode` dictionary

What every code means in each context:

| Code | UploadBilledTransaction (initiate) | GetCloudBasedTxnStatus (poll) | CancelTransaction |
|---|---|---|---|
| `0` | accepted → PENDING | APPROVED → SUCCESS | cancel succeeded |
| `1` | rejected → FAILED | declined → FAILED | cancel rejected (e.g. already processed) |
| `2` | rejected → FAILED | declined → FAILED | cancel rejected |
| `1001` | (not used) | still waiting → keep polling | (not used) |
| `1006` | (not used) | **TIMEOUT / NO RESPONSE FROM PLUTUS → FAILED immediately** | (not used) |
| `1008` | (not used) | **TXN VOIDED (cancelled at terminal UI / externally) → CANCELLED** | (not used) |
| other / unknown | treated as decline → FAILED | unknown → captured, aged out to FAILED if too old | logged, fall back to mark_cancelled |

---

## Reference — `TransactionData` tags this app understands

[`_parse_transaction_data_array`](pinelabs_fin/api/pinelabs_client.py#L222) flattens these from the array onto the response dict:

| Pine Labs `Tag` | Flattened key | Notes |
|---|---|---|
| `RRN` | `RRN` | Bank Retrieval Reference Number — closest thing to a payment receipt id |
| `ApprovalCode` / `Approval Code` | `ApprovalCode` | Issuer authorization code |
| `CardNumber` | `CardNumber` | Masked PAN, e.g. `XXXXXXXXXXXX1234` |
| `CardType` | `CardType` | `VISA`, `MASTER`, `RUPAY`, `UPI`, etc. |
| `CardHolderName` | `CardHolderName` | |
| `BankName` | `BankName` | Issuing bank |
| `TID` | `TID` | Terminal Id |
| `MID` | `MID` | Merchant Id |
| `PaymentMode` | `PaymentMode` | `Card`, `UPI`, etc. |
| `Amount` | `Amount` | In paisa, as a string |
| `InvoiceNumber` | `InvoiceNumber` | Echo of our `TransactionNumber` |
| `HostTxnID` | `HostTxnID` | UPI-only — NPCI/bank reference. Needed for UPI dispute filing and bank settlement matching |
| `VPA` | `VPA` | UPI-only — customer's UPI ID (e.g. `customer@upi`). Useful on receipts and for refund records |

Unknown tags are also flattened (upper-cased) so they show up in `response_payload` for debugging.

---

## Reference — Where things are stored

After each call, the `Pinelabs Transaction` row carries the full request and response:

| Pinelabs Transaction field | Source |
|---|---|
| `name` | PLT-... (the `transaction_name` returned to caller) |
| `reference_doctype` / `reference_name` | from caller input |
| `flow_type` | `"Terminal"` for these endpoints |
| `payment_method` | `"CARD"` (mode 1) or `"UPI"` (mode 11) |
| `payment_id` | Pine Labs `PlutusTransactionReferenceID` |
| `machine` | Machine that handled the call |
| `status` | The current state in the state machine |
| `request_payload` | Exact body sent to Pine Labs (JSON-stringified) |
| `response_payload` | Latest body received from Pine Labs (JSON-stringified, includes `TransactionData`) |
| `error_message` | Captured failure reason (truncated to 1000 chars) |
| `payment_entry` | Linked Payment Entry name on SUCCESS |
| `completed_at` | When the txn reached a terminal state |

---

## Reference — Caller-side conventions

When integrating with this app (custom button, wrapper API, external system):

- **Branch on `status`, not `success`.** A FAILED txn comes back as `success: true, status: "FAILED"` (we know the outcome — it failed).
- **The `transaction_name` is the only stable identifier** across initiate / poll / cancel. Store it.
- **For poll**, the caller decides the cadence. The built-in UI polls every 3s; the cron polls every 1 minute server-side.
- **For cancel**, the response can transition the txn to **SUCCESS** (not just CANCELLED) when a customer-paid race is detected. Always re-read `status` from the response.
- **The realtime channel** publishes `pinelabs_txn_update` on every state transition with payload `{transaction_name, reference_doctype, reference_name, flow_type, status, payment_id, payment_entry, error_message}` — subscribe to it for instant UI updates.
- **The frontend dialog** is auto-opened by the JS bundle for any `frappe.call` whose response includes a `transaction_name` matching `PLT-*` and `success: true`. Wrappers that pass through this field get the locked dialog for free.
- **Opting out of the dialog's internal poll** — a wrapper API that already runs its own poll against `check_terminal_status` can set `_pinelabs_external_poll: true` on the initiate response. The auto-modal then skips its own `setInterval` and relies solely on the `pinelabs_txn_update` realtime channel for state transitions. This avoids doubling the rate of upstream Pine Labs `GetCloudBasedTxnStatus` calls. Example for a Python wrapper:

  ```python
  def initiate_my_terminal_payment(...):
      result = pinelabs_fin.api.terminal.initiate_terminal_payment(...)
      if isinstance(result, dict):
          result["_pinelabs_external_poll"] = True
      return result
  ```

  The marker is opt-in — wrappers that don't set it keep the existing behaviour (modal polls + realtime).

---

## Related flows (UI-level, for context)

These are not API endpoints, but they consume the same response shapes:

- **Built-in "Pay on Machine" button** on Sales Invoice / POS Invoice / Payment Request — calls `initiate_terminal_payment`, opens the locked dialog.
- **`frappe.pinelabs.start_terminal_payment(args, options)`** JS helper — same as above for custom buttons.
- **Watch Terminal button** (Pinelabs Transaction doctype) — opens the locked dialog for an existing in-flight txn.
- **Retry Terminal button** (Pinelabs Transaction doctype + the locked dialog's failure state) — fetches the failed txn's details and re-initiates with the same source doc + Mode of Payment. Creates a NEW `Pinelabs Transaction` row.
- **Cancel button** in the locked dialog — calls `cancel_terminal_payment`.
- **1-minute reconcile cron** — calls `check_terminal_status` for every PENDING terminal txn so abandoned browser sessions still get reconciled.
