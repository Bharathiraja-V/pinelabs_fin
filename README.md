# Pinelabs Fin

`pinelabs_fin` is a Frappe/ERPNext app that connects ERP documents to Pine Labs payment terminals.

## Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app pinelabs_fin
```

## PineLabs App Behavior and Request/Response Flow

## 1. What This App Does

Current responsibility of the app:

1. Read the configurable `Payable Doctypes` child table in `Pinelabs Settings` to decide where built-in Pinelabs pay buttons appear on ERPNext and custom Frappe forms.
2. Expose a shared, API-first Pine Labs payment flow that can be used by built-in form buttons, custom pages, or external apps.
3. Validate source-document eligibility through `get_trigger_context` before terminal payment starts.
4. Store request and response data in `Pinelabs Transaction Log`.
5. Poll Pine Labs for status or receive a webhook callback.
6. Update the transaction log with terminal response and reconciliation data.
7. Finalize the document either through standard ERPNext `Payment Entry` creation or through mapped field updates, depending on the configured payable-doctype row.
8. For custom doctypes that need doctype-specific post-payment behavior, defer lifecycle transitions to the doctype controller through a custom handler instead of hardcoding that behavior in the shared adapter.

## 2. Where This App Works

Current frontend asset:

1. Shared helper script loaded globally on Desk through `app_include_js`
2. Dynamic form-button injector loaded globally on Desk through `app_include_js`

Current app-owned trigger point:

1. Built-in form toolbar buttons configured through `Pine Labs Settings > Payable Doctypes`
2. Explicit shared-helper calls from custom scripts or external apps

Current standard fallback source documents for direct API callers:

1. `Sales Invoice`
2. `POS Invoice`
3. `Payment Entry`

Custom doctypes can be used by adding an enabled row in `Pine Labs Settings > Payable Doctypes` with an amount field, status mapping, and finalization mode.

Current shared frontend API:

1. `run_mode_of_payment_api(opts)`
2. `trigger(opts)`
3. `get_enabled_modes()`
4. `get_mode_config(mode_of_payment)`
5. `get_upi_mode_config()`

Current backend integration points:

1. Trigger context evaluation
2. Payment initiation
3. Payment status polling
4. Payment cancellation
5. Webhook processing
6. ERPNext finalization through Payment Entry creation or mapped field updates
7. Doctype-owned payment-success hook for custom doctypes

## 3. Frontend Behavior

Current frontend behavior supports both built-in form buttons and explicit callers.

Current event path:

1. User clicks a configured Pinelabs form button, or another caller invokes the shared PineLabs helper explicitly.
2. Frontend resolves the active PineLabs payment modes.
3. If multiple modes are active, frontend opens a picker such as `Card` or `UPI`.
4. Frontend calls `get_trigger_context`.
5. Backend validates whether the document is eligible for PineLabs.
6. If valid, frontend starts PineLabs terminal payment.
7. Frontend opens polling dialog.
8. Frontend continues status/cancel/success flow.

Important note:

- Built-in trigger buttons are injected through global Desk JS, not through `doctype_js`.
- Built-in button rows are configured in `Pine Labs Settings > Payable Doctypes`.
- External callers can still render their own button and invoke the same shared APIs directly.
- Current code validates first through `get_trigger_context`, then starts terminal communication.
- Status, cancel, webhook, and finalization flows continue through the same backend APIs.
- In the built-in Desk flow, the source form is reloaded after a successful payment so the latest server-side status and mapped fields are visible immediately.

## 4. End-to-End Flow

### 4.1 Trigger Context Flow

1. A caller prepares doctype, document name, and mode-of-payment input.
2. Frontend or another caller invokes `get_trigger_context(doctype, docname, mode_of_payment, context)`.
3. Backend checks:
   - is PineLabs enabled
   - is at least one machine enabled
   - is selected mode a PineLabs mode
   - is document valid
   - is amount payable
   - is document not cancelled
   - if a payable-doctype row exists, whether the document matches the configured docstatus and field mapping
4. Backend returns trigger context.
5. The caller decides whether to continue into payment initiation.

### 4.2 Initiate Payment Flow

1. A caller provides PineLabs `mode_of_payment`.
2. Frontend validates PineLabs eligibility through `get_trigger_context`.
3. Frontend or another caller calls `initiate_payment`.
4. Backend resolves source document and payment mode.
5. Backend calls the service layer.
6. Service layer validates context again.
7. Service layer creates Pine Labs client.
8. Pine Labs client builds request payload.
9. App sends request to Pine Labs `UploadBilledTransaction`.
10. App creates a `Pinelabs Transaction Log`.
11. App stores request payload and immediate response payload.
12. App returns initiate result to the caller.

### 4.3 Status Polling Flow

1. Caller receives successful initiation.
2. If using the shared JS flow, frontend opens a waiting dialog.
3. Caller polls `check_payment_status(transaction_reference)`.
4. Backend loads transaction log.
5. If status is still pending, backend calls Pine Labs `GetCloudBasedTxnStatus`.
6. App updates transaction log using Pine Labs status response.
7. If approved, backend finalizes the ERPNext flow.
8. Backend returns consolidated status response to the caller.

### 4.4 Cancel Flow

1. User clicks `Cancel Payment` in the dialog.
2. Frontend sends `cancel_payment(transaction_reference)`.
3. Backend loads transaction log.
4. Backend extracts `PlutusTransactionReferenceID`.
5. App sends request to Pine Labs `CancelTransaction`.
6. App marks transaction log as cancelled.
7. Backend returns cancellation result to the caller.

### 4.5 Webhook Flow

1. Pine Labs sends POST request to app webhook endpoint.
2. App validates and parses incoming webhook data.
3. App matches webhook with transaction log using `TransactionNumber`.
4. App updates status and payment details in transaction log.
5. If approved, app tries to finalize ERPNext payment flow.
6. App returns webhook processing result.

### 4.6 ERPNext Finalization Flow

1. Transaction becomes `Approved`.
2. App resolves source document from transaction log.
3. App checks that source document exists and is eligible for the configured finalization path.
4. App reads the payable amount from the configured `amount_field` when a payable-doctype row exists, otherwise it uses the standard fallback logic.
5. App resolves ERPNext `mode_of_payment`.
6. App resolves bank account from `Mode of Payment Account`.
7. If `Create Payment Entry` is checked, app attempts ERPNext `Payment Entry` creation for source flows that finalize through the standard accounting path.
8. If `Create Payment Entry` is unchecked, app treats `status_field == paid_status_value` as the mapped paid-state check and writes the configured mapped fields on the source document after approval.
9. In mapped finalization, when `status_field` is the framework-managed `status` field and `paid_status_value` is `Submitted`, app first tries the real document transition and then recalculates the doctype status through ERPNext/Frappe logic.
10. When `link_payment_entry_field` is configured, app writes the Pine Labs transaction reference back to the source document for traceability.
11. For custom doctypes that expose an `apply_pinelabs_payment_success(transaction_log=None, config=None)` controller method, the adapter calls that doctype-owned handler first and then falls back to shared mapped-field updates for any remaining values.

## 5. Which External or Other App Integrations Are Possible

This app is designed to support both standard ERPNext form usage and API-first integrations. Another app or external caller can reuse the same business logic in two ways:

1. By calling whitelisted Frappe endpoints:
   - `pinelabs_fin.api.pos_endpoints.get_trigger_context`
   - `pinelabs_fin.api.pos_endpoints.initiate_payment`
   - `pinelabs_fin.api.pos_endpoints.check_payment_status`
   - `pinelabs_fin.api.pos_endpoints.cancel_payment`
   - `pinelabs_fin.api.pos_endpoints.get_pinelabs_payment_modes`
   - `pinelabs_fin.api.pos_endpoints.get_payable_doctypes`
   - `pinelabs_fin.api.pos_endpoints.get_payment_settings`
   - `pinelabs_fin.api.pos_endpoints.is_pinelabs_available`
   - `pinelabs_fin.api.pos_endpoints.get_available_machines`
   - `pinelabs_fin.api.pos_endpoints.test_credentials`
   - `pinelabs_fin.api.webhooks.get_webhook_url`

2. By importing shared backend functions from the service layer:
   - `evaluate_pinelabs_context`
   - `handle_mode_of_payment_trigger`
   - `get_payment_status`
   - `cancel_payment`
   - `handle_success`
   - `handle_failure`

That means another custom app can pass request data into this app and continue the same Pine Labs flow without duplicating terminal logic.

## 5.1 Configuring Built-in Form Buttons

Go to `Pine Labs Settings` and open the `Payable Doctypes` child table. Add one enabled row for each doctype where you want the built-in Pinelabs pay button to appear.

Display config fields:

- `Doctype`: Link to `DocType`
- `Button label`: custom toolbar label, default is `Pay via Pinelabs`
- `Show on docstatus`: `0`, `1`, or `0,1`
- `Enabled`: toggle to include or exclude the row

Field mapping fields:

- `Amount field`: source document field that holds the payable amount, such as `outstanding_amount`, `grand_total`, or `payable_amount`
- `Status field`: source document field used to detect and update paid state, such as `status` or `payment_status`
- `Paid status value`: value written to `Status field` after success when not creating a Payment Entry, such as `Paid` or `Completed`
- `Transaction reference field`: optional source document field that stores the Pine Labs transaction reference after payment success
- `Create Payment Entry`: when checked, the app runs the standard ERPNext Payment Entry finalization path; when unchecked, the app updates the mapped source-document fields only

Settings UI behavior:

- Select `Doctype` first. The settings form then loads valid options for `Show on docstatus`, `Amount field`, `Status field`, and `Transaction reference field` from that doctype.
- `Show on docstatus` is doctype-aware: submittable doctypes can use `0`, `1`, or `0,1`; non-submittable doctypes use `0` only.
- When you select `Status field`, the form loads `Paid status value` from that field's dropdown options, if the field has defined options.
- The `Transaction reference field` picker is limited to text-style fields intended to hold a Pine Labs transaction reference string.

Behavior after configuration:

- The frontend injector reads these rows through `pinelabs_fin.api.pos_endpoints.get_payable_doctypes` and adds the button in the form toolbar under the `Pinelabs` group.
- If `Show on docstatus = 0`, the button appears on saved draft documents.
- If `Show on docstatus = 1`, the button appears on submitted documents.
- If `Show on docstatus = 0,1`, the button appears on draft or submitted documents.
- Cancelled documents are always excluded.
- The frontend reads the payment amount from the configured `Amount field` and blocks the click if the value is missing or not greater than zero.
- Clicking the button opens a payment-mode picker when multiple Pine Labs modes are active, such as `Card` and `UPI`.
- If only one Pine Labs mode is enabled, the flow starts immediately without showing the picker.
- Clicking the button does not bypass backend validation. The app still calls `pinelabs_fin.api.pos_endpoints.get_trigger_context` before payment starts, so only eligible documents proceed.
- After terminal approval, the app uses the row mapping to either create a `Payment Entry` or update the mapped paid-state fields.
- In mapped-only flows, the backend treats `Status field == Paid status value` as the already-paid check for future retries.
- In mapped-only flows, if `Status field` is `status` and `Paid status value` is `Submitted`, the backend attempts the framework transition first instead of forcing a fake status value onto a managed doctype.
- If `Transaction reference field` is configured, the backend writes the Pine Labs transaction reference for both Payment Entry and mapped-only flows.
- After approval in the built-in Desk flow, the current form reloads automatically so the user sees the refreshed document state.
- When a custom doctype needs submit/save behavior that belongs to that doctype, implement `apply_pinelabs_payment_success(transaction_log=None, config=None)` in the doctype controller. The shared adapter will call it before applying generic mapped-field fallback logic.

Important migration note:

- Older rows that used `Show when = Outstanding > 0` or `Always` must be manually reconfigured with `Show on docstatus` and the mapping fields.
- `Create Payment Entry` is supported only for doctypes handled by the existing ERPNext Payment Entry bridge, currently `Sales Invoice` and `POS Invoice`.
- Custom doctypes should usually use `Create Payment Entry = 0`, then configure `Amount field`, `Status field`, and `Paid status value`.
- The settings form validates that the configured doctype exists, is not a child table or single doctype, and that `Amount field` is numeric.

Example rows:

| Doctype | Show on docstatus | Amount field | Status field | Paid status value | Create Payment Entry |
| --- | --- | --- | --- | --- | --- |
| Sales Invoice | `1` | `outstanding_amount` | `status` | `Paid` | checked |
| POS Invoice | `1` | `outstanding_amount` | `status` | `Paid` | checked |
| Custom Fee Form | `0,1` | `payable_amount` | `payment_status` | `Completed` | unchecked |

## 5.2 Integrating from a Custom Page or External App

For custom pages or external apps, the integration stays API-first: you render your own button or picker and call the same shared Pinelabs APIs the standard form flow uses. In the current codebase, the relevant whitelisted methods for payment initiation live under `pinelabs_fin.api.pos_endpoints.*`. The webhook URL helper lives under `pinelabs_fin.api.webhooks.get_webhook_url`.

### Step 1 — check availability

Call `pinelabs_fin.api.pos_endpoints.is_pinelabs_available` to confirm the integration is available. It returns `{ available, enabled, active_endpoint }`.

### Step 2 — fetch modes

Call `pinelabs_fin.api.pos_endpoints.get_pinelabs_payment_modes` to fetch the active modes. It returns rows shaped like `{ mode_of_payment, label, allowed_payment_mode }`. Render these in your own UI as buttons, chips, or a picker.

If you want to mirror the app-owned built-in button configuration, call `pinelabs_fin.api.pos_endpoints.get_payable_doctypes` to fetch the enabled doctype rows from `Pine Labs Settings`. Each row includes `show_on_docstatus`, `amount_field`, `status_field`, `paid_status_value`, `link_payment_entry_field`, and `finalize_via_payment_entry`. The `link_payment_entry_field` value represents the source field where the Pine Labs transaction reference should be written.

### Step 3 — initiate and poll

Call `pinelabs_fin.api.pos_endpoints.initiate_payment` with `source_doctype`, `source_docname`, `mode_of_payment`, and `amount`. If a payable-doctype row exists, the backend reloads that row and uses the configured `amount_field` as the authoritative amount. Use the returned `transaction_reference` to poll `pinelabs_fin.api.pos_endpoints.check_payment_status` every 3 seconds. When `status = "Approved"`, the app has already finalized the configured flow, either by creating a `Payment Entry` or by writing the mapped status and trace fields. For framework-managed `status` fields, the backend lets the doctype's own business logic drive the final status whenever possible.

```js
async function sendToPinelabs(doctype, docname, amount) {
  const api = "pinelabs_fin.api.pos_endpoints";
  const available = (await frappe.call({ method: `${api}.is_pinelabs_available` })).message;
  if (!available?.available) return frappe.msgprint(__("Pine Labs is not available."));
  const modes = (await frappe.call({ method: `${api}.get_pinelabs_payment_modes` })).message || [];
  const mode = modes[0];
  const init = (await frappe.call({
    method: `${api}.initiate_payment`,
    args: { source_doctype: doctype, source_docname: docname, mode_of_payment: mode.mode_of_payment, amount },
  })).message;
  const ref = init?.transaction_reference;
  const timer = setInterval(async () => {
    const status = (await frappe.call({
      method: `${api}.check_payment_status`,
      args: { transaction_reference: ref },
    })).message;
    if (status?.status === "Approved") { clearInterval(timer); cur_frm.reload_doc(); }
  }, 3000);
}
```

If the user cancels the dialog or your page times out, call `pinelabs_fin.api.pos_endpoints.cancel_payment(transaction_reference)` so the app can forward the cancel request to Pine Labs and close the transaction cleanly. If your integration also needs the webhook method URL, call `pinelabs_fin.api.webhooks.get_webhook_url`.

## 5.3 Custom Doctype Controller Hook

For custom doctypes, keep document lifecycle behavior in the doctype controller whenever payment success should do more than write a field value.

Current shared adapter behavior:

1. For mapped-only flows, the shared adapter updates the configured fields using standard Frappe document APIs.
2. If the doctype exposes a controller method named `apply_pinelabs_payment_success(transaction_log=None, config=None)`, the adapter calls that method first.
3. The doctype controller can then decide whether to:
   - call `self.submit()`
   - call `self.save()`
   - update read-only or trace fields through `self.db_set(..., notify=True)`
   - leave the document in draft and only change its custom payment fields

Why this is the preferred pattern:

- `docstatus` belongs to Frappe and the doctype controller, not to the shared Pinelabs mapping row
- doctype-specific business rules stay with that doctype
- the shared payment adapter remains reusable across ERPNext doctypes and custom doctypes

Current example in this app:

- [pinelabs_test_order.py](/home/finstein-emp/bench-bulb/apps/pinelabs_fin/pinelabs_fin/pinelabs_fin/doctype/pinelabs_test_order/pinelabs_test_order.py)

That example shows how a custom doctype can:

- set its own mapped payment status
- write the Pine Labs transaction reference
- decide whether a successful payment should submit the document

## 6. Request and Response Data

Below are the current request and response shapes used by the app.

These are taken from the current code behavior. Some fields may be optional depending on the calling doctype.

## 7. Request: `get_trigger_context`

### Request

```json
{
  "doctype": "Payment Entry",
  "docname": "ACC-PAY-2026-0001",
  "mode_of_payment": "PineLabs - Card",
  "context": null
}
```

### Response

```json
{
  "trigger_style": "manual",
  "can_trigger": true,
  "reason": null,
  "payable_amount": 1200.0,
  "selected_mode": "PineLabs - Card",
  "selected_mode_config": {
    "mode_of_payment": "PineLabs - Card",
    "label": "Card",
    "allowed_payment_mode": 1,
    "icon": "",
    "is_enabled": 1
  },
  "payment_modes": [
    {
      "mode_of_payment": "PineLabs - Card",
      "label": "Card",
      "allowed_payment_mode": 1,
      "icon": "",
      "is_enabled": 1
    }
  ],
  "doc_state": "draft",
  "payable_config": {
    "doctype_name": "Payment Entry",
    "button_label": "Pay via Pinelabs",
    "show_on_docstatus": "0",
    "amount_field": "paid_amount",
    "status_field": "status",
    "paid_status_value": "Paid",
    "link_payment_entry_field": null,
    "finalize_via_payment_entry": 0
  }
}
```

### Meaning of response fields

- `trigger_style`: backend hint only; the app does not currently auto-bind this to a doctype trigger
- `can_trigger`: whether payment may start
- `reason`: validation reason if not eligible
- `payable_amount`: amount to charge
- `selected_mode`: selected PineLabs ERPNext mode of payment
- `selected_mode_config`: full mode config
- `payment_modes`: all enabled PineLabs modes
- `doc_state`: `draft`, `submitted`, `cancelled`, or `null`
- `payable_config`: enabled payable-doctype row used for mapped validation, or `null` when direct API fallback logic is used

## 8. Request: `get_pinelabs_payment_modes`

### Request

```json
{}
```

### Response

```json
[
  {
    "mode_of_payment": "PineLabs - Card",
    "label": "Card",
    "allowed_payment_mode": 1,
    "icon": ""
  },
  {
    "mode_of_payment": "PineLabs - UPI",
    "label": "UPI",
    "allowed_payment_mode": 11,
    "icon": ""
  }
]
```

## 9. Request: `initiate_payment`

### Request

```json
{
  "invoice_name": "ACC-PAY-2026-0001",
  "amount": 1200.0,
  "payment_mode": "Card",
  "allowed_payment_mode": 1,
  "mode_of_payment": "PineLabs - Card",
  "source_system": "erpnext",
  "source_doctype": "Payment Entry",
  "source_docname": "ACC-PAY-2026-0001",
  "machine_name": "Terminal-1",
  "context": null
}
```

### Success Response

```json
{
  "success": true,
  "transaction_reference": "TXN20260409123456ABCD1234",
  "transaction_log": "PINLOG-0001",
  "message": "Payment initiated. Please complete payment on terminal.",
  "amount": 1200.0,
  "payment_mode": "Card",
  "mode_of_payment": "PineLabs - Card",
  "allowed_payment_mode": 1,
  "invoice_name": "ACC-PAY-2026-0001",
  "invoice_doctype": "Payment Entry",
  "response_code": 0,
  "response_message": "UPLOADED SUCCESSFULLY"
}
```

### Failure Response

```json
{
  "success": false,
  "error": "Document has no payable amount.",
  "transaction_reference": null,
  "transaction_log": null,
  "response_code": null
}
```

## 10. Request Sent by App to Pine Labs: Upload Transaction

This is built by the Pine Labs client before calling `UploadBilledTransaction`.

### Request to Pine Labs

```json
{
  "MerchantID": "MERCHANT_ID",
  "SecurityToken": "SECURITY_TOKEN",
  "ClientID": "CLIENT_ID",
  "StoreID": "STORE_ID",
  "TransactionNumber": "TXN20260409123456ABCD1234",
  "SequenceNumber": 1,
  "AllowedPaymentMode": 1,
  "Amount": 120000,
  "TotalInvoiceAmount": 120000,
  "UserID": "user@example.com",
  "AutoCancelDurationInMinutes": 3,
  "ForceCancelOnBack": false,
  "IsMQTTDisabled": false
}
```

### Expected Immediate Pine Labs Response Used by App

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "UPLOADED SUCCESSFULLY",
  "PlutusTransactionReferenceID": 123456789,
  "TransactionData": []
}
```

