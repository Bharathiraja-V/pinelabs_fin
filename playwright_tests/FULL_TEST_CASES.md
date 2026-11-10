# pinelabs_finstein v16: full test case specification

Status: **DRAFT for review. Nothing in this file has been run.** Results columns are blank on purpose.
App branch `version-16`. Site `pine.local`. ERPNext `http://127.0.0.1:8024`. Simulator API `:5000`, simulator UI `:8080`.
Supersedes `UI_TEST_CASES.md` (a 48-case subset that was already run on 2026-10-31).

## How to read this

- **Type**: `UI` = real clicks in Chrome, `API` = HTTP/RPC call as a named user, `DB` = check stored data, `SIM` = needs the simulator to answer a particular way, `REAL` = cannot be done on the simulator (UAT).
- **Pri**: `P1` must pass before release, `P2` should pass, `P3` nice to have.
- **R** = result column (fill in: Pass / Fail / Blocked / N/A).
- `Inv` = a fresh submitted, unpaid Sales Invoice of INR 1000 unless stated.
- "No gateway call" means the simulator's transaction count must not change.

## 0. Environment, data and users

**Services**: ERPNext web (8024), scheduler and worker running, Redis up, simulator API (5000) and UI (8080) up.
**Simulator**: `manual` outcome mode, webhook off unless a case says otherwise. Reset before each group.
**Pinelabs Settings** (baseline): Enabled; one active API endpoint -> `http://127.0.0.1:5000`; machine `Local Simulator Machine`;
Payable Documents row for `Sales Invoice` (enabled, Pay on Machine on, Send Payment Link on, show on submitted, amount field `outstanding_amount`,
Create Payment Entry on); `Enable Payment Links` on only in groups that need it; Plural base URL -> simulator, demo credentials, demo webhook secret
(restore the real values afterwards).
**Modes of Payment**: `PineLabs - Card`, `PineLabs - UPI`, `PineLabs - Payment Link`, each with a default account for the test company.
**Users**: Administrator; `cashier` (Sales User + Accounts User, no System Manager); `salesonly` (Sales User only); `noaccess` (no Sales/Accounts roles); `sysmgr` (System Manager, no accounts roles).
**Documents** (create fresh per case when the case changes them): submitted unpaid, draft, paid, cancelled, partly paid, return/credit note, zero-value, foreign-currency (USD), POS Invoice, Payment Request, a custom doctype with and without a Payable Documents row.
**Customers**: one with email and mobile, one with neither, one with only email, one with only mobile.

---

## A. Install, upgrade and seed data

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| A1 | P1 | DB | Fresh install seeds exactly three Modes of Payment | Install app on a clean site; list Mode of Payment | `PineLabs - Card`, `PineLabs - UPI`, `PineLabs - Payment Link` exist, enabled, no custom fields added to Mode of Payment | |
| A2 | P1 | DB | Upgrade seeds modes | On a site without the modes, run `bench migrate` | Modes created by the patch | |
| A3 | P1 | DB | Migrate is idempotent | Run migrate twice | No duplicate rows, no error | |
| A4 | P1 | DB | Credential scrub patch | Insert a transaction/Version row containing a fake SecurityToken and client secret; run the patch | Values replaced by `***REDACTED***`; other data untouched | |
| A5 | P2 | DB | No custom fields on core doctypes | Inspect Custom Field list | None on Mode of Payment, Sales Invoice, POS Invoice, Payment Entry, Customer, Contact | |
| A6 | P2 | DB | Uninstall leaves no orphan hooks | Uninstall on a copy | Payment Entry validate hook removed; no errors in Error Log | |
| A7 | P1 | DB | Upgrade with existing transactions | Upgrade a copy that has PENDING/SUCCESS/FAILED transactions | Rows intact, statuses unchanged, no data loss | |
| A8 | P1 | API | Behaviour change: doctype with no Payable Documents row | After upgrade, initiate on a doctype with no row | Refused with "not configured" (release note must say so) | |

