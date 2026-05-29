# Pine Labs Terminal API — Concrete Payload Examples

Copy-paste-ready JSON for every scenario. Pair this with [TERMINAL_API_CONTRACT.md](TERMINAL_API_CONTRACT.md) for field meanings.

For each scenario the layout is:

- **Caller → app** — what a custom button / wrapper / external system sends.
- **App → machine** — exact body POSTed to the Pine Labs Cloud endpoint.
- **Machine → app** — the raw Pine Labs response body (before this app's wrapping).
- **App → caller** — the final JSON returned to the caller, inside Frappe's `{"message": ...}` envelope.

All values are realistic placeholders — replace `MerchantID`, `SecurityToken`, etc. with your actual machine credentials when testing.

---

## ENDPOINT 1 — `initiate_terminal_payment`

### 1.1 Happy path — accepted, terminal will process

**Caller → app**

```http
POST /api/method/pinelabs_fin.api.terminal.initiate_terminal_payment
Content-Type: application/json
```

```json
{
  "reference_doctype": "Sales Invoice",
  "reference_name": "ACC-SINV-2026-00054",
  "mode_of_payment": "PineLabs - Card",
  "amount": 159.00
}
```

**App → machine** — `POST https://www.plutuscloudserviceuat.in:8201/API/CloudBasedIntegration/V1/UploadBilledTransaction`

```json
{
  "MerchantID": "987654321012345",
  "SecurityToken": "abc123token456xyz789def012ghi345",
  "ClientID": "CL12345",
  "StoreID": "ST001",
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

**Machine → app**

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "APPROVED",
  "PlutusTransactionReferenceID": 1234567890
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "transaction_name": "PLT-2026-00007",
    "status": "PENDING",
    "response_code": 0,
    "message": "APPROVED"
  }
}
```

---

### 1.2 UPI variant — same flow with `AllowedPaymentMode: 11`

**Caller → app**

```json
{
  "reference_doctype": "POS Invoice",
  "reference_name": "ACC-PSINV-2026-00088",
  "mode_of_payment": "PineLabs - UPI"
}
```

**App → machine**

```json
{
  "MerchantID": "987654321012345",
  "SecurityToken": "abc123token456xyz789def012ghi345",
  "ClientID": "CL12345",
  "StoreID": "ST001",
  "TransactionNumber": "PLT-2026-00008",
  "SequenceNumber": 1,
  "AllowedPaymentMode": 11,
  "Amount": 25000,
  "TotalInvoiceAmount": 25000,
  "UserID": "operator@example.com",
  "AutoCancelDurationInMinutes": 3,
  "ForceCancelOnBack": false,
  "IsMQTTDisabled": false
}
```

**Machine → app**

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "TRANSACTION INITIATED",
  "PlutusTransactionReferenceID": 1234567891
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "transaction_name": "PLT-2026-00008",
    "status": "PENDING",
    "response_code": 0,
    "message": "TRANSACTION INITIATED"
  }
}
```

---

### 1.3 Synchronous decline — `ResponseCode 2`

**App → machine** — same shape as 1.1

**Machine → app**

```json
{
  "ResponseCode": 2,
  "ResponseMessage": "Invalid Merchant Configuration"
}
```

**App → caller**

```json
{
  "message": {
    "success": false,
    "transaction_name": "PLT-2026-00009",
    "status": "FAILED",
    "response_code": 2,
    "message": "Invalid Merchant Configuration"
  }
}
```

---

### 1.4 Accepted but missing `PlutusTransactionReferenceID` (Gap 4 — hard fail)

**Machine → app**

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "APPROVED"
}
```

**App → caller**

```json
{
  "message": {
    "success": false,
    "transaction_name": "PLT-2026-00010",
    "status": "FAILED",
    "response_code": 0,
    "message": "Pine Labs accepted the transaction but returned no PlutusTransactionReferenceID. Cannot track this txn."
  }
}
```

---

### 1.5 HTTP timeout / connection error

**App → machine** — request fired but no response in 30s; `requests.exceptions.Timeout`.

