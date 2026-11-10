# pinelabs_finstein: browser (Desk UI) test cases for v16

Status: RUN on 2026-10-31 in Chrome against pine.local v16 (see Results at the end).
Site `pine.local`, ERPNext `http://127.0.0.1:8024`, simulator UI `http://127.0.0.1:8080`, simulator API `:5000`.

## Preconditions
- Pinelabs Settings: enabled, active API endpoint -> `http://127.0.0.1:5000`, machine `Local Simulator Machine`.
- Payable Documents row for `Sales Invoice` with both buttons on. `Enable Payment Links` ticked for section D only.
- `PineLabs - Card / UPI / Payment Link` each have a default account for the company.
- Simulator in `manual` mode, webhook off (turned on for D5).
- Users: Administrator, a cashier (Accounts User or Sales User), a user with no Sales Invoice access.
- Data: a submitted unpaid invoice (1000), a draft, a paid, a cancelled, a customer with email and mobile, a customer with none.

## A. Buttons on the source document
| ID | Scenario | Steps | Expected |
|---|---|---|---|
| A1 | Buttons show on a submitted unpaid invoice | Open the invoice | "Pay on Machine" and "Send Payment Link" present |
| A2 | Hidden on a paid invoice | Open the paid invoice | No Pinelabs buttons |
| A3 | Hidden on a cancelled invoice | Open it | No buttons |
| A4 | Draft shown only if configured (show on docstatus) | Open the draft | Matches the Payable Documents row |
| A5 | Hidden on a non-payable doctype | Open a Customer | No buttons |
| A6 | Per-row toggle respected | Untick Send Payment Link on the row, reload invoice | Only Pay on Machine shows |
| A7 | Master switch off | Untick Enable Payment Links | Link button gone / refused |
| A8 | Cashier sees buttons without Settings access | Log in as cashier, open invoice | Buttons show; no permission error in console |

## B. Pay on Machine (terminal)
| ID | Scenario | Steps | Expected |
|---|---|---|---|
| B1 | Card approved | Pay on Machine, Card; in simulator resolve success | Dialog "Waiting for terminal" closes with success, invoice Paid, 1 Payment Entry |
| B2 | UPI approved | Same with UPI | Payment Entry mode `PineLabs - UPI` |
| B3 | Declined | Resolve decline | Dialog shows failure, invoice unpaid, no Payment Entry, Retry offered |
| B4 | Cashier cancels while waiting | Click Cancel in dialog | Transaction CANCELLED, terminal released |
| B5 | Late success after cancel | Cancel, then resolve success in simulator | Stays CANCELLED, no Payment Entry |
| B6 | Timeout | Leave unresolved | Never records a payment |
| B7 | Part payment | Enter 400 on a 1000 invoice | Outstanding 600, status Partly Paid |
| B8 | Amount above payable | Enter 1500 | Refused before the terminal is contacted |
| B9 | Zero / negative amount | Enter 0 and -5 | Refused |
| B10 | Second payment while one is active | Start again on the same invoice | Refused (active transaction) |
| B11 | Close dialog / reload page mid-payment | Reload while waiting | Open transaction, "Watch Terminal" resumes the dialog; payment still completes |
| B12 | Cron finishes it | Close the tab, resolve in simulator, wait 1-2 min | Transaction SUCCESS without any click |
| B13 | Error text is escaped | Make the gateway return text with `<script>` | Shown as text, no script runs |

## C. Pinelabs Transaction form
| ID | Scenario | Steps | Expected |
|---|---|---|---|
| C1 | Status pill and links | Open a SUCCESS transaction | Green status; View Payment Entry / View Source Document work |
| C2 | Live update | Keep a PENDING terminal transaction open, resolve in simulator | Form reloads itself and shows an alert |
| C3 | Cancel Terminal button | Open PENDING terminal txn, Actions, Cancel | CANCELLED |
| C4 | Retry Terminal | Open FAILED terminal txn, Actions, Retry | New transaction created, old one kept |
| C5 | Retry not offered on SUCCESS | Open SUCCESS txn | No Retry |
| C6 | Manual edits blocked | As System Manager edit amount / status | Rejected (service-owned fields) |
| C7 | Cannot create manually | New Pinelabs Transaction in Desk | Not allowed |
| C8 | Cannot delete SUCCESS | Try delete | Not allowed |