## B. Pinelabs Settings and configuration

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| B1 | P1 | UI | Plural URL must be https | Save `http://example.com` | Refused | |
| B2 | P1 | UI | Credentials in URL refused | Save `https://user:pw@host` | Refused | |
| B3 | P1 | UI | Non-http scheme refused | Save `ftp://x.y` | Refused | |
| B4 | P1 | UI | Loopback http allowed | Save `http://127.0.0.1:5000` | Saved | |
| B5 | P1 | UI | Terminal endpoint with `verify_ssl` off on a non-loopback host | Save in production mode | Refused; allowed only on loopback or developer mode | |
| B6 | P2 | UI | Developer mode exemption | Turn on developer mode, save an http non-loopback URL | Allowed; refused again with developer mode off | |
| B7 | P1 | API | Per-request URL re-check | Change the stored URL directly in the DB to `http://evil.test`, then initiate a link | Request refused before any network call | |
| B8 | P1 | UI | Test Plural connection (good credentials) | Click Test Connection | Success message with expiry; no secret shown in response or console | |
| B9 | P1 | UI | Test Plural connection (bad credentials) | Use a wrong secret | Clear error; secret not echoed | |
| B10 | P2 | UI | Test connection with Plural unreachable | Stop simulator | Clear error, no stack trace to the user | |
| B11 | P1 | API | Settings read/write permissions | As cashier, read and save Settings | Denied | |
| B12 | P1 | API | Password fields not exposed | As System Manager, `frappe.client.get` Settings | Client secret, webhook secret, SecurityToken come back masked or absent | |
| B13 | P2 | UI | Machine routing: Single mode | Mode = single, one machine | Always uses that machine | |
| B14 | P2 | UI | Machine routing: Mapping mode by user | Map user -> machine B | That user's payments go to machine B | |
| B15 | P2 | UI | Mapping by warehouse / company / POS profile | One mapping each | Correct machine each time | |
| B16 | P2 | UI | Mapping falls back to default machine | No rule matches | Default machine used | |
| B17 | P2 | UI | No machine configured | Remove all machines | Refused before gateway with a clear message | |
| B18 | P2 | UI | Inactive endpoint | Mark endpoint inactive | Refused before gateway | |
| B19 | P1 | UI | Master switch: terminal off | Untick Enabled | Pay on Machine refused/hidden | |
| B20 | P1 | UI | Master switch: payment links off | Untick Enable Payment Links | Send Payment Link refused/hidden, terminal still works | |
| B21 | P2 | UI | Payable row disabled | Untick `is_enabled` | Both buttons gone for that doctype | |
| B22 | P2 | UI | Duplicate payable rows for one doctype | Add two rows | Validation message or documented behaviour | |
| B23 | P2 | UI | Configured amount field | Set amount field to `grand_total` on a partly-paid invoice | Charge amount follows the field; cap uses it too | |
| B24 | P2 | UI | Customer contact field mapping | Map mobile/email via direct field, link field, fetch-from | Link created with mapped values | |
| B25 | P3 | UI | Field pickers restricted | As non-System Manager, call `list_doctype_fields` / `get_link_field_target` | Denied | |

## C. Buttons and visibility on the source document

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| C1 | P1 | UI | Submitted unpaid | Open Inv | Pay on Machine and Send Payment Link shown | |
| C2 | P1 | UI | Paid | Open paid invoice | No Pinelabs buttons | |
| C3 | P1 | UI | Cancelled | Open cancelled invoice | No buttons | |
| C4 | P2 | UI | Draft with show-on = submitted | Open draft | No buttons | |
| C5 | P2 | UI | Draft with show-on = draft+submitted | Change row, open draft | Buttons shown | |
| C6 | P1 | UI | Non-payable doctype | Open a Customer | No buttons | |
| C7 | P1 | UI | Per-row toggle | Turn off Send Payment Link on the row | Only Pay on Machine shown | |
| C8 | P1 | UI | Cashier without Settings access | As `cashier`, open Inv | Buttons shown, no permission error in the console | |
| C9 | P2 | UI | `noaccess` user | Open Inv as `noaccess` | Cannot open the doc at all | |
| C10 | P2 | UI | Partly paid invoice | Open | Buttons shown; amount offered = outstanding | |
| C11 | P2 | UI | Return / credit note | Open a return | No buttons (negative outstanding) | |
| C12 | P2 | UI | Zero outstanding | Open a fully paid-by-credit invoice | No buttons | |
| C13 | P2 | UI | POS Invoice | Open submitted unpaid POS Invoice | Buttons per row config | |
| C14 | P2 | UI | Payment Request | Open a Payment Request | Buttons per row config | |
| C15 | P3 | UI | Button after payment without reload | Pay, watch the form | Form refreshes; buttons disappear when Paid | |
| C16 | P3 | UI | Button config failure | Make `get_payable_doctypes` fail | No JS error spam; no buttons | |
| C17 | P3 | UI | Mobile / narrow viewport | Resize to phone width | Buttons and dialog usable | |