### App Behavior After Response

- Creates `Pinelabs Transaction Log`
- Saves:
  - request payload
  - response payload
  - mode and source metadata
  - machine info
  - merchant/store info
- Saves `PlutusTransactionReferenceID` into `external_reference`
- Returns initiation response back to caller

## 11. Request: `check_payment_status`

### Request

```json
{
  "transaction_reference": "TXN20260409123456ABCD1234"
}
```

### App Request to Pine Labs

```json
{
  "MerchantID": "MERCHANT_ID",
  "SecurityToken": "SECURITY_TOKEN",
  "ClientID": "CLIENT_ID",
  "StoreID": "STORE_ID",
  "PlutusTransactionReferenceID": 123456789
}
```

### Expected Pine Labs Status Response Used by App

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "APPROVED",
  "PlutusTransactionReferenceID": 123456789,
  "RRN": "123456789012",
  "ApprovalCode": "APP123",
  "CardNumber": "********1234",
  "CardType": "CREDIT",
  "CardHolderName": "NAME",
  "BankName": "BANK",
  "TransactionData": []
}
```

### Response Returned by App to Frontend/Caller

```json
{
  "success": true,
  "status": "Approved",
  "transaction_reference": "TXN20260409123456ABCD1234",
  "invoice_name": "SI-0001",
  "invoice_doctype": "Sales Invoice",
  "invoice_finalized": true,
  "finalization_status": "finalized",
  "message": "Payment approved.",
  "error": null,
  "response_code": 0,
  "response_message": "APPROVED",
  "amount": 1200.0,
  "payment_mode": "Card",
  "payment_mode_label": "Card",
  "mode_of_payment": "PineLabs - Card",
  "allowed_payment_mode": 1,
  "source_system": "erpnext",
  "source_doctype": "Sales Invoice",
  "source_docname": "SI-0001",
  "external_order_id": "SI-0001",
  "rrn": "123456789012",
  "approval_code": "APP123",
  "payment_entry": "ACC-PAY-2026-0007"
}
```

For mapped-only doctypes, the same response shape is used, but `finalization_status` may be `fields_updated` and `payment_entry` may remain `null`.

### If Status Check Fails Before Approval

```json
{
  "success": true,
  "status": "Pending",
  "transaction_reference": "TXN20260409123456ABCD1234",
  "message": "Waiting for payment confirmation.",
  "response_code": 1001,
  "response_message": "TXN UPLOADED"
}
```

## 12. Request: `cancel_payment`

### Request

```json
{
  "transaction_reference": "TXN20260409123456ABCD1234"
}
```

### App Request to Pine Labs

```json
{
  "MerchantID": "MERCHANT_ID",
  "SecurityToken": "SECURITY_TOKEN",
  "PlutusTransactionReferenceID": 123456789,
  "UserID": "user@example.com"
}
```

### Expected Pine Labs Cancel Response Used by App

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "Cancelled"
}
```

