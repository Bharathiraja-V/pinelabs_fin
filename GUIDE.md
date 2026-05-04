# Pine Labs Integration Guide

This guide explains how to use the Pine Labs integration in your Frappe/ERPNext application, covering both automatic DocType configuration and custom API usage for developers.

---

## 1. Automatic Configuration (Payable Doctypes)

The easiest way to use Pine Labs is to configure it for an existing DocType. This will automatically add a "Pay via Pinelabs" button to the document's toolbar.

### Setup Steps:
1.  Go to **Pine Labs Settings**.
2.  In the **Payable Doctypes** table, add a new row.
3.  **DocType**: Select your DocType (e.g., `Sales Invoice`).
4.  **Show on docstatus**:
    *   `0`: Show on Drafts.
    *   `1`: Show on Submitted documents.
    *   `0,1`: Show on both.
5.  **Amount Field**: Select the field that contains the amount (e.g., `outstanding_amount`).
6.  **Status Field**: Select the field that tracks payment status (e.g., `status`).
7.  **Paid Status Value**: Enter the value that should be set when payment succeeds (e.g., `Paid`).
8.  **Create Payment Entry**: Check this if you want the system to automatically create a standard ERPNext Payment Entry.

---

## 2. Custom Integration (API Guide)

If you are building a custom page or a custom POS screen, you can trigger the terminal using our whitelisted APIs.

### A. Initiate Payment
Use this API to send the payment request to the Pine Labs terminal.

**Endpoint:** `pinelabs_fin.api.pos_endpoints.initiate_payment`

**Request Parameters:**
| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `invoice_name` | String | Yes | The name of the document (or a unique ID for offline docs). |
| `amount` | Float | Yes | The amount to be charged. |
| `source_doctype`| String | Yes | The DocType name (e.g., "POS Invoice"). |
| `payment_mode` | String | No | Label like "Card" or "UPI" (Default: "Card"). |
| `allowed_payment_mode` | Int | No | Pine Labs code: `1` for Card, `11` for UPI. |
| `machine_name` | String | No | Specific machine name if multiple are configured. |

**Success Response:**
```json
{
    "success": true,
    "transaction_reference": "PL-TXN-12345",
    "message": "Payment initiated. Please complete payment on terminal.",
    "amount": 100.0,
    "payment_mode": "UPI"
}
```

---

### B. Check Payment Status
After initiating, you must poll this API every 3-5 seconds to check if the user has completed the payment.

**Endpoint:** `pinelabs_fin.api.pos_endpoints.check_payment_status`

**Request Parameters:**
| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `transaction_reference` | String | Yes | The reference received from the initiate API. |

**Response Fields:**
| Field | Meaning |
| :--- | :--- |
| `status` | `Pending`, `Approved`, `Declined`, `Cancelled`, `Failed`, or `Timeout`. |
| `invoice_finalized`| `true` if the source document was updated/finalized. |
| `message` | Human-readable status message. |
| `rrn` | Retrieval Reference Number from the bank. |
| `approval_code` | Approval code from the bank. |

---

## 3. JavaScript Implementation Example

Here is how to combine these APIs in your custom JavaScript:

```javascript
function makePayment(docname, amount) {
    // 1. Trigger the terminal
    frappe.call({
        method: "pinelabs_fin.api.pos_endpoints.initiate_payment",
        args: {
            invoice_name: docname,
            amount: amount,
            source_doctype: "Sales Invoice",
            allowed_payment_mode: 11, // Forces UPI
            payment_mode: "UPI"
        },
        freeze: true,
        callback: function(r) {
            if (r.message && r.message.success) {
                // 2. Start checking for status
                startPolling(r.message.transaction_reference);
            }
        }
    });
}

function startPolling(ref) {
    let interval = setInterval(() => {
        frappe.call({
            method: "pinelabs_fin.api.pos_endpoints.check_payment_status",
            args: { transaction_reference: ref },
            callback: function(res) {
                if (res.message.status === "Approved") {
                    clearInterval(interval);
                    frappe.msgprint("Payment Successful!");
                    cur_frm.reload_doc();
                } else if (res.message.status !== "Pending") {
                    clearInterval(interval);
                    frappe.msgprint("Payment " + res.message.status);
                }
            }
        });
    }, 3000);
}
```

---

## 4. Troubleshooting
*   **Error: "No active API endpoint"**: Go to **Pine Labs Settings > API Endpoints** and ensure one row has **Is Active** checked.
*   **Error: "No machine configured"**: Go to **Pine Labs Settings > Machines** and ensure your machine is **Enabled**.
*   **UPI not triggering**: Ensure you pass `allowed_payment_mode: 11` in your API call.