## D. Pay on Machine (terminal): happy paths and outcomes

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| D1 | P1 | UI+SIM | Card approved | Click Pay on Machine, choose Card, Start; approve in simulator | Dialog closes, SUCCESS, one Payment Entry (mode Card, 1000), invoice Paid | |
| D2 | P1 | UI+SIM | UPI approved | Same with UPI | Payment Entry mode `PineLabs - UPI` | |
| D3 | P1 | UI+SIM | Declined | Decline in simulator | Dialog "Payment failed - TRANSACTION DECLINED", Close and Retry offered; invoice unpaid; no PE | |
| D4 | P1 | UI+SIM | Timeout / no response | Resolve as timeout | FAILED "NO RESPONSE", no PE | |
| D5 | P1 | UI | Cashier cancels in the dialog | Click Cancel | CANCELLED, terminal cancel sent, no PE | |
| D6 | P1 | UI+SIM | Late success after cancel | Cancel, then force a "success" for the same PTRN (backend call to the poll) | Stays CANCELLED, no PE | |
| D7 | P1 | UI | Retry from the failure dialog | Click Retry | New transaction created, old FAILED kept; success path works | |
| D8 | P1 | UI | Mode choices | Open the dialog | Only Card and UPI listed (not Payment Link) | |
| D9 | P2 | UI | Dialog is locked | Click outside / press Esc while waiting | Dialog stays until Cancel or result | |
| D10 | P2 | UI | Dialog shows transaction id and spinner | Observe | `PLT-…` shown | |
| D11 | P1 | UI | Reload mid-payment | Reload page while waiting | Transaction stays PENDING; Watch Terminal on its form resumes; payment completes | |
| D12 | P1 | UI | Two tabs, same invoice | Start in tab 1, try in tab 2 | Tab 2 refused ("already in progress") | |
| D13 | P1 | UI | Cron finishes unattended | Start, close the tab, approve in simulator, wait 1-2 min | SUCCESS with PE, nobody watching | |
| D14 | P1 | UI | Browser poll and cron race | Approve; let poll and cron fire together | Exactly one PE | |
| D15 | P1 | DB | Payment Entry content | Inspect PE after D1 | Party, mode of payment, paid amount, reference_no = PTRN, reference date, remarks include the `PLT-…` name, allocated to the invoice | |
| D16 | P2 | DB | GL entries | Inspect GL for the PE | Debit mode-of-payment account, credit receivable, balanced | |
| D17 | P2 | UI | Realtime update on the invoice | Watch the invoice while paying | Status updates without manual reload | |
| D18 | P2 | UI | Realtime event only to the owner | Pay as user A while user B has the invoice open | B's UI is not forced into a dialog | |
| D19 | P3 | UI | Slow terminal (10+ s) | Hold resolution back | Dialog keeps polling; no duplicate requests flood | |
| D20 | P2 | UI+SIM | Simulator random mode | Set `random` mode, pay 10 invoices | Every outcome ends in a consistent state; no PENDING left behind after timeouts | |

## E. Pay on Machine: amounts, guards and pre-flight

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| E1 | P1 | API | Part payment | `amount=400` on Inv | SUCCESS, PE 400, invoice Partly Paid, outstanding 600 | |
| E2 | P1 | API | Two part payments settle it | 300 then 700 | Two PEs, invoice Paid | |
| E3 | P1 | API | Amount above payable | `amount=1500` | Refused, no transaction row, no gateway call | |
| E4 | P1 | API | Zero / negative | `0`, `-5` | Refused, no transaction row | |
| E5 | P2 | API | Fractions | `amount=0.01`, `100.555`, `999.999` | Rounds to 2 places consistently; never charges a nonsensical value | |
| E6 | P2 | API | Non-numeric amount | `"abc"`, `null`, huge number, `1e308` | Clean error, no 500 | |
| E7 | P1 | API | Paid invoice | Initiate on paid invoice | Refused | |
| E8 | P1 | API | Cancelled invoice | Initiate | Refused | |
| E9 | P1 | API | Draft invoice | Initiate with row allowing drafts | Accepted; auto-submitted once paid | |
| E10 | P1 | API | Non-existent document | Initiate on a missing name | Refused with the uniform "not found or no permission" message | |
| E11 | P1 | API | Non-payable doctype | Initiate on a Customer | Refused ("not configured") | |
| E12 | P1 | API | Unknown / blank / wrong mode | `Cash`, `Credit Card`, `""`, `PineLabs - Payment Link` | Refused before gateway | |
| E13 | P1 | API | Second initiate on same document | Start twice | Second refused | |
| E14 | P1 | API | Link active, then terminal | Create link, then Pay on Machine | Refused | |
| E15 | P1 | API | Mode has no default account | Remove the account mapping | Refused before gateway, no transaction | |
| E16 | P2 | API | Custom doctype needs a Customer party | Custom doctype without customer, `create_payment_entry=1` | Refused before gateway | |
| E17 | P1 | API | Gateway down | Stop simulator, initiate | Clean error; no transaction left INITIATED or PENDING | |
| E18 | P1 | SIM | Gateway returns invalid PTRN | Sim returns error for the upload | Never SUCCESS; transaction FAILED | |
| E19 | P2 | SIM | Gateway returns 500 / malformed JSON | Force it | Clean error, no hung PENDING | |
| E20 | P2 | SIM | Gateway slow (timeout) on initiate | Delay beyond client timeout | Clean error; no double charge on retry | |
| E21 | P2 | API | Foreign-currency invoice | Initiate on USD invoice | Refused (INR only) | |
| E22 | P2 | API | Manual Payment Entry blocked while active | Create a PE for the invoice while a transaction is PENDING | Blocked by the conflict guard | |
| E23 | P2 | API | Manual PE allowed after a decline | Same after FAILED | Allowed | |
| E24 | P1 | API | Invoice paid by someone else while the charge is in flight | Pay invoice manually (bypass), then approve the terminal payment | Transaction SUCCESS, no PE, conflict note on the transaction, Error Log entry; no crash | |
| E25 | P1 | API | PTRN collision with an older Payment Entry | Make a PE for another invoice with the same `reference_no`; approve this one | This invoice gets its own PE; the other is untouched (regression for the bug found) | |
| E26 | P2 | API | `create_payment_entry=0` | Initiate with 0 | SUCCESS recorded, no PE, source untouched | |
| E27 | P2 | API | `create_payment_entry=1` on a custom doctype | Initiate | PE built by the custom path; party resolved | |
| E28 | P2 | API | `finalize_via_payment_entry=0` on the row | Pay | No PE, invoice untouched | |
| E29 | P2 | API | Transaction reference field | Set the field on the row, pay | Field on the source doc gets `PLT-…`; a bad field name does not roll back SUCCESS | |
| E30 | P2 | API | Draft auto-submit failure | Draft with missing mandatory data, pay | Customer charged is recorded SUCCESS with a clear note; no silent loss | |

