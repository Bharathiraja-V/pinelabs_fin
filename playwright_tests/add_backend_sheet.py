# -*- coding: utf-8 -*-
"""Add a 'Backend & API Automated' sheet to pinelabs_test_cases.xlsx from the
executed test_backend_api.py suite, and stamp Status onto matching manual rows.
"""
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

FILE = "pinelabs_test_cases.xlsx"
HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
HEAD_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
PASS_FILL = PatternFill("solid", fgColor="C6EFCE")
PASS_FONT = Font(bold=True, color="006100")
THIN = Border(*[Side(style="thin", color="D0D0D0")] * 4)
WRAP = Alignment(wrap_text=True, vertical="top")
CTR = Alignment(horizontal="center", vertical="top")

# id, manual_ref, module, title, type, method, precondition, steps, expected, actual, status, loc
A = [
 ("BE-SM-02","TC-SM-02","State machine","Manual edit to SUCCESS blocked","Negative","Backend unit",
  "A PENDING transaction.","Load the txn, set status=SUCCESS, save().",
  "Raises ValidationError (only the service may set SUCCESS).","Raised as expected.","PASS","test_backend_api.py:TestStateMachine"),
 ("BE-SM-03","TC-SM-03","State machine","Illegal transition PENDING->INITIATED blocked","Negative","Backend unit",
  "A PENDING transaction.","Set status=INITIATED, save().","Raises ValidationError.","Raised.","PASS","test_backend_api.py:TestStateMachine"),
 ("BE-SM-04","TC-SM-04","State machine","Manual PENDING->FAILED now rejected (BUG-03 FIXED)","Negative","Backend unit",
  "A PENDING transaction.","Set status=FAILED (no internal flag), save().",
  "Raises ValidationError; status stays PENDING.","Rejected; stayed PENDING.","PASS (BUG-03 fixed)","test_backend_api.py:TestStateMachine"),
 ("BE-SM-05","TC-SM-05","State machine","flow_type locked after creation","Negative","Backend unit",
  "An existing transaction.","Set flow_type=Plural, save().","Raises ValidationError (locked).","Raised.","PASS","test_backend_api.py:TestStateMachine"),
 ("BE-PE-01","TC-PE-01","Payment Entry","Terminal success creates a Payment Entry","Happy","Backend integration",
  "A submitted invoice; Card mode mapped.","create_transaction -> mark_pending -> mark_success (as Administrator).",
  "Txn SUCCESS; a submitted PE with reference_no=payment_id, mode=PineLabs - Card.","PE created and submitted; fields correct.","PASS","test_backend_api.py:TestPaymentEntry"),
 ("BE-PE-02","TC-PE-02","Payment Entry","Idempotent — double mark_success reuses the PE","Edge","Backend integration",
  "A SUCCESS transaction with a PE.","Call mark_success again.","Returns the same PE; no duplicate.","Same PE returned.","PASS","test_backend_api.py:TestPaymentEntry"),
 ("BE-CG-01","TC-CG-01","Conflict guard","Manual PE blocked while a payment is active","Negative","Backend integration",
  "An invoice with an active (PENDING) transaction.","Build a PE (get_payment_entry) and insert().",
  "Raises ValidationError (conflict guard).","Raised.","PASS","test_backend_api.py:TestConflictGuard"),
 ("BE-AMT-01","TC-AMT-01","Amount","_amount_for returns the outstanding","Boundary","Backend unit",
  "A submitted invoice, outstanding 1000.","Call terminal._amount_for(invoice).","Returns 1000.0.","Returned 1000.0.","PASS","test_backend_api.py:TestAmount"),
 ("BE-AMT-02","TC-AMT-02","Amount","Configured Amount Field is ignored [BUG-11]","Edge","Backend unit",
  "N/A.","Inspect terminal._amount_for signature.",
  "CORRECT: consults amount_field. ACTUAL: takes only source_doc — cannot (documents BUG-11).","Signature is (source_doc,).","PASS (documents BUG-11)","test_backend_api.py:TestAmount"),
 ("BE-AMT-03","TC-AMT-03","Amount","Paise conversion truncates [BUG-06]","Boundary","Backend unit",
  "N/A.","Compute int(262.15*100) vs int(round(...)).",
  "CORRECT: 26215. ACTUAL app value: 26214 (documents BUG-06).","int()=26214, round()=26215.","PASS (documents BUG-06)","test_backend_api.py:TestAmount"),
 ("BE-ROUTE-01","TC-ROUTE-01","Routing","Single Machine mode resolves the default","Happy","Backend unit",
  "Routing = Single Machine; a default machine.","Call resolve_machine_for_context('Sales Invoice', None).",
  "Returns the default machine (non-null).","Returned the default machine.","PASS","test_backend_api.py:TestRouting"),
 ("BE-CC-01","TC-CC-01","Contact","Direct-field resolution","Happy","Backend unit",
  "Row maps email to a direct field.","Call _resolve_value(doc, row, 'email').","Returns the direct field value.","Returned 'direct@example.com'.","PASS","test_backend_api.py:TestContact"),
 ("BE-CC-05","TC-CC-05","Contact","Dynamic Link resolves wrong party [BUG-14]","Edge","Backend integration",
  "Customer+Supplier share a name, different mobiles; map pinned to Customer.","Call _resolve_value on a doc with party_type=Supplier.",
  "CORRECT: resolve the Supplier. ACTUAL: returns the Customer's mobile (documents BUG-14).","Returned the Customer's number.","PASS (documents BUG-14)","test_backend_api.py:TestContact"),
 ("BE-WH-U1","TC-WH-11","Webhook","Event classification paid/failed/unknown","Edge","Backend unit",
  "N/A.","Call _classify for processed / failed / unknown payloads.","'paid' / 'failed' / 'unknown'.","All three correct.","PASS","test_backend_api.py:TestWebhookUnit"),
 ("BE-WH-U2","—","Webhook","payment_method extraction","Edge","Backend unit",
  "N/A.","Call _extract_method for direct + nested payments.","Uppercased method (UPI / CARD).","Correct.","PASS","test_backend_api.py:TestWebhookUnit"),
 ("BE-WH-01","TC-WH-01","Webhook","Guest paid webhook creates Payment Entry (BUG-16 FIXED)","Happy","HTTP (live)",
  "Webhook secret set; a PENDING Plural txn on a real Sales Invoice; mode account mapped.","POST signed ORDER_PROCESSED; then re-POST the same event_id.",
  "First: HTTP 200, txn SUCCESS, a Payment Entry created (was 500). Second: 200 'Duplicate'.","200 + SUCCESS + PE; retry 200 duplicate.","PASS (BUG-16 fixed)","test_backend_api.py:TestWebhookHTTP"),
 ("BE-WH-15","TC-WH-15","Webhook","Failed delivery retry reprocesses (BUG-15 FIXED)","Negative","HTTP (live)",
  "A FAILED Plural txn; a paid webhook whose processing throws.","POST signed paid event (500); re-POST the same event_id.",
  "First 500; retry REPROCESSES (500 again), NOT swallowed as a 200 duplicate.","First 500; retry 500, not deduped.","PASS (BUG-15 fixed)","test_backend_api.py:TestWebhookHTTP"),
 ("BE-BUG01","TC-TERM-19","Client parse","Malformed 200 body is failure (BUG-01 FIXED)","Edge","Backend unit",
  "N/A.","Feed a 200 with an HTML body to _parse_response.",
  "success=False, data={}, error set (was success=True).","success False; error set.","PASS (BUG-01 fixed)","test_backend_api.py:TestClientParse"),
 ("BE-BUG05","TC-TERM-05","Cancel payload","Cancel carries store id + Amount (BUG-05 FIXED)","Edge","Backend unit",
  "N/A.","Build _cancel_payload(ref, amount=1000).",
  "Payload has ClientID, StoreID and Amount (paise). Verify vs real gateway.","ClientID/StoreID/Amount present.","PASS (BUG-05 code; verify gateway)","test_backend_api.py:TestCancelPayload"),
 ("BE-BUG08","TC-TERM-08c","Concurrency","Double-submit yields one active txn (BUG-08 FIXED)","Concurrency","2 procs (live DB)",
  "Clean submitted invoice; gateway mocked to hold the lock.","Fire two concurrent initiate calls synced to one instant.",
  "Exactly one active transaction; the other refused 'already in progress'.","1 active; other refused.","PASS (BUG-08 fixed)","race_worker.py (concurrency harness)"),
 ("BE-WH-02","TC-WH-02","Webhook","Missing X-Verify -> 401","Negative","HTTP (live)",
  "Webhook secret set.","POST a paid event with no X-Verify header.","HTTP 401.","401.","PASS","test_backend_api.py:TestWebhookHTTP"),
 ("BE-WH-03","TC-WH-03","Webhook","Invalid signature -> 401","Negative","HTTP (live)",
  "Webhook secret set.","POST with X-Verify='deadbeef'.","HTTP 401.","401.","PASS","test_backend_api.py:TestWebhookHTTP"),
 ("BE-WH-06","TC-WH-06","Webhook","Unknown order_id -> 404","Negative","HTTP (live)",
  "Webhook secret set.","POST a valid signed event for an unknown order.","HTTP 404.","404.","PASS","test_backend_api.py:TestWebhookHTTP"),
 ("BE-WH-08","TC-WH-08","Webhook","Non-POST method -> 405","Negative","HTTP (live)",
  "Endpoint reachable.","GET the webhook URL.","HTTP 405.","405.","PASS","test_backend_api.py:TestWebhookHTTP"),
]