**Machine → app** — *(none — `_handle_request_exception` synthesises)*

```json
{
  "success": false,
  "error": "Request timeout - Pine Labs server did not respond",
  "status_code": 0,
  "data": {}
}
```

**App → caller**

```json
{
  "message": {
    "success": false,
    "transaction_name": "PLT-2026-00011",
    "status": "FAILED",
    "response_code": null,
    "message": "Request timeout - Pine Labs server did not respond"
  }
}
```

---

### 1.6 Unknown ResponseCode (e.g. `999`)

**Machine → app**

```json
{
  "ResponseCode": 999,
  "ResponseMessage": "Undocumented status"
}
```

**App → caller**

```json
{
  "message": {
    "success": false,
    "transaction_name": "PLT-2026-00012",
    "status": "FAILED",
    "response_code": 999,
    "message": "Undocumented status"
  }
}
```

---

## ENDPOINT 2 — `check_terminal_status`

### 2.1 Still waiting — `ResponseCode 1001`

**Caller → app**

```http
POST /api/method/pinelabs_fin.api.terminal.check_terminal_status
Content-Type: application/json
```

```json
{
  "transaction_name": "PLT-2026-00007"
}
```

**App → machine** — `POST https://www.plutuscloudserviceuat.in:8201/API/CloudBasedIntegration/V1/GetCloudBasedTxnStatus`

```json
{
  "MerchantID": "987654321012345",
  "SecurityToken": "abc123token456xyz789def012ghi345",
  "ClientID": "CL12345",
  "StoreID": "ST001",
  "PlutusTransactionReferenceID": 1234567890
}
```

**Machine → app**

```json
{
  "ResponseCode": 1001,
  "ResponseMessage": "TXN UPLOADED"
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "status": "PENDING",
    "response_code": 1001,
    "message": "Waiting on terminal"
  }
}
```

---

### 2.2 Approved — full `TransactionData` payload

**Machine → app**

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

**App → caller**

```json
{
  "message": {
    "success": true,
    "status": "SUCCESS",
    "response_code": 0,
    "message": "Approved"
  }
}
```

> Side effects: this app's `mark_success` ALSO creates a `Payment Entry` linked to the source invoice, and stores RRN, ApprovalCode, CardType, etc. inside `Pinelabs Transaction.response_payload`.

---

### 2.3 UPI approved variant

**Machine → app**

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "APPROVED",
  "PlutusTransactionReferenceID": 1234567891,
  "TransactionData": [
    { "Tag": "PaymentMode",    "Value": "UPI" },
    { "Tag": "Amount",         "Value": "25000" },
    { "Tag": "CardType",       "Value": "UPI" },
    { "Tag": "BankName",       "Value": "STATE BANK OF INDIA" },
    { "Tag": "RRN",            "Value": "611234567890" },
    { "Tag": "ApprovalCode",   "Value": "654321" },
    { "Tag": "TID",            "Value": "12345678" },
    { "Tag": "MID",            "Value": "987654321012345" },
    { "Tag": "InvoiceNumber",  "Value": "PLT-2026-00008" }
  ]
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "status": "SUCCESS",
    "response_code": 0,
    "message": "Approved"
  }
}
```

---

### 2.4 Declined — `ResponseCode 1`

**Machine → app**

```json
{
  "ResponseCode": 1,
  "ResponseMessage": "Declined by Issuer"
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "status": "FAILED",
    "response_code": 1,
    "message": "Declined by Issuer"
  }
}
```

---

### 2.5 Declined — `ResponseCode 2` (insufficient funds / card limit)

**Machine → app**

```json
{
  "ResponseCode": 2,
  "ResponseMessage": "Insufficient Funds"
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "status": "FAILED",
    "response_code": 2,
    "message": "Insufficient Funds"
  }
}
```

---

### 2.6 Age-out catches paid race (Gap 2) — final-poll returns 0

**Machine → app (during the 1001-past-cutoff poll)**

```json
{
  "ResponseCode": 1001,
  "ResponseMessage": "TXN UPLOADED"
}
```

**App → machine (cancel call fired by settlement helper)** — `POST .../V1/CancelTransaction`

```json
{
  "MerchantID": "987654321012345",
  "SecurityToken": "abc123token456xyz789def012ghi345",
  "PlutusTransactionReferenceID": 1234567890,
  "UserID": "operator@example.com"
}
```

**Machine → app (cancel response — too late, customer already paid)**

```json
{ "ResponseCode": 1, "ResponseMessage": "Transaction already processed" }
```

**App → machine (final status poll fired by settlement helper)** — same shape as 2.1

**Machine → app (final status response)**

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "APPROVED",
  "PlutusTransactionReferenceID": 1234567890,
  "TransactionData": [
    { "Tag": "RRN", "Value": "534299988877" },
    { "Tag": "ApprovalCode", "Value": "888001" }
  ]
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "status": "SUCCESS",
    "response_code": 0,
    "message": "Approved (caught at age-out)"
  }
}
```