### Response Returned by App

```json
{
  "success": true,
  "status": "Cancelled",
  "transaction_reference": "TXN20260409123456ABCD1234",
  "invoice_name": "SI-0001",
  "invoice_doctype": "Sales Invoice",
  "invoice_finalized": true,
  "finalization_status": "already_finalized",
  "message": "Transaction cancelled successfully",
  "error": null,
  "response_code": 0,
  "response_message": "Cancelled",
  "amount": 1200.0,
  "payment_mode": "Card",
  "payment_mode_label": "Card",
  "mode_of_payment": "PineLabs - Card",
  "allowed_payment_mode": 1,
  "source_system": "erpnext",
  "source_doctype": "Sales Invoice",
  "source_docname": "SI-0001",
  "external_order_id": "SI-0001",
  "rrn": "123456789012",
  "approval_code": "APP123",
  "payment_entry": "ACC-PAY-2026-0007"
}
```

## 13. Request Received from Pine Labs Webhook

Webhook endpoint:

`/api/method/pinelabs_fin.api.webhooks.handle_pinelabs_webhook`

### Incoming Webhook Request Example

```json
{
  "TransactionNumber": "TXN20260409123456ABCD1234",
  "ResponseCode": 0,
  "ResponseMessage": "APPROVED",
  "RRN": "123456789012",
  "ApprovalCode": "APP123",
  "PlutusTransactionReferenceID": 123456789,
  "CardNumber": "********1234",
  "CardType": "CREDIT",
  "CardHolderName": "NAME",
  "BankName": "BANK"
}
```

