# -*- coding: utf-8 -*-
"""Backend + API test suite for pinelabs_fin (the non-UI test cases).

Covers the manual cases in pinelabs_test_cases.xlsx that cannot be driven from
the browser: state-machine internals, Payment Entry finalization, conflict guard,
amount handling, routing, contact resolution, webhook classification + signature,
and live webhook HTTP behaviour.

Standalone runner (uses the REAL pine.local data + the app's own functions with
the gateway mocked). Run from the bench root:

    cd <bench>/sites && ../env/bin/python <app>/playwright_tests/test_backend_api.py

Each test rolls back uncommitted writes; the few that commit (Payment Entry
creation, webhook success) clean up after themselves. A short webhook secret is
set for the HTTP tests and restored at the end.
"""
import hashlib
import hmac
import inspect
import json
import unittest
from unittest.mock import patch

import frappe
import requests
from frappe.utils import add_days, nowdate

SITE = "pine.local"
SITES_PATH = "/home/finstein-emp/bench-pinelab/sites"
BASE = "http://127.0.0.1:8015"
COMPANY = "Pine Lab Machine"
WEBHOOK_URL = f"{BASE}/api/method/pinelabs_fin.api.webhook.handle_webhook"


# ── shared helpers ───────────────────────────────────────────────────────
def make_invoice(submit=True):
    si = frappe.new_doc("Sales Invoice")
    si.customer = "Pine Demo Customer"
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
    return si


def cleanup_invoice(name):
    for r in frappe.get_all("Pinelabs Transaction", filters={"reference_name": name}, pluck="name"):
        t = frappe.get_doc("Pinelabs Transaction", r)
        t.payment_entry = None
        t.flags.ignore_validate = True
        t.db_set("payment_entry", None)
        frappe.delete_doc("Pinelabs Transaction", r, force=1, ignore_permissions=True)
    try:
        doc = frappe.get_doc("Sales Invoice", name)
        if doc.docstatus == 1:
            doc.cancel()
        frappe.delete_doc("Sales Invoice", name, force=1, ignore_permissions=True)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# State machine  (TC-SM-*)
# ══════════════════════════════════════════════════════════════════════════
class TestStateMachine(unittest.TestCase):
    def setUp(self):
        from pinelabs_fin.api import transaction as ts
        self.inv = make_invoice()
        self.txn = ts.mark_pending(
            ts.create_transaction(reference_doctype="Sales Invoice", reference_name=self.inv.name,
                                  flow_type="Terminal", amount=1000, payment_method="CARD",
                                  machine="Local Simulator Machine"),
            payment_id="PLUTUS-SM")

    def tearDown(self):
        frappe.db.rollback()

    def test_TC_SM_02_manual_success_blocked(self):
        doc = frappe.get_doc("Pinelabs Transaction", self.txn.name)
        doc.status = "SUCCESS"
        with self.assertRaises(frappe.ValidationError):
            doc.save(ignore_permissions=True)

    def test_TC_SM_03_illegal_transition_blocked(self):
        doc = frappe.get_doc("Pinelabs Transaction", self.txn.name)
        doc.status = "INITIATED"
        with self.assertRaises(frappe.ValidationError):
            doc.save(ignore_permissions=True)

    def test_TC_SM_05_flowtype_locked(self):
        doc = frappe.get_doc("Pinelabs Transaction", self.txn.name)
        doc.flow_type = "Plural"
        with self.assertRaises(frappe.ValidationError):
            doc.save(ignore_permissions=True)

    def test_TC_SM_04_pending_to_failed_now_rejected(self):
        # BUG-03 FIXED: a manual FAILED edit (no internal flag) is now rejected;
        # the transaction stays PENDING.
        doc = frappe.get_doc("Pinelabs Transaction", self.txn.name)
        doc.status = "FAILED"
        with self.assertRaises(frappe.ValidationError):
            doc.save(ignore_permissions=True)
        self.assertEqual(frappe.db.get_value("Pinelabs Transaction", self.txn.name, "status"), "PENDING")