## F. Transaction state machine and the Pinelabs Transaction form

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| F1 | P1 | DB | Legal transitions | INITIATED->PENDING->SUCCESS / FAILED / CANCELLED | Allowed | |
| F2 | P1 | DB | Terminal states are final | Try SUCCESS->FAILED, FAILED->PENDING, CANCELLED->SUCCESS via the service | Refused | |
| F3 | P1 | API | Manual status edit | As System Manager set status by `set_value` | Refused | |
| F4 | P1 | API | Manual edit of amount, payment_id, order_id, machine, payloads, references | Edit each | Refused ("set by the Pinelabs transaction service") | |
| F5 | P1 | API | Manual create | As `sysmgr` insert a transaction | Not allowed (no create permission) | |
| F6 | P1 | API | Delete a SUCCESS / linked transaction | Delete | Refused | |
| F7 | P2 | API | Delete a FAILED unlinked transaction | Delete as System Manager | Allowed or refused per design (record the actual behaviour) | |
| F8 | P1 | UI | Status pill and links | Open SUCCESS txn | Green pill, View Payment Entry, View Source Document | |
| F9 | P1 | UI | Watch Terminal and Cancel Terminal only on PENDING/INITIATED terminal | Open each status | Buttons match | |
| F10 | P1 | UI | Retry Terminal only on FAILED terminal | Open each status | Shown on FAILED (and per code CANCELLED via the API only); not on SUCCESS | |
| F11 | P1 | UI | Retry Terminal creates a new transaction | Click, confirm | New `PLT-…`, browser moves to it | |
| F12 | P1 | UI | Cancel Terminal confirm dialog | Click, No, then Yes | No: nothing; Yes: CANCELLED | |
| F13 | P1 | UI | Refresh Status on a Plural txn | Click on PENDING link txn | Status refreshed | |
| F14 | P2 | UI | Live update | Keep PENDING txn open, approve in simulator | Form reloads itself, alert shown | |
| F15 | P2 | UI | Retry on Source Document | Shown on a recent FAILED txn | Opens the source doc | |
| F16 | P1 | API | Retry via API on a PENDING or SUCCESS txn | Call `retry_failed_terminal_payment` | Refused (only FAILED or CANCELLED) | |
| F17 | P1 | API | Idempotency | Call each `mark_*` twice | Second call is a no-op | |
| F18 | P2 | DB | Naming | New transactions | `PLT-YYYY-NNNNN`, unique, sequential | |
| F19 | P2 | DB | Timestamps and error message | After a failure | `completed_at`, `error_message` set; error text truncated at the limit | |
| F20 | P1 | UI | HTML in stored text | Plant `<img onerror>` in `payment_method` / `error_message`, open form | Shown as text, nothing executes | |
| F21 | P3 | UI | Form list view | Open list, filters, sort by status | Works, no JS errors | |