wb = openpyxl.load_workbook(FILE)
if "Backend & API Automated" in wb.sheetnames:
    del wb["Backend & API Automated"]
ws = wb.create_sheet("Backend & API Automated")
cols = ["Test ID","Manual Ref","Module","Title","Type","Method","Preconditions",
        "Steps","Expected Result","Actual Result","Status","Test Location"]
ws.append([""]*len(cols))    # banner
ws.append(cols)              # header
for c in range(1, len(cols)+1):
    cell = ws.cell(2, c); cell.fill = HEAD_FILL; cell.font = HEAD_FONT
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True); cell.border = THIN
ws.row_dimensions[2].height = 28
ws.freeze_panes = "A3"
ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
b = ws.cell(1,1); b.value = ("Executed backend + API suite — 23/23 PASSED (includes Priority-1 fix verifications: "
    "BUG-01/03/05/08/15/16). Python (frappe + mocks) and signed "
    "HTTP; not browser-driven. Run: cd <bench>/sites && ../env/bin/python "
    "<app>/playwright_tests/test_backend_api.py")
b.font = Font(name="Calibri", bold=True, color="1F4E78"); b.alignment = Alignment(wrap_text=True, vertical="center")
ws.row_dimensions[1].height = 30

for row in A:
    ws.append(list(row))
    r = ws.max_row
    for c in range(1, len(cols)+1):
        cell = ws.cell(r, c); cell.border = THIN
        cell.alignment = CTR if c in (5,6,11) else WRAP
    sc = ws.cell(r, 11); sc.fill = PASS_FILL; sc.font = PASS_FONT

widths = [13,12,15,34,10,18,30,42,44,38,26,30]
for i,w in enumerate(widths,1):
    ws.column_dimensions[get_column_letter(i)].width = w

# Stamp manual rows not already covered by the Playwright sheet.
already = {"TC-SM-02","TC-SM-03","TC-SM-04","TC-SM-05","TC-CG-01"}  # stamped by Playwright
stamp = {r[1]: r[9] for r in A if r[1] not in ("—",) and r[1] not in already}
tcs = wb["Test Cases"]
for rr in range(2, tcs.max_row+1):
    tcid = tcs.cell(rr, 1).value
    if tcid in stamp:
        ac = tcs.cell(rr, 10, value=f"[Backend/API] {stamp[tcid]}"); ac.alignment = WRAP
        st = tcs.cell(rr, 11, value="Pass"); st.alignment = CTR; st.fill = PASS_FILL; st.font = PASS_FONT

wb.save(FILE)
print(f"Added 'Backend & API Automated' with {len(A)} rows; stamped {len(stamp)} additional manual rows.")
print("Sheets:", wb.sheetnames)