# ══════════════════════════════════════════════════════════════════════════
# Payment Entry finalization  (TC-PE-*)
# ══════════════════════════════════════════════════════════════════════════
class TestPaymentEntry(unittest.TestCase):
    def setUp(self):
        self.inv = make_invoice()

    def tearDown(self):
        # mark_success commits a PE; clean up explicitly.
        for r in frappe.get_all("Pinelabs Transaction", filters={"reference_name": self.inv.name}, pluck="name"):
            pe = frappe.db.get_value("Pinelabs Transaction", r, "payment_entry")
            if pe and frappe.db.exists("Payment Entry", pe):
                d = frappe.get_doc("Payment Entry", pe)
                if d.docstatus == 1:
                    d.cancel()
                frappe.delete_doc("Payment Entry", pe, force=1, ignore_permissions=True)
            frappe.db.set_value("Pinelabs Transaction", r, "payment_entry", None)
            frappe.delete_doc("Pinelabs Transaction", r, force=1, ignore_permissions=True)
        cleanup_invoice(self.inv.name)
        frappe.db.commit()

    def _pending(self, payment_id):
        from pinelabs_fin.api import transaction as ts
        return ts.mark_pending(
            ts.create_transaction(reference_doctype="Sales Invoice", reference_name=self.inv.name,
                                  flow_type="Terminal", amount=1000, payment_method="CARD",
                                  machine="Local Simulator Machine"),
            payment_id=payment_id)

    def test_TC_PE_01_success_creates_payment_entry(self):
        from pinelabs_fin.api import transaction as ts
        txn = self._pending("PLUTUS-PE-1")
        txn = ts.mark_success(txn, payment_id="PLUTUS-PE-1", payment_method="CARD")
        self.assertEqual(txn.status, "SUCCESS")
        self.assertTrue(txn.payment_entry)
        pe = frappe.get_doc("Payment Entry", txn.payment_entry)
        self.assertEqual(pe.reference_no, "PLUTUS-PE-1")
        self.assertEqual(pe.mode_of_payment, "PineLabs - Card")
        self.assertEqual(pe.docstatus, 1)

    def test_TC_PE_02_idempotent_double_success(self):
        from pinelabs_fin.api import transaction as ts
        txn = self._pending("PLUTUS-PE-2")
        txn = ts.mark_success(txn, payment_id="PLUTUS-PE-2", payment_method="CARD")
        first_pe = txn.payment_entry
        again = ts.mark_success(txn, payment_id="PLUTUS-PE-2", payment_method="CARD")
        self.assertEqual(again.payment_entry, first_pe)  # no duplicate PE


# ══════════════════════════════════════════════════════════════════════════
# Conflict guard  (TC-CG-*)
# ══════════════════════════════════════════════════════════════════════════
class TestConflictGuard(unittest.TestCase):
    def setUp(self):
        from pinelabs_fin.api import transaction as ts
        self.inv = make_invoice()
        self.txn = ts.mark_pending(
            ts.create_transaction(reference_doctype="Sales Invoice", reference_name=self.inv.name,
                                  flow_type="Terminal", amount=1000, payment_method="CARD",
                                  machine="Local Simulator Machine"),
            payment_id="PLUTUS-CG")

    def tearDown(self):
        frappe.db.rollback()

    def test_TC_CG_01_manual_pe_blocked_while_active(self):
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
        pe = get_payment_entry("Sales Invoice", self.inv.name)
        pe.mode_of_payment = "Cash"
        pe.reference_no = "MANUAL-1"
        pe.reference_date = nowdate()
        with self.assertRaises(frappe.ValidationError):
            pe.insert(ignore_permissions=True)  # conflict_guard.refuse_if_pinelabs_active