## G. Send Payment Link (Plural)

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| G1 | P1 | UI | Link created | Click Send Payment Link (contact on the invoice) | "Payment Link Generated" dialog with URL; **no terminal dialog**; transaction PENDING | |
| G2 | P1 | UI+SIM | Customer pays; Refresh Status | Pay in simulator, click Refresh Status | SUCCESS, PE in `PineLabs - Payment Link`, invoice Paid | |
| G3 | P1 | SIM | Customer pays; cron | Pay; wait 1-2 min, no clicks, webhook off | SUCCESS with one PE | |
| G4 | P1 | SIM | Customer pays; webhook | Send a correctly signed paid event | SUCCESS with one PE | |
| G5 | P1 | SIM | One failed attempt | Fail once | Link still payable, transaction still PENDING, invoice unpaid | |
| G6 | P1 | SIM | Then success after a failed attempt | Pay after G5 | SUCCESS | |
| G7 | P1 | SIM | Link expiry | Let link expire (or expiry event) | Transaction FAILED/expired, invoice unpaid; new link allowed | |
| G8 | P1 | API | No contact details | Customer with neither email nor mobile, none supplied | Refused before Plural; message with resolver trace | |
| G9 | P2 | API | Only email | Customer with email only | Link created | |
| G10 | P2 | API | Only mobile | Customer with mobile only | Link created | |
| G11 | P1 | API | Invalid mobile | `abc`, `123`, `98765 4321x`, `+1` | Refused: "Enter a valid 10-digit mobile number." | |
| G12 | P1 | API | Valid mobile formats | `9876543210`, `+91 98765 43210`, `09876543210` | Normalised to 10 digits; link created | |
| G13 | P2 | API | Invalid email | `not-an-email`, very long string | Clean error or accepted per Plural rules; no crash | |
| G14 | P1 | API | Second link while one is active | Create twice | Second refused | |
| G15 | P1 | API | Terminal payment while a link is active | Pay on Machine | Refused | |
| G16 | P1 | API | Link on paid / cancelled invoice | Create | Refused | |
| G17 | P1 | API | Mode `PineLabs - Payment Link` has no account | Remove mapping | Refused before Plural | |
| G18 | P1 | API | Plural credentials missing | Clear credentials | Clean "credentials missing" error; no transaction left active | |
| G19 | P1 | API | Plural down / 500 / bad JSON / expired token | Force each in the simulator | Clean error; no stuck PENDING; token refreshed on expiry | |
| G20 | P2 | API | Expiry minutes | Pass `expiry_minutes` 5, 0, -1, 100000 | Sensible limits enforced | |
| G21 | P2 | API | Flow option | `flow=all_methods` and an invalid flow | Valid accepted, invalid refused | |
| G22 | P1 | API | Refresh twice after success | Call twice | Still one PE | |
| G23 | P1 | UI | Link URL is escaped in the dialog | Plant HTML in the URL returned by the simulator | Rendered as text | |
| G24 | P2 | UI | Link dialog contents | Open | Link clickable, opens simulator page in a new tab | |
| G25 | P2 | API | Partial amount for a link | `amount` smaller than payable (if supported) or refused | Behaviour documented and consistent | |
| G26 | P3 | UI | Send link twice quickly (double click) | Double-click the button | Only one link created | |