Snake case variants are also accepted:

```json
{
  "transaction_number": "TXN20260409123456ABCD1234",
  "response_code": "0",
  "response_message": "APPROVED",
  "rrn": "123456789012",
  "approval_code": "APP123",
  "plutus_transaction_reference_id": 123456789
}
```

### Webhook Response from App

```json
{
  "success": true,
  "message": "Webhook processed successfully",
  "transaction_reference": "TXN20260409123456ABCD1234",
  "status": "Approved"
}
```

### Webhook Failure Response

```json
{
  "success": false,
  "error": "Transaction reference not found in webhook data"
}
```

## 14. Request: `get_payment_settings`

### Request

```json
{}
```

### Response

```json
{
  "success": true,
  "enabled": true,
  "active_endpoint": "UAT",
  "client_id": "CLIENT_ID",
  "store_id": "STORE_ID",
  "device_number": "1"
}
```

## 15. Request: `is_pinelabs_available`

### Request

```json
{}
```

### Response

```json
{
  "available": true,
  "enabled": true,
  "active_endpoint": "UAT"
}
```

## 16. Request: `get_available_machines`

### Request

```json
{}
```

### Response

```json
[
  {
    "machine_name": "Terminal-1",
    "warehouse": "Stores - FP",
    "description": "Main billing machine"
  }
]
```