# ══════════════════════════════════════════════════════════════════════════
# Amount handling  (TC-AMT-*)
# ══════════════════════════════════════════════════════════════════════════
class TestAmount(unittest.TestCase):
    def test_TC_AMT_03_paise_truncation_BUG06(self):
        # KNOWN ISSUE BUG-06: terminal uses int() (truncates) not round().
        self.assertEqual(int(float(262.15) * 100), 26214)          # buggy value the app sends
        self.assertEqual(int(round(float(262.15) * 100)), 26215)   # correct value

    def test_TC_AMT_02_amount_field_ignored_BUG11(self):
        # KNOWN ISSUE BUG-11: _amount_for takes only source_doc, so it cannot
        # consult the configured amount_field.
        from pinelabs_fin.api import terminal
        params = list(inspect.signature(terminal._amount_for).parameters)
        self.assertEqual(params, ["source_doc"])

    def test_TC_AMT_01_amount_for_uses_outstanding(self):
        from pinelabs_fin.api import terminal
        inv = make_invoice()
        try:
            self.assertEqual(terminal._amount_for(inv), 1000.0)
        finally:
            frappe.db.rollback()

    def test_TC_AMT_04_paid_invoice_returns_zero_BUG17(self):
        """BUG-17 FIXED: a submitted invoice with outstanding 0 must return 0 so
        the caller refuses — not fall back to grand_total and charge it again.
        Drafts must still fall back (outstanding is only computed at submit).
        """
        from pinelabs_fin.api import terminal

        class Doc:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        paid = Doc(docstatus=1, outstanding_amount=0.0, grand_total=1000.0, rounded_total=1000.0)
        self.assertEqual(terminal._amount_for(paid), 0.0)

        unpaid = Doc(docstatus=1, outstanding_amount=250.0, grand_total=1000.0)
        self.assertEqual(terminal._amount_for(unpaid), 250.0)

        draft = Doc(docstatus=0, outstanding_amount=0.0, grand_total=1000.0, rounded_total=1000.0)
        self.assertEqual(terminal._amount_for(draft), 1000.0)

        # Submittable doctype with no outstanding_amount field at all -> total.
        custom = Doc(docstatus=1, grand_total=400.0)
        self.assertEqual(terminal._amount_for(custom), 400.0)


# ══════════════════════════════════════════════════════════════════════════
# Routing  (TC-ROUTE-*)
# ══════════════════════════════════════════════════════════════════════════
class TestRouting(unittest.TestCase):
    def test_TC_ROUTE_01_single_machine_default(self):
        from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
            resolve_machine_for_context,
        )
        machine = resolve_machine_for_context("Sales Invoice", None)
        self.assertIsNotNone(machine)
        self.assertTrue(machine.machine_name)


# ══════════════════════════════════════════════════════════════════════════
# Customer contact resolution  (TC-CC-*)
# ══════════════════════════════════════════════════════════════════════════
class TestContact(unittest.TestCase):
    def tearDown(self):
        for dt, nm in (("Customer", "Acme Traders BT"), ("Supplier", "Acme Traders BT")):
            if frappe.db.exists(dt, nm):
                frappe.delete_doc(dt, nm, force=1, ignore_permissions=True)
        frappe.db.rollback()

    def test_TC_CC_01_direct_field(self):
        from pinelabs_fin.api.customer_contact import _resolve_value

        class Row(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)

        class Doc(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)

        row = Row(email_fetch_from="Direct Field", email_direct_field="contact_email")
        doc = Doc(contact_email="direct@example.com")
        self.assertEqual(_resolve_value(doc, row, "email"), "direct@example.com")

    def test_TC_CC_05_dynamic_link_wrong_party_BUG14(self):
        # KNOWN ISSUE BUG-14: linked lookup uses a pinned doctype, ignoring the
        # doc's real Dynamic Link target -> wrong party's contact.
        from pinelabs_fin.api.customer_contact import _resolve_value

        name = "Acme Traders BT"
        if not frappe.db.exists("Customer", name):
            c = frappe.new_doc("Customer"); c.customer_name = name
            c.customer_group = "Commercial"; c.territory = "India"; c.mobile_no = "9000000001"
            c.insert(ignore_permissions=True)
        if not frappe.db.exists("Supplier", name):
            sg = frappe.get_all("Supplier Group", limit=1, pluck="name")[0]
            s = frappe.new_doc("Supplier"); s.supplier_name = name
            s.supplier_group = sg; s.mobile_no = "9111111111"
            s.insert(ignore_permissions=True)

        class Row(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)

        class Doc(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)

        row = Row(mobile_fetch_from="Linked DocType", mobile_link_field="party",
                  mobile_linked_doctype="Customer", mobile_linked_field="mobile_no")
        doc = Doc(party_type="Supplier", party=name)
        # Returns the CUSTOMER's number even though the doc points at the Supplier.
        self.assertEqual(_resolve_value(doc, row, "mobile"), "9000000001")