## H. Webhook (Plural)

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| H1 | P1 | API | GET and other methods | GET/PUT/DELETE the webhook URL | 405 / rejected | |
| H2 | P1 | API | Missing `X-Verify` | POST without it | Rejected, no state change | |
| H3 | P1 | API | Wrong signature | POST with `deadbeef` | Rejected | |
| H4 | P1 | API | Signature over a different body | Sign body A, send body B | Rejected | |
| H5 | P1 | API | Webhook secret not configured | Clear the secret, POST | Rejected (fail closed) | |
| H6 | P1 | API | Non-ASCII junk in the signature header | Send `é` | Rejected cleanly, no 500 | |
| H7 | P1 | API | Valid paid event | Signed `ORDER_PROCESSED`, right amount | 200, SUCCESS, one PE | |
| H8 | P1 | API | Replay of the same event id | Send twice | Second returns "already processed"; one PE | |
| H9 | P1 | API | Same payment, new event id | Send again with a new id | No second PE | |
| H10 | P1 | API | Amount mismatch | Signed paid event with different `order_amount` | 409, no PE, transaction stays PENDING | |
| H11 | P2 | API | Non-INR currency | Signed event with USD | Rejected | |
| H12 | P2 | API | No amount in payload | Signed paid event without `order_amount` | Trusted as before (documented) | |
| H13 | P1 | API | Identifier matching | Match by `merchant_order_reference` (PLT-…), `merchant_payment_link_reference`, `payment_link_id`, `order_id` | Each finds the right transaction | |
| H14 | P1 | API | No matching transaction | Signed paid event for an unknown id | Event logged; handled as failed/ignored; no crash; no PE | |
| H15 | P1 | API | `PAYMENT_FAILED` event | Signed failed-payment event | Ignored; transaction stays PENDING (link still payable) | |
| H16 | P1 | API | `ORDER_CANCELLED` / expired / failed | Signed events | Transaction FAILED, no PE | |
| H17 | P2 | API | Unknown event type | Signed unknown event | 200 and logged; no state change | |
| H18 | P1 | API | Empty body / bad JSON / huge body | Signed variants | 400 or rejected; no 500 | |
| H19 | P1 | API | Webhook for an already CANCELLED/FAILED txn then paid | Send paid event | Behaviour defined and safe (no PE on a cancelled txn, or flagged for reconciliation) | |
| H20 | P1 | DB | Webhook Event log | After H7 | One `Pinelabs Webhook Event` row with identifiers only, no card data, no secrets | |
| H21 | P1 | API | Concurrency: webhook + cron + Refresh at once | Fire all three | One PE | |
| H22 | P2 | API | Webhook runs as a trusted system user and restores the session user | Check after the call | No privilege leak to the next request on the same worker | |
| H23 | P2 | API | Rate / flood | 100 valid-signature duplicates | Handled quickly; one PE | |

## I. Reconciliation cron

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| I1 | P1 | SIM | Terminal reconcile finishes success | Leave PENDING, approve | SUCCESS within ~1-2 min | |
| I2 | P1 | SIM | Terminal reconcile finishes decline / timeout | Leave PENDING, decline | FAILED | |
| I3 | P1 | SIM | Plural reconcile | Leave link PENDING, pay | SUCCESS | |
| I4 | P1 | DB | Does not touch final states | SUCCESS/FAILED/CANCELLED rows | Unchanged by the job | |
| I5 | P1 | DB | Old PENDING handling | A PENDING txn older than the expiry/age limit | Resolved or flagged per design | |
| I6 | P2 | SIM | Gateway error during reconcile | Simulator down for a minute | Job logs and continues; next run recovers | |
| I7 | P2 | DB | Many pending transactions | 200 PENDING rows | Job finishes within the interval; no overlap | |
| I8 | P2 | DB | One bad row does not stop the batch | Make one row raise | The others still processed | |
| I9 | P1 | DB | Scheduler disabled | Disable scheduler | Foreground poll and webhook still finish payments (documented) | |
| I10 | P2 | DB | Worker restart mid-job | Restart worker while reconciling | No half-written PE; safe to rerun | |

## J. Permissions and access control

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| J1 | P1 | API | `noaccess` initiates on an existing invoice | Call initiate | Uniform "not found, or you do not have permission" | |
| J2 | P1 | API | `noaccess` initiates on a missing invoice | Call | Identical message and timing class as J1 | |
| J3 | P1 | API | Poll/cancel/retry/refresh on others' transactions | As `noaccess` | Uniform denial, same for missing names | |
| J4 | P1 | API | Frappe's own permission message does not leak | Inspect message log after J1 | Only the uniform message | |
| J5 | P1 | API | `salesonly` can pay on invoices they can write | Initiate | Allowed per Sales Invoice permissions | |
| J6 | P1 | API | Draft of a submittable doctype needs submit permission | User with write but no submit, draft invoice | Refused | |
| J7 | P1 | API | User permissions (company restriction) | User restricted to company X, invoice in company Y | Refused | |
| J8 | P1 | API | Settings read/write | `cashier`, `salesonly` | Denied | |
| J9 | P1 | API | `get_payable_doctypes` | Any desk user | Returns flags only; no credentials, no URLs | |
| J10 | P1 | API | Guest user | Call every whitelisted method without login | Denied (except the signed webhook) | |
| J11 | P1 | API | CSRF | POST without CSRF token from another origin | Rejected | |
| J12 | P1 | API | GET on every payment endpoint | List, call each | 403/405 | |
| J13 | P2 | API | Transaction list visibility | `cashier` opens the list | Sees only what permissions allow | |
| J14 | P2 | API | Webhook Event doctype | Non-admin opens it | Denied | |
| J15 | P1 | API | `list_doctype_fields`, `get_link_field_target` | Non-System Manager | Denied | |
| J16 | P2 | UI | Impersonation | Administrator impersonates `cashier` and pays | Works, audit shows the right user | |