---

### 2.7 Age-out, no race — terminal genuinely abandoned (Gap 1+2)

**Machine → app** (during 1001-past-cutoff poll)

```json
{
  "ResponseCode": 1001,
  "ResponseMessage": "TXN UPLOADED"
}
```

**Machine → app** (final status response after cancel)

```json
{
  "ResponseCode": 1,
  "ResponseMessage": "Customer did not present card"
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "status": "FAILED",
    "response_code": 1001,
    "message": "Aged out"
  }
}
```

---

### 2.8 Unknown code (within cutoff — captured but not failed yet)

**Machine → app**

```json
{
  "ResponseCode": 5,
  "ResponseMessage": "Network busy"
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "status": "PENDING",
    "response_code": 5,
    "message": "Network busy"
  }
}
```

---

### 2.9 HTTP timeout during poll (Gap 5 — error string propagated)

**App → machine** — request fires, no response in 30s.

**Machine → app** — *(synthesised by `_handle_request_exception`)*

```json
{
  "success": false,
  "error": "Request timeout - Pine Labs server did not respond",
  "status_code": 0,
  "data": {}
}
```

**App → caller** (assuming still within cutoff)

```json
{
  "message": {
    "success": true,
    "status": "PENDING",
    "response_code": null,
    "message": "Request timeout - Pine Labs server did not respond"
  }
}
```

---

### 2.10 `payment_id` missing on the txn (degenerate state)

**Caller → app**

```json
{ "transaction_name": "PLT-2026-00010" }
```

(`PLT-2026-00010` is one of the Gap-4 hard-failed txns — never got a Plutus ref.)

**App → machine** — *(no call made — short-circuits)*

**App → caller**

```json
{
  "message": {
    "success": false,
    "status": "FAILED",
    "response_code": null,
    "message": "PlutusTransactionReferenceID missing"
  }
}
```

---

## ENDPOINT 3 — `cancel_terminal_payment`

### 3.1 Cancel succeeded

**Caller → app**

```http
POST /api/method/pinelabs_fin.api.terminal.cancel_terminal_payment
Content-Type: application/json
```

```json
{
  "transaction_name": "PLT-2026-00007",
  "reason": "user cancelled from modal"
}
```

**App → machine** — `POST .../V1/CancelTransaction`

```json
{
  "MerchantID": "987654321012345",
  "SecurityToken": "abc123token456xyz789def012ghi345",
  "PlutusTransactionReferenceID": 1234567890,
  "UserID": "operator@example.com"
}
```