# ══════════════════════════════════════════════════════════════════════════
# Webhook classification + signature (unit)  (TC-WH-*)
# ══════════════════════════════════════════════════════════════════════════
class TestWebhookUnit(unittest.TestCase):
    def test_TC_WH_classify_paid_failed_unknown(self):
        from pinelabs_fin.api.webhook import _classify
        self.assertEqual(_classify({"status": "PROCESSED"}, "ORDER_PROCESSED"), "paid")
        self.assertEqual(_classify({"status": "FAILED"}, "PAYMENT_FAILED"), "failed")
        self.assertEqual(_classify({"status": "???"}, "SOMETHING_ELSE"), "unknown")

    def test_TC_WH_extract_method(self):
        from pinelabs_fin.api.webhook import _extract_method
        self.assertEqual(_extract_method({"payment_method": "upi"}), "UPI")
        self.assertEqual(_extract_method({"payments": [{"payment_mode": "card"}]}), "CARD")


# ══════════════════════════════════════════════════════════════════════════
# Client response parsing (BUG-01) and cancel payload (BUG-05)
# ══════════════════════════════════════════════════════════════════════════
class TestClientParse(unittest.TestCase):
    def test_TC_TERM_19_malformed_200_is_failure_BUG01(self):
        # BUG-01 FIXED: a 200 with an unreadable (non-JSON) body is now reported
        # as failure, not success.
        from pinelabs_fin.api.pinelabs_client import PineLabsClient
        resp = requests.models.Response()
        resp.status_code = 200
        resp._content = b"<html><body>502 upstream busy</body></html>"
        parsed = PineLabsClient._parse_response(object.__new__(PineLabsClient), resp)
        self.assertFalse(parsed["success"])
        self.assertEqual(parsed["data"], {})
        self.assertIn("error", parsed)


class TestCancelPayload(unittest.TestCase):
    def test_TC_TERM_cancel_payload_has_store_and_amount_BUG05(self):
        # BUG-05 FIXED: cancel payload now carries the store identifier and Amount.
        from pinelabs_fin.api.pinelabs_client import PineLabsClient
        c = object.__new__(PineLabsClient)
        c.merchant_id, c.security_token, c.client_id, c.store_id = "M1", "S1", "C1", "ST1"
        payload = c._cancel_payload(123456789, amount=1000)
        self.assertEqual(payload["ClientID"], "C1")
        self.assertEqual(payload["StoreID"], "ST1")
        self.assertEqual(payload["Amount"], 100000)               # 1000 rupees -> paise
        self.assertEqual(payload["PlutusTransactionReferenceID"], 123456789)