## 17. Request: `test_credentials`

### Request

```json
{}
```

### Success Response

```json
{
  "success": true,
  "message": "Credentials are valid! API connection successful.",
  "response_code": 0,
  "response_message": "UPLOADED SUCCESSFULLY",
  "api_url": "https://example-host",
  "active_endpoint": "UAT",
  "validation_results": {
    "merchant_id": true,
    "security_token": true,
    "client_id": true,
    "store_id": true,
    "device_number": true,
    "api_url": true
  }
}
```

### Failure Response

```json
{
  "success": false,
  "error": "API connection failed. HTTP Status: 500",
  "http_status": 500,
  "api_url": "https://example-host",
  "active_endpoint": "UAT",
  "validation_results": {
    "merchant_id": true,
    "security_token": true,
    "client_id": true,
    "store_id": true,
    "device_number": true,
    "api_url": true
  },
  "note": "Check: API URL, network connectivity, firewall settings"
}
```

## 18. Request: `get_webhook_url`

### Request

```json
{}
```

### Response

```json
{
  "success": true,
  "webhook_url": "https://your-site.com/api/method/pinelabs_fin.api.webhooks.handle_pinelabs_webhook"
}
```

## 19. What Data This App Stores in `Pinelabs Transaction Log`

Stored business and communication fields:

1. `transaction_reference`
2. `invoice`
3. `external_order_id`
4. `source_system`
5. `source_doctype`
6. `source_docname`
7. `status`
8. `amount`
9. `payment_mode`
10. `mode_of_payment`
11. `allowed_payment_mode`
12. `payment_mode_label`
13. `payment_entry`
14. `machine_name`
15. `merchant_id`
16. `store_id`
17. `rrn`
18. `approval_code`
19. `external_reference`
20. `card_number_masked`
21. `card_holder_name`
22. `card_type`
23. `bank_name`
24. `request_payload`
25. `response_payload`
26. `error_message`
27. `created_at`
28. `updated_at`
29. `completed_at`

## 20. Final ERPNext Response Behavior

When the transaction reaches `Approved`, the app either creates a `Payment Entry` or updates mapped fields and returns the final business result.

Typical `Payment Entry` finalization response:

```json
{
  "success": true,
  "invoice_name": "SI-0001",
  "invoice_doctype": "Sales Invoice",
  "invoice_finalized": true,
  "finalization_status": "finalized",
  "payment_entry": "ACC-PAY-2026-0007",
  "message": "Payment Entry ACC-PAY-2026-0007 created successfully."
}
```

Typical mapped-field finalization response:

```json
{
  "success": true,
  "invoice_name": "ACC-PAY-2026-0001",
  "invoice_doctype": "Payment Entry",
  "invoice_finalized": true,
  "finalization_status": "fields_updated",
  "payment_entry": null,
  "message": "Updated reference_no, status on Payment Entry ACC-PAY-2026-0001."
}
```