**Machine → app**

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "Transaction Cancelled"
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "transaction_name": "PLT-2026-00007",
    "status": "CANCELLED",
    "response_code": 0,
    "message": "Transaction Cancelled"
  }
}
```

---

### 3.2 Cancel rejected — customer already paid (Gap 3 → SUCCESS)

**Caller → app** — same as 3.1

**App → machine (cancel)** — same as 3.1

**Machine → app (cancel response)**

```json
{
  "ResponseCode": 1,
  "ResponseMessage": "Transaction already processed"
}
```

**App → machine (verification status poll)**

```json
{
  "MerchantID": "987654321012345",
  "SecurityToken": "abc123token456xyz789def012ghi345",
  "ClientID": "CL12345",
  "StoreID": "ST001",
  "PlutusTransactionReferenceID": 1234567890
}
```

**Machine → app (status response)**

```json
{
  "ResponseCode": 0,
  "ResponseMessage": "APPROVED",
  "PlutusTransactionReferenceID": 1234567890,
  "TransactionData": [
    { "Tag": "RRN", "Value": "534212345678" },
    { "Tag": "ApprovalCode", "Value": "123456" }
  ]
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "transaction_name": "PLT-2026-00007",
    "status": "SUCCESS",
    "response_code": 0,
    "message": "Cancel rejected by gateway: customer already paid. Marked SUCCESS."
  }
}
```

---

### 3.3 Cancel rejected — customer NOT paid → still CANCELLED

**Machine → app (cancel response)**

```json
{
  "ResponseCode": 1,
  "ResponseMessage": "Cannot cancel — terminal busy"
}
```

**Machine → app (verification status poll response)**

```json
{
  "ResponseCode": 1001,
  "ResponseMessage": "TXN UPLOADED"
}
```

**App → caller**

```json
{
  "message": {
    "success": true,
    "transaction_name": "PLT-2026-00007",
    "status": "CANCELLED",
    "response_code": 1,
    "message": "Cannot cancel — terminal busy"
  }
}
```

---

### 3.4 Cancel on a txn with no `payment_id` (degenerate state — no API call)

**Caller → app**

```json
{
  "transaction_name": "PLT-2026-00010",
  "reason": "abandoned"
}
```

**App → machine** — *(no call made — short-circuits)*

**App → caller**

```json
{
  "message": {
    "success": true,
    "transaction_name": "PLT-2026-00010",
    "status": "CANCELLED",
    "response_code": null,
    "message": "abandoned"
  }
}
```

---

### 3.5 Cancel of an already-terminal txn (idempotent re-call)

**Caller → app**

```json
{ "transaction_name": "PLT-2026-00007" }
```

**App → machine** — *(no call made — already terminal)*

**App → caller**

```json
{
  "message": {
    "success": true,
    "transaction_name": "PLT-2026-00007",
    "status": "CANCELLED",
    "response_code": null,
    "message": "Already terminal"
  }
}
```

---

## Quick-reference index

| Scenario | Section | App → caller `status` | App → caller `response_code` |
|---|---|---|---|
| Initiate accepted (Card) | 1.1 | `PENDING` | `0` |
| Initiate accepted (UPI) | 1.2 | `PENDING` | `0` |
| Initiate sync decline | 1.3 | `FAILED` | `2` |
| Initiate accept-without-Plutus-ref | 1.4 | `FAILED` | `0` |
| Initiate HTTP timeout | 1.5 | `FAILED` | `null` |
| Initiate unknown ResponseCode | 1.6 | `FAILED` | the unknown code |
| Poll — still waiting | 2.1 | `PENDING` | `1001` |
| Poll — approved Card | 2.2 | `SUCCESS` | `0` |
| Poll — approved UPI | 2.3 | `SUCCESS` | `0` |
| Poll — declined (issuer) | 2.4 | `FAILED` | `1` |
| Poll — declined (funds) | 2.5 | `FAILED` | `2` |
| Poll — age-out catches paid race | 2.6 | `SUCCESS` | `0` |
| Poll — age-out, no race | 2.7 | `FAILED` | `1001` |
| Poll — unknown code | 2.8 | `PENDING` | the unknown code |
| Poll — HTTP timeout | 2.9 | `PENDING` | `null` |
| Poll — payment_id missing | 2.10 | `FAILED` | `null` |
| Cancel succeeded | 3.1 | `CANCELLED` | `0` |
| Cancel rejected, customer paid | 3.2 | `SUCCESS` | `0` |
| Cancel rejected, not paid | 3.3 | `CANCELLED` | `1` |
| Cancel no payment_id | 3.4 | `CANCELLED` | `null` |
| Cancel already terminal | 3.5 | (whatever it was) | `null` |