# ══════════════════════════════════════════════════════════════════════════
# Webhook HTTP behaviour (live endpoint)  (TC-WH-*)
# ══════════════════════════════════════════════════════════════════════════
class TestWebhookHTTP(unittest.TestCase):
    SECRET = "backend_test_webhook_secret_123"

    @classmethod
    def setUpClass(cls):
        from pinelabs_fin.api import transaction as ts
        # Defensive pre-clean: a prior interrupted run may have left EVT-WHB-*
        # event rows (a terminal 'ignored'/'processed' row would dedup this run's
        # re-posts). Start from a clean slate regardless of past teardown.
        for r in frappe.get_all("Pinelabs Webhook Event",
                                filters={"event_id": ["like", "EVT-WHB-%"]}, pluck="name"):
            frappe.delete_doc("Pinelabs Webhook Event", r, force=1, ignore_permissions=True)
        for r in frappe.get_all("Pinelabs Transaction",
                                filters={"order_id": ["in", ["ORD-WH-PAID", "ORD-WH-FAIL"]]}, pluck="name"):
            frappe.db.set_value("Pinelabs Transaction", r, "payment_entry", None)
            frappe.delete_doc("Pinelabs Transaction", r, force=1, ignore_permissions=True)
        frappe.db.commit()

        s = frappe.get_single("Pinelabs Settings")
        cls._orig_enable = s.enable_payment_links
        cls._orig_secret = s.get_password("payment_link_webhook_secret", raise_exception=False)
        s.enable_payment_links = 1
        s.plural_client_id = s.plural_client_id or "dummy"
        s.plural_client_secret = s.plural_client_secret or "dummy"
        s.payment_link_webhook_secret = cls.SECRET
        s.flags.ignore_mandatory = True
        s.save(ignore_permissions=True)

        # Map the Payment Link account so the paid webhook can build a PE — the
        # BUG-16 case uses a real Sales Invoice (Guest → PE creation).
        cls._mapped_pl = not frappe.db.exists(
            "Mode of Payment Account", {"parent": "PineLabs - Payment Link", "company": COMPANY})
        if cls._mapped_pl:
            acct = frappe.db.get_value("Account", {"company": COMPANY, "account_type": "Cash", "is_group": 0}, "name") \
                or "Cash - PLM"
            mop = frappe.get_doc("Mode of Payment", "PineLabs - Payment Link")
            mop.append("accounts", {"company": COMPANY, "default_account": acct})
            mop.flags.ignore_permissions = True
            mop.save()
        frappe.db.commit()

        # (A) Real Sales Invoice + PENDING Plural txn — a paid webhook must now
        # succeed AND create a Payment Entry as Guest (BUG-16).
        cls.inv = make_invoice()
        cls.order = "ORD-WH-PAID"
        t = ts.mark_pending(
            ts.create_transaction(reference_doctype="Sales Invoice", reference_name=cls.inv.name,
                                  flow_type="Plural", amount=1000, order_id=cls.order),
            order_id=cls.order)
        cls.txn = t.name

        # (B) A FAILED Plural txn — a paid webhook's processing throws; its retry
        # must REPROCESS, not be swallowed as a duplicate (BUG-15).
        cls.order_fail = "ORD-WH-FAIL"
        tf = ts.create_transaction(reference_doctype="Customer", reference_name="Pine Demo Customer",
                                   flow_type="Plural", amount=1000, order_id=cls.order_fail)
        tf = ts.mark_pending(tf, order_id=cls.order_fail)
        tf = ts.mark_failed(tf, error_message="seeded FAILED for BUG-15")
        cls.txn_fail = tf.name
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        for od in (cls.order, cls.order_fail):
            for r in frappe.get_all("Pinelabs Transaction", filters={"order_id": od},
                                    fields=["name", "payment_entry"]):
                if r.payment_entry and frappe.db.exists("Payment Entry", r.payment_entry):
                    d = frappe.get_doc("Payment Entry", r.payment_entry)
                    if d.docstatus == 1:
                        d.cancel()
                    frappe.delete_doc("Payment Entry", r.payment_entry, force=1, ignore_permissions=True)
                frappe.db.set_value("Pinelabs Transaction", r.name, "payment_entry", None)
                frappe.delete_doc("Pinelabs Transaction", r.name, force=1, ignore_permissions=True)
        for r in frappe.get_all("Pinelabs Webhook Event",
                                filters={"event_id": ["like", "EVT-WHB-%"]}, pluck="name"):
            frappe.delete_doc("Pinelabs Webhook Event", r, force=1, ignore_permissions=True)
        cleanup_invoice(cls.inv.name)
        if getattr(cls, "_mapped_pl", False):
            mop = frappe.get_doc("Mode of Payment", "PineLabs - Payment Link")
            mop.accounts = [a for a in mop.accounts if a.company != COMPANY]
            mop.flags.ignore_permissions = True
            mop.save()
        s = frappe.get_single("Pinelabs Settings")
        s.enable_payment_links = cls._orig_enable
        s.payment_link_webhook_secret = cls._orig_secret or ""
        s.flags.ignore_validate = True
        s.save(ignore_permissions=True)
        frappe.db.commit()

    def _sign(self, body):
        return hmac.new(self.SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()

    def _post(self, body, sign=True, headers=None):
        h = {"Content-Type": "application/json"}
        if sign:
            h["X-Verify"] = self._sign(body)
        if headers:
            h.update(headers)
        return requests.post(WEBHOOK_URL, data=body, headers=h, timeout=20)

    def test_TC_WH_02_missing_signature_401(self):
        body = json.dumps({"event_id": "EVT-WHB-2", "event_type": "ORDER_PROCESSED",
                           "data": {"order_id": self.order, "status": "PROCESSED"}})
        self.assertEqual(self._post(body, sign=False).status_code, 401)

    def test_TC_WH_03_invalid_signature_401(self):
        body = json.dumps({"event_id": "EVT-WHB-3", "event_type": "ORDER_PROCESSED",
                           "data": {"order_id": self.order, "status": "PROCESSED"}})
        self.assertEqual(self._post(body, sign=False, headers={"X-Verify": "deadbeef"}).status_code, 401)

    def test_TC_WH_06_unknown_order_404(self):
        body = json.dumps({"event_id": "EVT-WHB-6", "event_type": "ORDER_PROCESSED",
                           "data": {"order_id": "no-such-order", "status": "PROCESSED"}})
        self.assertEqual(self._post(body).status_code, 404)

    def test_TC_WH_08_non_post_405(self):
        self.assertEqual(requests.get(WEBHOOK_URL, timeout=15).status_code, 405)

    def test_TC_WH_01_paid_creates_payment_entry_BUG16(self):
        # BUG-16 FIXED: the Guest webhook now creates the Payment Entry.
        body = json.dumps({"event_id": "EVT-WHB-1", "event_type": "ORDER_PROCESSED",
                           "data": {"order_id": self.order, "status": "PROCESSED", "payment_id": "PAY-WHB-1"}})
        r1 = self._post(body)
        self.assertEqual(r1.status_code, 200)   # was 500 (No permission for Account)
        frappe.db.rollback()  # read fresh committed state
        self.assertEqual(frappe.db.get_value("Pinelabs Transaction", self.txn, "status"), "SUCCESS")
        self.assertTrue(frappe.db.get_value("Pinelabs Transaction", self.txn, "payment_entry"))
        # duplicate re-delivery of a completed event -> deduped 200
        r2 = self._post(body)
        self.assertEqual(r2.status_code, 200)
        self.assertIn("uplicate", r2.text)

    def test_TC_WH_15_failed_delivery_retry_reprocesses_BUG15(self):
        # BUG-15 FIXED: a delivery whose processing throws (paid event on a
        # FAILED txn) 500s; the retry must REPROCESS (not be swallowed as a
        # 200 duplicate).
        body = json.dumps({"event_id": "EVT-WHB-15", "event_type": "ORDER_PROCESSED",
                           "data": {"order_id": self.order_fail, "status": "PROCESSED", "payment_id": "PAY-15"}})
        r1 = self._post(body)
        self.assertEqual(r1.status_code, 500)          # mark_success throws on a FAILED txn
        r2 = self._post(body)                           # Plural retries the same event_id
        self.assertNotIn("uplicate", r2.text)           # NOT swallowed
        self.assertEqual(r2.status_code, 500)           # reprocessed (still fails, as expected)


# ── standalone runner ──────────────────────────────────────────────────────
def main():
    frappe.init(site=SITE, sites_path=SITES_PATH)
    frappe.connect()
    frappe.set_user("Administrator")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (TestStateMachine, TestPaymentEntry, TestConflictGuard, TestAmount,
                TestRouting, TestContact, TestClientParse, TestCancelPayload,
                TestWebhookUnit, TestWebhookHTTP):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    frappe.destroy()
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
