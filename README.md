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

1. Detect whether a document is eligible for Pine Labs payment.
2. Expose a shared Mode-of-Payment-facing flow that can start a terminal payment request when called explicitly.
3. Store request/response data in `Pinelabs Transaction Log`.
4. Poll Pine Labs for status or receive a webhook callback.
5. Update the transaction log with the terminal response.
6. Finalize the business flow in ERPNext by creating a `Payment Entry` when applicable.

## 2. Where This App Works

Current frontend asset:

1. Shared helper script loaded globally on Desk through `app_include_js`

Current app-owned trigger point:

1. None. The app does not currently bind itself to any doctype event or custom button.

Current supported backend source documents:

1. `Sales Invoice`
2. `POS Invoice`
3. `Payment Entry`

Current shared frontend API:

1. `run_mode_of_payment_api(opts)`
2. `trigger(opts)`
3. `get_enabled_modes()`
4. `get_mode_config(mode_of_payment)`

Current backend integration points:

1. Trigger context evaluation
2. Payment initiation
3. Payment status polling
4. Payment cancellation
5. Webhook processing
6. ERPNext finalization and Payment Entry creation

## 3. Frontend Behavior

Current frontend behavior is trigger-agnostic.

Current event path:

1. An explicit caller invokes the shared PineLabs Mode-of-Payment helper.
2. Frontend checks whether the provided mode is a PineLabs mode.
3. Frontend calls `get_trigger_context`.
4. Backend validates whether the document is eligible for PineLabs.
5. If valid, frontend starts PineLabs terminal payment.
6. Frontend opens polling dialog.
7. Frontend continues status/cancel/success flow.

Important note:

- Current code does not choose a doctype event or UI widget as the trigger.
- Current code does not show PineLabs trigger buttons.
- Current code does not attach any active `doctype_js` integration.
- Current code validates first, then starts terminal communication when called explicitly.
- Status, cancel, webhook, and finalization flows continue through the same backend APIs.

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
   - is the source document one of the currently supported payable-flow doctypes
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
3. App checks that source document exists and is submitted.
4. App checks outstanding amount.
5. App resolves ERPNext `mode_of_payment`.
6. App resolves bank account from `Mode of Payment Account`.
7. App creates ERPNext `Payment Entry`.
8. App links that `Payment Entry` back to the transaction log.

## 5. Which External or Other App Integrations Are Possible

This app can be used by another app or external caller in two ways:

1. By calling whitelisted Frappe endpoints:
   - `get_trigger_context`
   - `initiate_payment`
   - `check_payment_status`
   - `cancel_payment`
   - `get_pinelabs_payment_modes`
   - `get_payment_settings`
   - `is_pinelabs_available`
   - `get_available_machines`
   - `test_credentials`
   - `get_webhook_url`

2. By importing shared backend functions from the service layer:
   - `evaluate_pinelabs_context`
   - `handle_mode_of_payment_trigger`
   - `get_payment_status`
   - `cancel_payment`
   - `handle_success`
   - `handle_failure`

That means another custom app can pass request data into this app and continue the same Pine Labs flow without duplicating terminal logic.

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
  "doc_state": "draft"
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

When the transaction reaches `Approved`, the app may create `Payment Entry` and return final business result.

Typical finalization response:

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

Possible finalization statuses:

1. `finalized`
2. `already_finalized`
3. `invoice_not_submitted`
4. `failed`
5. `skipped`

## 21. Short Summary

In current code, the app behaves like this:

1. The app exposes a shared PineLabs frontend API but does not currently place its own button or field trigger on any doctype.
2. Another caller can pass a saved `Sales Invoice`, `POS Invoice`, or `Payment Entry` plus a configured PineLabs `mode_of_payment`.
3. The app validates PineLabs settings, enabled machine, selected mode, document state, and payable amount.
4. If valid, the app starts terminal communication, stores the transaction log, and continues through polling, webhook, cancel, and finalization flow.

This file documents the current implemented behavior only.

## License

mit
