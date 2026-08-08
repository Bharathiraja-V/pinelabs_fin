"""Set up fixtures + auth for the Playwright UI suite. Writes fixtures.json
and storageState.json into the same directory. Idempotent-ish: creates fresh
disposable invoices/transactions each run and records their names.

Run:  cd <bench>/sites && ../env/bin/python <this> [outdir]
"""
import json
import os
import sys

import frappe
from frappe.utils import add_days, nowdate

OUTDIR = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))

frappe.init(site="pine.local", sites_path=".")
frappe.connect()
frappe.set_user("Administrator")

from pinelabs_fin.api import transaction as txn_service  # noqa: E402

COMPANY = "Pine Lab Machine"


def mint_sid():
    frappe.local.form_dict = frappe._dict()
    frappe.local.request_ip = "127.0.0.1"
    frappe.local.cookie_manager = frappe._dict(set_cookie=lambda *a, **k: None, cookies={}, to_set=[])

    class _Req:
        cookies = {}
        headers = {}

        def get(self, *a, **k):
            return None

    frappe.local.request = _Req()
    from frappe.sessions import Session

    s = Session(user="Administrator", resume=False, full_name="Administrator", user_type="System User")
    frappe.db.commit()
    return s.sid


def make_invoice(submit=True, customer="Pine Demo Customer"):
    si = frappe.new_doc("Sales Invoice")
    si.customer = customer
    si.company = COMPANY
    si.currency = "INR"
    si.posting_date = nowdate()
    si.due_date = add_days(nowdate(), 7)
    si.append("items", {"item_code": "PL-DEMO-001", "qty": 2, "rate": 500,
                        "income_account": "Sales - PLM", "cost_center": "Main - PLM"})
    si.set_missing_values()
    si.insert(ignore_permissions=True)
    if submit:
        si.submit()
    frappe.db.commit()
    return si.name


def make_pending_txn(invoice):
    t = txn_service.create_transaction(
        reference_doctype="Sales Invoice", reference_name=invoice,
        flow_type="Terminal", amount=1000, payment_method="CARD",
        machine="Local Simulator Machine")
    t = txn_service.mark_pending(t, payment_id=None)
    frappe.db.commit()
    return t.name


# Fresh disposable fixtures.
inv_clean = make_invoice(submit=True)          # submitted, unpaid, no txn
inv_active = make_invoice(submit=True)         # submitted, unpaid, will carry active txns
txn_active = make_pending_txn(inv_active)      # the "in progress" one
txn_reject = make_pending_txn(inv_active)      # for SM-02 / SM-03 / SM-05 (stay PENDING)
txn_fail = make_pending_txn(inv_active)        # for SM-04 (will become FAILED)

# A cancelled invoice (submit then cancel) for the button-gating test.
_c = frappe.get_doc("Sales Invoice", make_invoice(submit=True))
_c.cancel()
frappe.db.commit()
inv_cancelled = _c.name

# A customer with NO email/mobile + a submitted invoice — for the payment-link
# "customer contact required" guard (fires before Plural is ever contacted).
if not frappe.db.exists("Customer", "No Contact Customer"):
    _nc = frappe.new_doc("Customer")
    _nc.customer_name = "No Contact Customer"
    _nc.customer_group = "Commercial"
    _nc.territory = "India"
    _nc.insert(ignore_permissions=True)
    frappe.db.commit()
inv_nocontact = make_invoice(submit=True, customer="No Contact Customer")

fixtures = {
    "base": "http://127.0.0.1:8015",
    "inv_clean": inv_clean,
    "inv_active": inv_active,
    "inv_paid": "ACC-SINV-2026-00001",   # pre-existing Paid invoice
    "inv_draft": "ACC-SINV-2026-00002",  # pre-existing Draft (show_on_docstatus=0,1)
    "inv_cancelled": inv_cancelled,      # cancelled invoice
    "inv_nocontact": inv_nocontact,      # submitted invoice, customer has no email/mobile
    "non_payable_doctype_record": "Pine Demo Customer",  # a Customer (not payable) for BTN-07
    "txn_active": txn_active,
    "txn_reject": txn_reject,
    "txn_fail": txn_fail,
    "txn_with_pe": "PLT-2026-00001",     # SUCCESS txn linked to a Payment Entry
}

sid = mint_sid()
storage = {
    "cookies": [
        {"name": "sid", "value": sid, "domain": "127.0.0.1", "path": "/",
         "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax"},
        {"name": "user_id", "value": "Administrator", "domain": "127.0.0.1", "path": "/",
         "expires": -1, "httpOnly": False, "secure": False, "sameSite": "Lax"},
    ],
    "origins": [],
}

with open(os.path.join(OUTDIR, "fixtures.json"), "w") as f:
    json.dump(fixtures, f, indent=2)
with open(os.path.join(OUTDIR, "storageState.json"), "w") as f:
    json.dump(storage, f, indent=2)

print("FIXTURES:", json.dumps(fixtures, indent=2))
print("sid len:", len(sid))