Possible finalization statuses:

1. `finalized`
2. `already_finalized`
3. `fields_updated`
4. `invoice_not_submitted`
5. `failed`
6. `skipped`

## 21. Short Summary

In current code, the app behaves like this:

1. The app can place built-in Pinelabs buttons on configured ERPNext or custom Frappe forms by reading `Pinelabs Settings > Payable Doctypes`.
2. The app also remains API-first, so custom pages and external apps can render their own payment control and call the same shared Pinelabs endpoints.
3. When a payable-doctype row exists, the app validates docstatus, mapped amount, selected mode, enabled machine, and paid state through `get_trigger_context`.
4. If valid, the app starts terminal communication, stores the transaction log, tracks status through polling or webhook, and finalizes the flow using either `Payment Entry` creation or mapped field updates.

This file documents the current implemented behavior only.

## 22. Manual Testing Instructions

After deploying this change:

1. Run `bench --site <site-name> migrate`.
2. Run `bench build --app pinelabs_fin`.
3. Hard-refresh Desk with `Ctrl+Shift+R`.
4. Open `Pine Labs Settings`.
5. Reconfigure existing `Payable Doctypes` rows because old `Show when` values are no longer used.
6. Add a `Sales Invoice` row:
   - `Show on docstatus`: `1`
   - `Amount field`: `outstanding_amount`
   - `Status field`: `status`
   - `Paid status value`: `Paid`
   - `Create Payment Entry`: checked
7. Open a submitted Sales Invoice with `outstanding_amount > 0` and confirm the Pinelabs toolbar button appears under the `Pinelabs` group.
8. Open a draft Sales Invoice and confirm the button does not appear for the `Show on docstatus = 1` row.
9. Change the row to `Show on docstatus = 0,1`, refresh Desk, and confirm the button appears on draft and submitted documents but not cancelled documents.
10. Configure a custom doctype row with `Create Payment Entry` unchecked, a numeric `Amount field`, a `Status field`, and a `Paid status value`.
11. If that doctype is submittable and you want the payment flow to submit the document, set `Paid status value` to `Submitted`.
12. Trigger a test terminal payment from that custom doctype and confirm approval writes the mapped fields. If `Transaction reference field` is configured, confirm it stores the Pine Labs transaction reference.
13. For the Sales Invoice row, confirm approval creates a `Payment Entry` and links it to `Pinelabs Transaction Log`.
14. Confirm the open document reloads after a successful built-in Desk payment so refreshed status and mapped fields are visible without a manual refresh.
15. Test the cancel path from the polling dialog and confirm the transaction log is marked cancelled.

## License

mit
