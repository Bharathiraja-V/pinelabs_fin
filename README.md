<div align="center">

<img src="logos/finstein_logo.png" alt="Finstein" height="56">
&nbsp;&nbsp;<strong>×</strong>&nbsp;&nbsp;
<img src="logos/frappe_logo.png" alt="Frappe" height="56">

# pinelabs_fin

**Pine Labs payments inside ERPNext.** Tap-on-terminal *or* send-a-link, with the Payment Entry created automatically when the customer pays.

![ERPNext 15](https://img.shields.io/badge/ERPNext-15-blue) ![Frappe 15](https://img.shields.io/badge/Frappe-15-orange) ![License MIT](https://img.shields.io/badge/license-MIT-lightgrey)

</div>

---

## What you get

One ERPNext app, two payment flows, zero manual reconciliation. Open any submitted invoice — you see two new buttons:

![Pay on Machine + Send Payment Link buttons on a Sales Invoice](screenshots/button.png)

Click one. The customer pays. The invoice flips to **Paid** automatically.

| Button | Use when… | Customer pays via… |
|---|---|---|
| **Pay on Machine** | The customer is in front of you | A physical Pine Labs Cloud terminal (card / UPI) |
| **Send Payment Link** | The customer is elsewhere | A Plural hosted checkout link delivered by SMS + email |

> **Zero ERPNext core changes.** No custom fields are added to Sales Invoice, POS Invoice, Payment Entry, Customer, or Contact. All payment data lives in a new `Pinelabs Transaction` doctype, linked via a Dynamic Link.

---

## Flow 1 — Pay on Machine

Customer at the counter. Tap card or scan UPI.

| Step | What happens |
|---|---|
| 1. Pick Card or UPI | ![Mode of Payment selector](screenshots/mode_of_payment.png) |
| 2. Wait for the customer | ![Waiting for terminal](screenshots/waiting_for_terminal.png) |
| 3. Customer taps → invoice flips to Paid | ![Sales Invoice marked Paid](screenshots/invoice_paid.png) |

Total time: ~5–10 seconds. The Payment Entry is created and submitted automatically.

---

## Flow 2 — Send Payment Link

Customer not present. Pine Labs delivers a link by SMS and email.

| Step | What happens |
|---|---|
| 1. Click the button | ![Send Payment Link button](screenshots/send_link_invoice.png) |
| 2. Link generated and delivered | ![Payment Link Generated popup](screenshots/payment_link_generated.png) |

When the customer pays — minutes or hours later — Pine Labs sends a webhook to the app. The invoice flips to **Paid** within a minute.

---

## Quick install

```bash
cd ~/frappe-bench
bench get-app https://github.com/<owner>/pinelabs_fin
bench --site <your-site> install-app pinelabs_fin
bench --site <your-site> migrate
```

Need Frappe v15, ERPNext v15, Python 3.10+, MariaDB 10.6+ with InnoDB.

---

## Configure once

All configuration lives in a single doctype: **Pinelabs Settings**. Two things to fill in.

### Your Pine Labs machine

![Machine configuration](screenshots/Machine_Configurations.png)

Drop in the credentials from your Pine Labs onboarding email. Mark one machine as **Is Default** and you are done.

### Which doctypes can be paid

![Payable Documents row](screenshots/Button%20render.png)

Add a row per doctype you want the buttons on (Sales Invoice, POS Invoice, Payment Request, or your own custom doctype). The two toggles **Pay on Machine** / **Send Payment Link** control which buttons render on that doctype.

For the **Send Payment Link** flow you also need to tell the app where to find the customer's email and mobile:

![Customer Contact Field Mapping](screenshots/customer_contact_mapping.png)

Either point at a field directly on the source doc (**Direct Field**), or follow a link to another doctype (**Linked DocType**) — the most common setup is `customer_address → Address → phone` for mobile, `contact_email` direct for email.

---

## Optional — multi-terminal routing

Have two or more terminals (one per till, one per warehouse)? Switch **Terminal Routing Mode** to `Mapping` and add rules:

![Machine Mappings table](screenshots/Machine%20mapping.png)

The app picks the right terminal automatically based on who is at the till, what warehouse is on the invoice, or what POS profile is active. No rule matches? Falls back to the default machine.

---

## Track every payment

Every payment attempt — terminal, link, retry — lands on one **Pinelabs Transaction** record. Status moves through `INITIATED → PENDING → SUCCESS / FAILED / CANCELLED`. The record carries the full request and response payloads for debugging.

![Pinelabs Transaction record](screenshots/pinelabs_transaction.png)

Click **Actions → Refresh Status** to ask Pine Labs for the latest state on demand. Otherwise a 1-minute scheduler does it for you in the background.

---

## For developers — call the same flows from your own code

Three Python entry points (whitelisted, callable from JS or REST):

```python
pinelabs_fin.api.terminal.initiate_terminal_payment(reference_doctype, reference_name, mode_of_payment, amount=None)
pinelabs_fin.api.plural.initiate_payment_link(reference_doctype, reference_name, flow="all_methods", customer_email=None, customer_mobile=None)
pinelabs_fin.api.terminal.check_terminal_status(transaction_name)
```

Plus two `frappe.pinelabs.*` JS helpers that wrap the API and open the same UI the built-in buttons use:

```javascript
frappe.pinelabs.start_terminal_payment(args, options);  // → "Waiting for terminal" modal
frappe.pinelabs.start_payment_link(args, options);      // → "Payment Link Generated" popup
```

Full API contract + custom-button example: see [guide.md](guide.md) section 5.

---

## Documentation

| What you want | Where to go |
|---|---|
| Install + configure step by step, with screenshots | **[guide.md](guide.md)** |
| API contract for custom buttons | **[guide.md](guide.md) §5** |
| Marketplace readiness, security notes, internal architecture | **[PINELABS_FIN_GUIDE.txt](PINELABS_FIN_GUIDE.txt)** |

---

## License

MIT — see [license.txt](license.txt).

## Support

Open a GitHub issue. Include the Pinelabs Transaction id of the affected payment and any `[PineLabs]` / `[PineLabs PayByLink]` lines from your `bench start` terminal.

---

<div align="center">

Built with <img src="logos/frappe_logo.png" alt="Frappe" height="22"> &nbsp;·&nbsp; by <img src="logos/finstein_logo.png" alt="Finstein" height="22">

</div>