## K. Security

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| K1 | P1 | DB | No secrets in logs | Run a full flow, grep `logs/*.log` | No client secret, bearer token, SecurityToken, webhook secret | |
| K2 | P1 | DB | No secrets in stored payloads | Grep transaction and Version rows | Only `***REDACTED***` | |
| K3 | P1 | DB | Personal data in logs | Grep for email and mobile | Redacted or masked | |
| K4 | P1 | UI | No secrets in network responses | Browser network tab through a full flow | None | |
| K5 | P1 | UI | Stored XSS in every displayed field | Plant payloads in payment_method, error_message, reference fields, link URL, customer name | Shown as text everywhere | |
| K6 | P1 | API | SQL injection in IDs and amounts | `'; drop table` in names and amount | Rejected or harmless | |
| K7 | P1 | API | Path / host spoofing for Plural URL | URLs with `@`, odd ports, IP forms, `localhost.evil.com` | Treated correctly by the URL guard | |
| K8 | P2 | API | SSRF via configured URL | Point Plural URL at an internal address | Documented risk; only System Manager can set it | |
| K9 | P1 | API | Webhook timing / signature compare | Wrong signatures of varying length | Constant-time compare, always rejected | |
| K10 | P2 | API | Request size and malformed content types | Send odd bodies to every endpoint | No 500s | |
| K11 | P1 | API | Error pages | Trigger errors | No stack traces, paths or secrets shown to users | |
| K12 | P2 | API | Rate limiting | Hammer initiate/poll | Documented behaviour; no resource exhaustion | |
| K13 | P1 | DB | `pip-audit` and semgrep | Run CI checks | No new findings; `nosemgrep` suppressions still justified | |
| K14 | P2 | API | Mass assignment | Pass extra fields to initiate | Ignored | |
| K15 | P1 | API | Over-charge via manipulated amount | Any route to charge more than payable | None found | |
| K16 | P1 | API | Double charge via double submit | Fire two initiates simultaneously | One transaction | |
| K17 | P2 | UI | Clickjacking / frames | Load the form in an iframe | Frappe defaults apply | |

## L. Concurrency and data integrity

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| L1 | P1 | API | Four pollers on one approved txn | 4 parallel `poll_terminal_status` | One PE | |
| L2 | P1 | API | Poll + cron + manual refresh | Together | One PE | |
| L3 | P1 | API | Poll while cancel is clicked | Race them | One consistent final state; never SUCCESS-after-CANCELLED without a PE note | |
| L4 | P1 | DB | Crash after charge, before PE | Kill the worker inside `mark_success` | On rerun, SUCCESS and one PE; nothing lost | |
| L5 | P1 | DB | `mark_success` commit survives later failure | Raise after `mark_success` in the request | PE and SUCCESS persisted | |
| L6 | P2 | DB | Savepoint rollback | Force PE validation failure | Transaction state consistent, error recorded | |
| L7 | P2 | DB | Isolation level | Check MariaDB `tx_isolation` / `innodb_snapshot_isolation` | Locking read returns latest committed row (record the version) | |
| L8 | P2 | DB | Deadlock handling | Many simultaneous finalizations | No unhandled deadlock error to the user | |
| L9 | P1 | DB | Invoice outstanding matches PEs | After every case | Sum of allocations = grand total - outstanding | |
| L10 | P1 | DB | Orphan check | After the whole run | No SUCCESS txn without PE (unless documented no-PE paths), no PE without a txn | |

## M. Other doctypes and finalization modes

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| M1 | P1 | UI | POS Invoice | Pay | PE via ERPNext builder; invoice paid | |
| M2 | P1 | UI | Payment Request | Pay | PE built; Payment Request status updated | |
| M3 | P2 | API | Custom doctype with row and `create_payment_entry=1` | Pay | Standalone PE with party, no references | |
| M4 | P2 | API | Custom doctype with row, no PE | Pay | Transaction SUCCESS, source untouched | |
| M5 | P1 | API | Custom doctype with no row | Pay | Refused | |
| M6 | P2 | API | Custom doctype misconfigured (finalize on, not PE-native, no override) | Initiate | Refused at click time by `validate_finalization_ready` | |
| M7 | P2 | API | Amount field missing on the doc | Row points to a field that does not exist | Clear error, no gateway call | |
| M8 | P2 | API | Multi-company | Invoice in a second company with and without accounts | Refused without; works with | |
| M9 | P2 | API | Multiple currencies on accounts | Account in USD | Clear refusal | |
| M10 | P3 | API | Very long customer name / remarks | Pay | PE remarks and Plural payload handled | |