## D. Send Payment Link (Plural)
| ID | Scenario | Steps | Expected |
|---|---|---|---|
| D1 | Link created | Send Payment Link with email and mobile | Dialog shows link URL; transaction PENDING |
| D2 | Customer pays | Open link in simulator, pay | Refresh Status or cron: SUCCESS, Payment Entry in `PineLabs - Payment Link`, invoice Paid |
| D3 | Payment fails once | Fail the attempt in simulator | Invoice unpaid; link still payable; transaction not FAILED |
| D4 | No contact details | Customer with no email/mobile and none typed | Refused before Plural is called |
| D5 | Webhook path | Turn simulator webhook on, pay | Finishes without clicking Refresh |
| D6 | Second link while one is active | Send again | Refused |
| D7 | Terminal payment while link is active | Pay on Machine | Refused |
| D8 | Bad mobile number | Enter `abc`, `123` | Clean validation message, no crash |
| D9 | Refresh Status twice after success | Click twice | Still one Payment Entry |

## E. Permissions and security (browser)
| ID | Scenario | Steps | Expected |
|---|---|---|---|
| E1 | User without invoice access | Log in, try Pay on Machine URL/API | Same "not found or no permission" message as for a missing invoice |
| E2 | Cashier cannot open Settings | Open Pinelabs Settings | Permission denied |
| E3 | GET on a payment endpoint | Open `/api/method/pinelabs_finstein.api.terminal.initiate_terminal_payment` in the address bar | Rejected (POST only) |
| E4 | No secrets in the browser | Network tab on payment calls | No client secret / SecurityToken in responses |
| E5 | Cashier cannot read other users' transactions | Open a transaction on a document the cashier cannot see | Denied with the uniform message |
| E6 | Webhook without signature | POST to webhook with no/invalid `X-Verify` | Rejected |
| E7 | Webhook amount mismatch | Signed event with a different amount | 409, no Payment Entry |

## F. Settings page
| ID | Scenario | Steps | Expected |
|---|---|---|---|
| F1 | Reject insecure URL | Save base URL `http://example.com` | Refused (https required) |
| F2 | Loopback http allowed | Save `http://127.0.0.1:5000` | Saved |
| F3 | Test Plural connection | Click Test Connection | Success or clear error, no secret shown |
| F4 | Missing account mapping | Remove the mode's default account, Pay on Machine | Refused before the terminal is contacted |

## Out of scope for the simulator
Real terminal hardware, real Plural payloads, real money. Those need UAT.

## Results (2026-10-31)

Browser = real clicks / the app's own front-end functions in Chrome. HTTP = curl or scripted calls as a real user.

| Group | Result | Notes |
|---|---|---|
| A1-A7 | Pass | A4: draft hidden because the row says "show on submitted". |
| A8 | Pass (scripted) | Sales User gets button config; run as that user, not in a second browser. |
| B1-B12 | Pass | B1 found the PTRN-collision bug (fixed). B5: simulator itself refused the late success, so the app side was not exercised here. |
| B13 | Pass for the transaction form | The dialog's own escaping was not exercised (simulator cannot send arbitrary error text). |
| C1-C6, C8 | Pass | C7 not a clean test: Administrator bypasses create permission. |
| D1 | Pass after fix | Found the terminal dialog opening for payment links (fixed in `pinelabs_buttons.js`). |
| D2-D9 | Pass | D8 found an invalid mobile being accepted (fixed). D5 sent as a signed webhook by curl. |
| E1-E7 | Pass | Users: a Sales user and a no-role user (scripted). |
| F1-F4 | Pass | |

Bugs found and fixed during this run: (1) a repeated gateway reference adopted another invoice's Payment Entry;
(2) the Send Payment Link button opened the terminal "Waiting" dialog; (3) an invalid inline mobile number was accepted.
