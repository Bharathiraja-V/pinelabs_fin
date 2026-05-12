![ERPNext 15](https://img.shields.io/badge/ERPNext-15-blue) ![Frappe 15](https://img.shields.io/badge/Frappe-15-orange) ![Version 0.0.1](https://img.shields.io/badge/version-0.0.1-green) ![License MIT](https://img.shields.io/badge/license-MIT-lightgrey)

# pinelabs_fin

A complete guide for ERPNext admins and cashiers. Read it in order the first time. After that, use the table of contents to jump to the part you need.

---

## Table of contents

1. [What this app does](#1-what-this-app-does)
2. [How to install](#2-how-to-install)
3. [First-time setup](#3-first-time-setup)
4. [Feature 1 — Pay on Machine (terminal)](#4-feature-1--pay-on-machine-terminal)
5. [Feature 2 — Send Payment Link (online)](#5-feature-2--send-payment-link-online)
6. [Managing payments](#6-managing-payments)
7. [Common admin jobs](#7-common-admin-jobs)
8. [Quick fixes](#8-quick-fixes)
9. [License and help](#9-license-and-help)

---

## 1. What this app does

`pinelabs_fin` links ERPNext to Pine Labs payments. Open an invoice in ERPNext and you can:

- **Pay on Machine** — send the bill to a Pine Labs card machine. The customer pays by tapping a card or scanning UPI on the machine.
- **Send Payment Link** — make a Plural payment link. Pine Labs sends the link to the customer by SMS and email. The customer can pay later on their phone using card, UPI, net banking, or wallet.

When the customer pays, the app **makes the Payment Entry in ERPNext on its own**. The invoice changes to Paid. You do not need to do this by hand.

### Who should read which part

- **Admins** who install and set up the app → read parts 2 and 3.
- **Cashiers** who take payments every day → read parts 4 and 5.
- **Admins** who keep the app running → read parts 6 and 7.

---

## 2. How to install

### What you need

| Thing | Minimum version | Notes |
|---|---|---|
| Frappe | 15.x | |
| ERPNext | 15.x | Must be installed |
| Python | 3.10 or newer | |
| MariaDB | 10.6+ with InnoDB | Needed so the app can lock rows safely |

> **Note:** To check your ERPNext version, run `bench --site <your-site> list-apps`.

### Install steps

Open a terminal in your bench folder and run:

```bash
cd ~/frappe-bench
bench get-app https://github.com/<owner>/pinelabs_fin
bench --site <your-site> install-app pinelabs_fin
bench --site <your-site> migrate
```

The `migrate` step does three things:

- Makes the new tables (doctypes) the app needs: `Pinelabs Settings`, `Pinelabs Transaction`, and a few others.
- Adds 4 Modes of Payment: `PineLabs - Card`, `PineLabs - UPI`, `PineLabs - NetBanking`, `PineLabs - Wallet`.
- Adds 4 small fields on Mode of Payment under a "Pine Labs Integration" section.

> **Tip:** The app does **not** change any ERPNext built-in doctype. It does not add fields to Sales Invoice, POS Invoice, Payment Entry, Customer, or Contact. The only new fields are on Mode of Payment, which is just a setup doctype.

### Turn on the scheduler

The scheduler runs a job every minute. This job is a safety net. If a webhook is lost or a cashier closes a window too soon, the scheduler still finishes the payment correctly.

```bash
bench --site <your-site> scheduler enable
bench --site <your-site> scheduler resume
```

Check it is on:

```bash
bench --site <your-site> scheduler status      # should say "Scheduler is enabled"
bench --site <your-site> show-pending-jobs     # should list 2 pinelabs_fin jobs
```

### After install — check that everything works

- [ ] In ERPNext, **Pinelabs Settings** opens as a single page.
- [ ] **Mode of Payment** list shows the 4 new `PineLabs - *` rows.
- [ ] **Pinelabs Transaction** list opens (it will be empty for now).
- [ ] `bench scheduler status` says `enabled`.

If something is missing, run `bench migrate` again. It is safe to run it many times.

---

## 3. First-time setup

All settings are in one place: **Pinelabs Settings**. Open it from the Awesome Bar in the desk.

### Step 1 — Master switch

Tick **Enabled**. Save. This is the main on-off switch. Untick it any time to hide all PineLabs buttons in the whole app.

### Step 2 — Add a card machine

Go to **Machines** in Pinelabs Settings. Add one row for each Pine Labs card machine you have. Fill in these fields:

| Field | Where it comes from |
|---|---|
| machine_name | A short name you choose ("Front counter", "Till 2") |
| client_id, security_token, merchant_id, store_id, terminal_id | From the welcome email Pine Labs sent you |
| enabled | Tick |
| is_default | Tick on **only one** machine |

> **Warning:** You cannot save Pinelabs Settings if zero or more than one machine is the default. If you have one machine, mark it as default.

### Step 3 — Pick a routing mode

In **Terminal Routing Mode**, choose:

- **Single Machine** — best if you have one card machine (or one main and one backup). Every payment goes to the default machine.
- **Mapping** — best if you have many card machines. The app picks the right machine using rules. If no rule matches, it uses the default machine.

If you pick Mapping, a **Machine Mappings** table appears. Add rules like:

```
mapping_type=User,      reference_name=alice@example.com,  machine="Counter 1"
mapping_type=Warehouse, reference_name="Stores - WH",       machine="Counter 2"
```

### Step 4 — Set up Plural API (only if you will use Send Payment Link)

If you only want **Pay on Machine**, skip this step.

In **Plural API Configuration**, fill in:

| Field | What to put |
|---|---|
| Enable Payment Links | Tick |
| Plural Client ID | From your Pine Labs / Plural dashboard |
| Plural Client Secret | From the dashboard |
| Webhook Secret (HMAC) | Any random text, at least 16 letters. **Save a copy — you need it in Step 6.** |
| Plural API Base URL | Leave blank for the test server. For live, use `https://api.pluralpay.in`. |
| Payment Link Expiry (Minutes) | Default 1440 (1 day). Change it if you want the link to expire sooner. |

Save. Then click **Test Plural Connection** (a button on the form). A green message means your details are correct. A red message means something is wrong.

> **Warning:** Webhook Secret must be at least 16 letters long. To make one, run `openssl rand -hex 32`.

### Step 5 — Tell the app which forms can take payments

In **Payable Documents**, add one row for each form that should show the payment buttons.

For Sales Invoice:

| Field | Value |
|---|---|
| DocType | Sales Invoice |
| Enabled | ✅ |
| Show on docstatus | `1` (only submitted invoices show the buttons) |
| Pay on Machine | ✅ |
| Send Payment Link | ✅ |
| Amount Field | `outstanding_amount` |
| Create Payment Entry | ✅ |

Add the same kind of row for POS Invoice (`outstanding_amount`), Payment Request (`grand_total`), and any other form you want.

### Step 6 — Tell Pine Labs where to send updates

In the **Plural dashboard**:

| Field | Value |
|---|---|
| Webhook URL | `https://<your-site>/api/method/pinelabs_fin.api.webhook.handle_webhook` |
| Method | POST |
| Secret | The **same** Webhook Secret you saved in Step 4 |

> **Warning:** The secret in the Plural dashboard and the secret in Pinelabs Settings must match exactly. If they do not match, every update from Pine Labs gets rejected and payments stay stuck. The minute-by-minute scheduler will pick them up later, but matching the secrets fixes it right away.

Setup is done. From now on, daily use is just clicking buttons.

---

## 4. Feature 1 — Pay on Machine (terminal)

### When to use it

The customer is **standing in front of you** and ready to tap a card or scan UPI on the Pine Labs machine.

### How to use it

1. Open the submitted Sales Invoice (or POS Invoice / Payment Request) in ERPNext.
2. Click **Pay on Machine** at the top of the page.
3. A small window opens with a Mode of Payment list. Pick **PineLabs - Card** or **PineLabs - UPI**.
4. Click **Start Payment**.
5. A "Waiting for customer..." window appears. The Pine Labs machine beeps with the bill.
6. The customer taps their card or scans UPI.
7. In a few seconds, the window changes to "Status: SUCCESS" and closes on its own.
8. The invoice refreshes. Outstanding is now 0. A new Payment Entry shows under Payments.

Time taken: about 5 to 10 seconds. Most of that is the customer using the machine.

### If the button is missing

The button is hidden when any of these is true:

- The invoice is not submitted (it is still a draft, or it was cancelled).
- Outstanding is already 0 (the invoice is paid).
- In Payable Documents, **Pay on Machine** is unticked or **Enabled** is unticked.

If you think the button should be there but it is not, open the browser developer tools (press F12) and look at the Console tab. Lines that start with `[Pinelabs] skip:` tell you why the button was hidden.

### What to do if it fails

> **Safety: no double-charge.** Five layers of protection apply, including a row-level lock and **DB-level UNIQUE constraints** on `payment_id` and `order_id`. Even if a webhook retry, the per-minute scheduler, and a manual Refresh Status all race for the same transaction, only one Payment Entry is created. The app also blocks a second click while a payment is still INITIATED or PENDING — you will see: "A Terminal payment (PLT-...) is already in progress for this document." A fresh attempt is only allowed after the first reaches SUCCESS / FAILED / CANCELLED. Details: see [§ How no-duplicate is guaranteed](#how-no-duplicate-is-guaranteed) at the bottom of this README.

| Problem | What you see | What to do |
|---|---|---|
| Card was rejected | Red message: "Payment FAILED" | The payment is now FAILED (a final state). Click the button again to start a fresh attempt — the app allows this because the first attempt is no longer "in progress". |
| Customer waited too long | Window stays open | Click **Cancel** in the window. The payment is CANCELLED. The Pine Labs machine cancels too. |
| Cannot reach Pine Labs | Error: "Failed to reach Pine Labs" | Check your internet and try again. |
| You closed the window by mistake | Payment is still PENDING | **Do not click the button again** — the app will reject the second click with "A Terminal payment is already in progress for this document." The scheduler will finish the first attempt in a minute. To check sooner, open the Pinelabs Transaction record and click **Refresh Status**. |

### Picking the right machine (more than one machine)

If you set **Routing Mode** to **Mapping** and added rules, the app picks the right machine on its own. It looks at who you are, which warehouse the invoice uses, and so on. You do not have to choose.

---

## 5. Feature 2 — Send Payment Link (online)

### When to use it

The customer is **not in front of you**. Maybe it is a phone order, a remote customer, or you need the money in advance. The customer will pay later, on their own phone, in their own time.

### How to use it

1. Open the submitted invoice in ERPNext.
2. Make sure the customer has an **email** or a **mobile number** filled in. Without one of these, the link cannot be sent.
3. Click **Send Payment Link**. There is no extra window — every click makes one link that works for all payment methods.
4. A message shows the payment link URL.
5. **Pine Labs sends the link to the customer by SMS and email on its own.** You do not write or send any messages.
6. The customer opens the link, picks a payment method (card, UPI, net banking, or wallet) and pays.
7. When the customer pays, Pine Labs tells the app. The app makes the Payment Entry, and Outstanding becomes 0.

Default link life: 1 day. You can change this in Pinelabs Settings → Plural API.

### How the app finds the customer's email and mobile

The app needs the customer's email and mobile to give to Plural. It tries to find them in this order:

1. **Per-doctype Customer Contact Field Mapping** (set on the Payable Documents row).
2. Common fields on the invoice itself (`contact_mobile`, `contact_email`, `mobile_no`, `email_id`).
3. The customer's main Contact record.

If it still cannot find them, the click fails with a clear message that tells you what it tried. Fix the mapping or fill in the customer's record.

The simplest mapping for Sales Invoice:

```
Mobile — Fetch From: Linked DocType
  Link Field:        contact_person
  Linked DocType:    Contact
  Field in Linked:   mobile_no

Email  — Fetch From: Linked DocType
  Link Field:        contact_person
  Linked DocType:    Contact
  Field in Linked:   email_id
```

### Tracking the customer's payment

After you click Send, a Pinelabs Transaction record is made with status PENDING. You can:

- Open the **Pinelabs Transaction** list to see all open links.
- Open one record and click **Refresh Status** to ask Plural for the latest state right now.
- Or wait — every minute the scheduler checks all open links. When the customer pays, status becomes SUCCESS within a minute.

### What to do if it fails

> **Safety: no double-link, no duplicate Payment Entry.** While a payment link is PENDING (customer has not paid yet), clicking Send Payment Link again is **rejected** with "A Plural payment (PLT-...) is already in progress for this document." Wait for the first link to either be paid (SUCCESS), expire (FAILED), or cancel it manually before sending a new one. Webhook redeliveries are deduplicated at the database via `Pinelabs Webhook Event.event_id` UNIQUE — a replayed webhook returns 200 OK and never re-runs the handler. Details: see [§ How no-duplicate is guaranteed](#how-no-duplicate-is-guaranteed) at the bottom of this README.

| Problem | What you see | What to do |
|---|---|---|
| Customer has no email or mobile | Error message with details | Add the email/mobile to the customer, or fix the Contact Field Mapping. |
| Customer never pays | Link stays PENDING until it expires | The scheduler marks it FAILED after it expires. Then click Send Payment Link again to make a new link. (Trying to click before the first link expires is rejected.) |
| Wrong Plural login details | Error: "You are not authenticated" | Re-check `plural_client_id` and `plural_client_secret`. Use **Test Plural Connection** to check. |
| Update from Pine Labs did not arrive | Payment stays PENDING for over a minute | The scheduler will finish it. Or open the record and click **Refresh Status**. |

---

## 6. Managing payments

### Where to find all payments

In ERPNext, open the Awesome Bar and type **Pinelabs Transaction**. You see a list of every payment try with its status.

### What each status means

| Status | What it means |
|---|---|
| INITIATED | Just created. App is talking to Pine Labs / Plural. |
| PENDING | Waiting for the customer (or for an update from Pine Labs). |
| SUCCESS | Done. Payment Entry was made (or the status field was updated). |
| FAILED | Done, but the payment did not work. No Payment Entry. |
| CANCELLED | The cashier cancelled, or it timed out and was cancelled. |

A healthy site has **no** PENDING rows older than 5 minutes. If you see older PENDING rows, the scheduler is off or the customer has not paid yet.

### Refresh Status button

Open any PENDING Pinelabs Transaction and click **Refresh Status**. The app asks Plural or Pine Labs for the latest state right now. This is the same thing the scheduler does, but you control when.

### Looking inside a payment

Every Pinelabs Transaction record keeps:

- **request_payload** — what was sent to Pine Labs
- **response_payload** — what came back (the webhook reply for online payments)
- **error_message** — what went wrong (when status = FAILED)

When you have a problem, these three fields show you exactly what each side said.

---

## 7. Common admin jobs

### Adding a new card machine

1. Pinelabs Settings → Machines → Add Row.
2. Type a `machine_name` and the 5 login details from the Pine Labs welcome email.
3. Tick `enabled`.
4. Tick `is_default` only if this should be the new fallback machine. Untick it on the old default.
5. Save.

### Adding a new form (doctype) that can take payments

1. Pinelabs Settings → Payable Documents → Add Row.
2. Pick the DocType (for example "Delivery Note" if you take cash on delivery).
3. Fill in `amount_field` (for example `grand_total`).
4. If the form is **not** Sales Invoice / POS Invoice / Payment Request:
   - Untick **Create Payment Entry**.
   - Set `status_field` (the field on the form to update) and `paid_status_value` (the value to put in it).
5. If you will use Send Payment Link, also set the Customer Contact Field Mapping.
6. Save.

> **Note:** The app can only make a Payment Entry on its own for Sales Invoice, POS Invoice, and Payment Request. For other forms, use the status-field option in step 4.

### Hiding only one button on a form

Open the Payable Documents row → untick **Pay on Machine** or **Send Payment Link**. Only that button is hidden on that form.

### Showing buttons on Drafts too

Open the Payable Documents row → set **Show on docstatus** to `0,1`. Drafts now show the buttons. (Cancelled invoices never show them.)

### Turning a machine off for a while

Untick `enabled` on the Machine Config row. Rules that point to this machine are skipped while it is off. Tick it again to turn it back on.

### Changing your secrets

When you change your Plural Client Secret or Webhook Secret:

1. Update Pinelabs Settings.
2. Save. The app clears its saved login token on its own.
3. If you changed the Webhook Secret, change it in the Plural dashboard too.

Both sides must match for updates from Pine Labs to be accepted.

---

## 8. Quick fixes

| Problem | First thing to try |
|---|---|
| Button does not show on invoice | Open browser DevTools (F12) → Console tab. Look for `[Pinelabs] skip:` lines. They tell you why. |
| "Pay on Machine" gives an error | Open the Pinelabs Transaction record → read `error_message`. Usually the machine details are wrong or the machine is off. |
| "Customer email or mobile is required" | Fix the Customer Contact Field Mapping, or fill in the customer record. |
| Payment Entry not made after success | The status is SUCCESS but the doctype is not Sales Invoice / POS Invoice / Payment Request. Set `finalize_via_payment_entry = 0` and set a status field. |
| Payment stuck in PENDING | Wait one minute for the scheduler, or open the record and click **Refresh Status**. Still stuck? The webhook from Pine Labs never came — check your Plural dashboard. |
| Webhook returns 401 | The Webhook Secret in Pinelabs Settings and in the Plural dashboard are different. Make them the same. |

For deeper checks, every Pinelabs Transaction record shows the full request and reply data. Open it to see what each side said.

---

## 9. License and help

MIT. See [license.txt](license.txt).

- **Bug reports:** open a GitHub issue. Add the `request_payload`, `response_payload`, and the `[PineLabs]` / `[PineLabs PayByLink]` log lines from your `bench start` terminal.
- **Questions:** read [PINELABS_FIN_GUIDE.txt](PINELABS_FIN_GUIDE.txt) for deeper notes on how the app works inside.

---

## How no-duplicate is guaranteed

Six independent layers protect against a customer being charged twice or a Payment Entry being created twice. The first three are app-level; the last three are **database-level constraints** that hold even if app code has a bug.

| # | Layer | Where | Catches |
|---|---|---|---|
| 1 | "In progress" check on click | `terminal.py:50`, `plural.py:73` | Rapid double-click; second tab; cashier panicking |
| 2 | Row-level lock (`SELECT ... FOR UPDATE`) | `transaction.py:_lock_and_reload` | Webhook + cron + manual refresh racing to finalize the same transaction |
| 3 | Status re-read after acquiring the lock | `transaction.py:155` | Caller arrives after another caller already set SUCCESS |
| 4 | PE-by-reference_no check | `transaction.py:343` | A second transaction somehow tries to finalize the same Pine Labs payment |
| 5 | **Controller-level uniqueness on `Pinelabs Transaction.payment_id` and `.order_id`** | `pinelabs_transaction.py:_enforce_unique_provider_ids` | Two `Pinelabs Transaction` rows ever sharing the same provider id — *for new saves*. Legacy test rows that already share an id are grandfathered in; the rule only blocks NEW saves that would introduce a duplicate. |
| 6 | **DB UNIQUE on `Pinelabs Webhook Event.event_id`** | `pinelabs_webhook_event.json` + `webhook.py:_claim_webhook_event` | Plural redelivering the same webhook event — the duplicate insert fails, handler returns 200 immediately without re-dispatching |

Layers 5 and 6 are the strong promises. They are enforced by MariaDB itself — not by application code — so they hold even if a future change introduces a bug in the dedup logic.

### What this does not protect

- **Two separate physical attempts on different terminals at the same time.** Layer 1 blocks this for the *same* invoice, but if cashier A starts on one invoice and cashier B starts a different attempt on the same invoice across two terminals, you'd get two transactions with different `payment_id` values. This is by design — each attempt is an independent payment session at Pine Labs.
- **Refunds.** No refund flow exists yet.
- **Manual Payment Entry edits.** An admin who manually edits a Payment Entry in the desk can break the link with the Pinelabs Transaction. The PE-by-reference_no check (layer 4) still prevents a *second* PE, but the existing PE's amount could diverge from the Pine Labs transaction. Treat Payment Entries created by this app as read-only.