## N. UI behaviour and compatibility (Chrome)

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| N1 | P1 | UI | Console clean | Run a full flow with the console open | No uncaught errors from `pinelabs_buttons.js` | |
| N2 | P1 | UI | Hard reload picks up new JS | Edit JS, bump version, hard reload | New behaviour present | |
| N3 | P2 | UI | Desk theme dark / light | Open dialogs | Readable | |
| N4 | P2 | UI | Language | Switch language | Strings in `__()` translate; no layout break | |
| N5 | P2 | UI | Browser back/forward during payment | Navigate away and back | Dialog state consistent; no duplicate dialogs | |
| N6 | P2 | UI | Multiple Pay clicks | Click Pay on Machine repeatedly | One dialog, one transaction | |
| N7 | P2 | UI | Session expiry mid-payment | Expire the session while waiting | Clean login prompt; payment still finalizes server-side | |
| N8 | P3 | UI | Other browsers | Firefox, Edge | Same behaviour | |
| N9 | P3 | UI | Accessibility | Keyboard-only through dialogs | Focus order, labels, Esc behaviour | |
| N10 | P1 | UI | No terminal dialog for payment links | Send a link; watch for a "Waiting for terminal" dialog | None (regression for the bug found) | |
| N11 | P2 | UI | Wrapper APIs (fin_pos) still open the terminal dialog | Call `fin_pos`'s initiate wrapper | Dialog opens as before | |

## O. Observability and operations

| ID | Pri | Type | Scenario | Steps | Expected | R |
|---|---|---|---|---|---|---|
| O1 | P2 | DB | Error Log entries are useful | Trigger a conflict | `Pinelabs PE conflict` entry with a readable message | |
| O2 | P2 | DB | Log volume | Run 100 payments | Logs stay bounded; no payload dumps | |
| O3 | P2 | DB | Backup and restore | Restore a backup | Transactions intact and consistent with PEs | |
| O4 | P2 | DB | Time zone | Run with a non-UTC site timezone | Expiry and `completed_at` are correct | |
| O5 | P3 | DB | Performance | 1000 transactions in the list; open forms | Acceptable load time | |
| O6 | P2 | DB | Gateway `Request-Timestamp` format | Inspect a Plural request | Valid ISO UTC | |

## P. Real gateway (UAT; cannot run on the simulator)

| ID | Pri | Type | Scenario | Expected | R |
|---|---|---|---|---|---|
| P1 | P1 | REAL | Real terminal: card approved, UPI approved | PTRN stored; PE with the real reference | |
| P2 | P1 | REAL | Real terminal: decline, cancel on device, timeout | Each ends consistently | |
| P3 | P1 | REAL | Real terminal status response shape | Record the real JSON; confirm field names the app reads | |
| P4 | P1 | REAL | Real Plural link: pay by UPI, card, netbanking | SUCCESS via webhook | |
| P5 | P1 | REAL | Real Plural webhook payload | Capture one; confirm identifiers, `order_amount` shape and event names match the code | |
| P6 | P1 | REAL | Real signature | `X-Verify` over the raw body with the real secret | Accepted | |
| P7 | P2 | REAL | Gateway-reported amount on the terminal | Confirm whether it can be cross-checked (currently not) | |
| P8 | P2 | REAL | Refund / reversal | App does not refund; confirm the manual process and what the app shows afterwards | |
| P9 | P2 | REAL | Credentials rotated | Old secret and token stop working; new ones work; nothing in logs | |

---

## Counts

| Section | Cases | P1 |
|---|---|---|
| A Install/upgrade | 8 | 6 |
| B Settings | 25 | 12 |
| C Buttons | 17 | 6 |
| D Terminal flows | 20 | 13 |
| E Terminal guards | 30 | 17 |
| F State machine / form | 21 | 15 |
| G Payment links | 26 | 18 |
| H Webhook | 23 | 18 |
| I Cron | 10 | 6 |
| J Permissions | 16 | 13 |
| K Security | 17 | 12 |
| L Concurrency | 10 | 7 |
| M Other doctypes | 10 | 3 |
| N UI | 11 | 3 |
| O Operations | 6 | 0 |
| P Real gateway | 9 | 6 |
| **Total** | **259** | **155** |

## Suggested order of execution

1. A, B (setup and configuration), then C, D, E, F in the browser.
2. G, H, I (links, webhook, cron), then J, K by script as named users.
3. L, M, N, O last. P when a UAT terminal and Plural UAT account are available.

## Known limits of the simulator

- It cannot send arbitrary text in error messages, so XSS cases (F20, G23, K5) plant data in the database or fake the response.
- It refuses a late "success" for a cancelled transaction itself (D6 needs a direct backend call).
- It does not model real Plural payload shapes (P5) or real timing.
- The simulator's default webhook callback points at a dead address; webhook cases are sent by hand with a signed body.
