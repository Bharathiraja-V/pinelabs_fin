# -*- coding: utf-8 -*-
"""End-to-end test of pinelabs_fin against the live Pine Labs simulator (:5000).

Unlike test_phase1.py (gateway mocked) this calls the app's real whitelisted
functions - initiate / check / cancel / retry / refresh - on pine.local, and the
app talks to the simulator over HTTP. The simulator is only the fake gateway; every
assertion is about the APP: Pinelabs Transaction state, Payment Entry, invoice
outstanding amount and the pre-flight guards.

Terminal flow: simulator is put in manual mode and each scenario is resolved through
POST /api/transactions/<ptrn>/resolve. Pay-by-Link flow: the Plural base URL is pointed
at the simulator in-process only (Pinelabs Settings is NOT modified) and the link is
resolved with .../sim-resolve; the app finalises via refresh_payment_link_status.

Run from the sites dir (creates and then removes its own invoices / PEs / transactions):

    cd <bench>/sites && ../env/bin/python <app>/playwright_tests/test_app_e2e_simulator.py
    ... test_app_e2e_simulator.py TestTerminalFlow.test_card_success
"""
import json
import sys
import time
import unittest
import urllib.request
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, nowdate

SITE = "pine.local"
SITES_PATH = "/home/finstein-emp/bench-16/bench-pine/sites"
SIM = "http://127.0.0.1:5000"
COMPANY = "Pinelabs FIn"  # v16 site's company
CUSTOMER = "Demo customer 01"
ITEM = "WB001"
INCOME_ACCOUNT = "Sales - PF"
COST_CENTER = "Main - PF"
CARD, UPI, LINK = "PineLabs - Card", "PineLabs - UPI", "PineLabs - Payment Link"

_created_invoices = []
_saved_sim = {}


# ── simulator helpers (control plane only) ───────────────────────────────
def sim(method, path, body=None):
	req = urllib.request.Request(SIM + path, data=None if body is None else json.dumps(body).encode(), method=method)
	req.add_header("Content-Type", "application/json")
	try:
		with urllib.request.urlopen(req, timeout=10) as r:
			return json.loads(r.read())
	except urllib.error.HTTPError as e:
		return {"_http": e.code, **(json.loads(e.read() or b"{}"))}


def sim_txn_count():
	return sim("GET", "/api/status")["cloud_transaction_count"]


# ── data helpers ─────────────────────────────────────────────────────────
def make_invoice(submit=True, qty=2, rate=500, customer=CUSTOMER):
	si = frappe.new_doc("Sales Invoice")
	si.customer = customer
	si.company = COMPANY
	si.currency = "INR"
	si.posting_date = nowdate()
	si.due_date = add_days(nowdate(), 7)
	si.append("items", {"item_code": ITEM, "qty": qty, "rate": rate,
						"income_account": INCOME_ACCOUNT, "cost_center": COST_CENTER})
	si.set_missing_values()
	si.insert(ignore_permissions=True)
	if submit:
		si.submit()
	frappe.db.commit()
	_created_invoices.append(si.name)
	return si.name


def invoice(name):
	return frappe.get_doc("Sales Invoice", name)


def txn(name):
	frappe.db.rollback()  # drop any stale snapshot so we see rows committed by the app
	return frappe.get_doc("Pinelabs Transaction", name)


def pes_for(inv):
	return frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": inv, "docstatus": 1},
		pluck="parent",
	)


def poll_until_final(txn_name, fn, timeout=15):
	from pinelabs_fin.api import terminal

	deadline = time.time() + timeout
	while time.time() < deadline:
		res = getattr(terminal, fn)(txn_name) if fn != "link" else None
		frappe.db.commit()
		if txn(txn_name).status in ("SUCCESS", "FAILED", "CANCELLED"):
			return txn(txn_name).status
		time.sleep(0.7)
	return txn(txn_name).status


def resolve_in_sim(txn_name, outcome):
	t = txn(txn_name)
	assert t.payment_id, f"{txn_name} has no PTRN (payment_id): status={t.status} err={t.error_message}"
	r = sim("POST", f"/api/transactions/{t.payment_id}/resolve", {"outcome": outcome})
	assert r.get("success"), r
	return t.payment_id


def cleanup():
	frappe.db.rollback()
	for inv in _created_invoices:
		try:
			for pe in frappe.get_all("Payment Entry Reference", filters={"reference_name": inv}, pluck="parent"):
				doc = frappe.get_doc("Payment Entry", pe)
				if doc.docstatus == 1:
					doc.flags.pinelabs_internal_pe = True
					doc.cancel()
			for pe in frappe.get_all("Payment Entry Reference", filters={"reference_name": inv}, pluck="parent"):
				frappe.delete_doc("Payment Entry", pe, force=1, ignore_permissions=True)
			for t in frappe.get_all("Pinelabs Transaction", filters={"reference_name": inv}, pluck="name"):
				frappe.db.set_value("Pinelabs Transaction", t, "payment_entry", None)
				frappe.delete_doc("Pinelabs Transaction", t, force=1, ignore_permissions=True)
			doc = frappe.get_doc("Sales Invoice", inv)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Sales Invoice", inv, force=1, ignore_permissions=True)
			frappe.db.commit()
		except Exception as e:  # leave it and report - never mask a real failure
			frappe.db.rollback()
			print(f"  [cleanup] could not remove {inv}: {e}", file=sys.stderr)


_temp_link_account = []


def ensure_link_mode_account():
	"""pine.local has no default account for 'PineLabs - Payment Link' on the test company, so the app's
	pre-flight (correctly) refuses to start a link. Add one for the duration of the run, then remove it."""
	mop = frappe.get_doc("Mode of Payment", LINK)
	if any(a.company == COMPANY for a in mop.accounts):
		return
	card = frappe.get_doc("Mode of Payment", CARD)
	acct = next(a.default_account for a in card.accounts if a.company == COMPANY)
	row = mop.append("accounts", {"company": COMPANY, "default_account": acct})
	mop.save(ignore_permissions=True)
	frappe.db.commit()
	_temp_link_account.append(mop.accounts[-1].name)


_links_were_enabled = []


def ensure_payment_links_enabled():
	"""v16 refuses Send Payment Link unless 'Enable Payment Links' is ticked. Tick it for the run, restore after."""
	if _links_were_enabled:
		return
	_links_were_enabled.append(frappe.db.get_single_value("Pinelabs Settings", "enable_payment_links"))
	frappe.db.set_single_value("Pinelabs Settings", "enable_payment_links", 1)
	frappe.db.commit()


def restore_payment_links_flag():
	if _links_were_enabled:
		frappe.db.rollback()
		frappe.db.set_single_value("Pinelabs Settings", "enable_payment_links", _links_were_enabled[0])
		frappe.db.commit()


def remove_link_mode_account():
	if _temp_link_account:
		frappe.db.rollback()
		mop = frappe.get_doc("Mode of Payment", LINK)
		mop.accounts = [a for a in mop.accounts if a.name not in _temp_link_account]
		mop.save(ignore_permissions=True)
		frappe.db.commit()


class AppCase(unittest.TestCase):
	def setUp(self):
		sim("POST", "/api/reset")
		sim("POST", "/api/config", {"outcome_mode": "manual", "manual_outcome": "success", "webhook_enabled": False})

	def initiate(self, inv, mode=CARD, **kw):
		from pinelabs_fin.api import terminal

		res = terminal.initiate_terminal_payment("Sales Invoice", inv, mode, **kw)
		frappe.db.commit()
		return res


# ═════════════════════════════ Terminal ("Pay on Machine") ═════════════════════════════
class TestTerminalFlow(AppCase):
	def test_card_success(self):
		inv = make_invoice()
		self.assertEqual(flt(invoice(inv).outstanding_amount), 1000)
		res = self.initiate(inv, CARD)
		self.assertTrue(res["success"], res)
		t = txn(res["transaction_name"])
		self.assertEqual(t.status, "PENDING")
		self.assertTrue(t.payment_id, "PTRN from the gateway was not stored")
		self.assertEqual(flt(t.amount), 1000)
		resolve_in_sim(t.name, "success")
		self.assertEqual(poll_until_final(t.name, "poll_terminal_status"), "SUCCESS")
		t = txn(t.name)
		self.assertTrue(t.payment_entry, "SUCCESS without a Payment Entry")
		pe = frappe.get_doc("Payment Entry", t.payment_entry)
		self.assertEqual((pe.docstatus, flt(pe.paid_amount), pe.mode_of_payment), (1, 1000, CARD))
		self.assertEqual(flt(invoice(inv).outstanding_amount), 0)
		self.assertEqual(invoice(inv).status, "Paid")

	def test_upi_success_uses_upi_mode_of_payment(self):
		inv = make_invoice()
		res = self.initiate(inv, UPI)
		self.assertTrue(res["success"], res)
		resolve_in_sim(res["transaction_name"], "success")
		self.assertEqual(poll_until_final(res["transaction_name"], "poll_terminal_status"), "SUCCESS")
		pe = frappe.get_doc("Payment Entry", txn(res["transaction_name"]).payment_entry)
		self.assertEqual(pe.mode_of_payment, UPI)

	def test_declined_marks_failed_and_leaves_invoice_unpaid(self):
		inv = make_invoice()
		res = self.initiate(inv)
		resolve_in_sim(res["transaction_name"], "declined")
		self.assertEqual(poll_until_final(res["transaction_name"], "poll_terminal_status"), "FAILED")
		self.assertFalse(txn(res["transaction_name"]).payment_entry)
		self.assertEqual(pes_for(inv), [])
		self.assertEqual(flt(invoice(inv).outstanding_amount), 1000)

	def test_timeout_never_records_a_payment(self):
		inv = make_invoice()
		res = self.initiate(inv)
		resolve_in_sim(res["transaction_name"], "timeout")
		final = poll_until_final(res["transaction_name"], "poll_terminal_status", timeout=8)
		self.assertNotEqual(final, "SUCCESS")
		self.assertEqual(pes_for(inv), [])
		self.assertEqual(flt(invoice(inv).outstanding_amount), 1000)

	def test_user_cancel_while_pending(self):
		from pinelabs_fin.api import terminal

		inv = make_invoice()
		res = self.initiate(inv)
		out = terminal.cancel_terminal_payment(res["transaction_name"], "customer walked away")
		frappe.db.commit()
		self.assertTrue(out.get("success"), out)
		self.assertEqual(txn(res["transaction_name"]).status, "CANCELLED")
		self.assertEqual(pes_for(inv), [])
		# the gateway side must have been told, and a late "success" must not resurrect it
		ptrn = txn(res["transaction_name"]).payment_id
		sim("POST", f"/api/transactions/{ptrn}/resolve", {"outcome": "success"})
		terminal.poll_terminal_status(res["transaction_name"])
		frappe.db.commit()
		self.assertEqual(txn(res["transaction_name"]).status, "CANCELLED")
		self.assertEqual(pes_for(inv), [])

	def test_retry_after_decline_creates_a_new_working_transaction(self):
		from pinelabs_fin.api import terminal

		inv = make_invoice()
		first = self.initiate(inv)
		resolve_in_sim(first["transaction_name"], "declined")
		self.assertEqual(poll_until_final(first["transaction_name"], "poll_terminal_status"), "FAILED")
		retry = terminal.retry_failed_terminal_payment(first["transaction_name"])
		frappe.db.commit()
		self.assertTrue(retry["success"], retry)
		self.assertNotEqual(retry["transaction_name"], first["transaction_name"])
		resolve_in_sim(retry["transaction_name"], "success")
		self.assertEqual(poll_until_final(retry["transaction_name"], "poll_terminal_status"), "SUCCESS")
		self.assertEqual(len(pes_for(inv)), 1)
		self.assertEqual(txn(first["transaction_name"]).status, "FAILED")  # history preserved

	def test_polling_after_success_never_creates_a_second_payment_entry(self):
		from pinelabs_fin.api import terminal

		inv = make_invoice()
		res = self.initiate(inv)
		resolve_in_sim(res["transaction_name"], "success")
		poll_until_final(res["transaction_name"], "poll_terminal_status")
		for _ in range(5):
			terminal.poll_terminal_status(res["transaction_name"])
			terminal.check_terminal_status(res["transaction_name"])
		frappe.db.commit()
		self.assertEqual(len(pes_for(inv)), 1)

	def test_concurrent_pollers_create_exactly_one_payment_entry(self):
		"""Foreground poll + cron + webhook race to finalise the same transaction."""
		import threading

		inv = make_invoice()
		res = self.initiate(inv)
		resolve_in_sim(res["transaction_name"], "success")
		time.sleep(1.2)  # let the simulator's 0.8s finalise window pass
		errors = []

		def worker():
			try:
				frappe.init(site=SITE, sites_path=SITES_PATH)
				frappe.connect()
				frappe.set_user("Administrator")
				from pinelabs_fin.api import terminal

				terminal.poll_terminal_status(res["transaction_name"])
				frappe.db.commit()
			except Exception as e:  # noqa: BLE001
				errors.append(repr(e))
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=worker) for _ in range(4)]
		[t.start() for t in threads]
		[t.join() for t in threads]
		frappe.init(site=SITE, sites_path=SITES_PATH)
		frappe.connect()
		frappe.set_user("Administrator")
		self.assertEqual(len(pes_for(inv)), 1, f"errors={errors}")
		self.assertEqual(txn(res["transaction_name"]).status, "SUCCESS")

	def test_create_payment_entry_zero_records_payment_without_posting(self):
		inv = make_invoice()
		res = self.initiate(inv, create_payment_entry=0)
		resolve_in_sim(res["transaction_name"], "success")
		self.assertEqual(poll_until_final(res["transaction_name"], "poll_terminal_status"), "SUCCESS")
		self.assertEqual(pes_for(inv), [])
		self.assertEqual(flt(invoice(inv).outstanding_amount), 1000)

	def test_partial_amount_leaves_the_balance_outstanding(self):
		inv = make_invoice()
		res = self.initiate(inv, amount=400)
		self.assertEqual(flt(txn(res["transaction_name"]).amount), 400)
		resolve_in_sim(res["transaction_name"], "success")
		poll_until_final(res["transaction_name"], "poll_terminal_status")
		self.assertEqual(flt(invoice(inv).outstanding_amount), 600)
		self.assertEqual(invoice(inv).status, "Partly Paid")

	def test_two_sequential_part_payments_settle_the_invoice(self):
		inv = make_invoice()
		for amt in (300, 700):
			res = self.initiate(inv, amount=amt)
			resolve_in_sim(res["transaction_name"], "success")
			self.assertEqual(poll_until_final(res["transaction_name"], "poll_terminal_status"), "SUCCESS")
		self.assertEqual(flt(invoice(inv).outstanding_amount), 0)
		self.assertEqual(len(pes_for(inv)), 2)


# ═════════════════════════════ Guards that must fire BEFORE the gateway ═════════════════════════════
class TestPreflightGuards(AppCase):
	def _assert_refused_without_gateway_call(self, fn):
		before = sim_txn_count()
		# v16: a missing and a forbidden document give the same PermissionError on purpose.
		with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
			fn()
		frappe.db.rollback()
		self.assertEqual(sim_txn_count(), before, "the gateway was contacted although the request should be refused")

	def test_second_initiate_on_same_invoice_is_refused(self):
		inv = make_invoice()
		self.initiate(inv)
		self._assert_refused_without_gateway_call(lambda: self.initiate(inv))
		self.assertEqual(sim_txn_count(), 1)

	def test_draft_invoice_is_accepted_and_auto_submitted_once_paid(self):
		"""By design (transaction._create_payment_entry_idempotent): the customer has paid, so a draft is submitted."""
		inv = make_invoice(submit=False)
		self.assertEqual(invoice(inv).docstatus, 0)
		res = self.initiate(inv)
		self.assertTrue(res["success"], res)
		resolve_in_sim(res["transaction_name"], "success")
		self.assertEqual(poll_until_final(res["transaction_name"], "poll_terminal_status"), "SUCCESS")
		self.assertEqual((invoice(inv).docstatus, flt(invoice(inv).outstanding_amount)), (1, 0))

	def test_already_paid_invoice_refused(self):
		inv = make_invoice()
		res = self.initiate(inv)
		resolve_in_sim(res["transaction_name"], "success")
		poll_until_final(res["transaction_name"], "poll_terminal_status")
		self._assert_refused_without_gateway_call(lambda: self.initiate(inv))

	def test_cancelled_invoice_refused(self):
		inv = make_invoice()
		doc = invoice(inv)
		doc.cancel()
		frappe.db.commit()
		self._assert_refused_without_gateway_call(lambda: self.initiate(inv))

	def test_non_pinelabs_and_unknown_modes_refused(self):
		inv = make_invoice()
		for mode in ("Cash", "Credit Card", "No Such Mode", ""):
			with self.subTest(mode=mode):
				self._assert_refused_without_gateway_call(lambda: self.initiate(inv, mode))

	def test_payment_link_mode_refused_on_the_terminal_flow(self):
		inv = make_invoice()
		self._assert_refused_without_gateway_call(lambda: self.initiate(inv, LINK))

	def test_unknown_document_refused(self):
		from pinelabs_fin.api import terminal

		self._assert_refused_without_gateway_call(
			lambda: terminal.initiate_terminal_payment("Sales Invoice", "ACC-SINV-DOES-NOT-EXIST", CARD)
		)

	def test_non_payable_doctype_refused(self):
		from pinelabs_fin.api import terminal

		self._assert_refused_without_gateway_call(
			lambda: terminal.initiate_terminal_payment("Customer", CUSTOMER, CARD)
		)

	def test_amount_edge_values(self):
		inv = make_invoice()
		for amt in (-5, 0.001):
			with self.subTest(amount=amt):
				try:
					res = self.initiate(inv, amount=amt)
				except frappe.ValidationError:
					frappe.db.rollback()
					continue
				# if accepted it must not charge a nonsensical amount
				self.assertGreater(flt(txn(res["transaction_name"]).amount), 0)
				sim("POST", "/api/reset")
				from pinelabs_fin.api import terminal

				terminal.cancel_terminal_payment(res["transaction_name"])
				frappe.db.commit()

	def test_amount_above_outstanding(self):
		inv = make_invoice()
		try:
			res = self.initiate(inv, amount=5000)
		except frappe.ValidationError:
			frappe.db.rollback()
			return
		self.fail(f"terminal charge of 5000 accepted against a 1000 invoice: {res}")

	def test_manual_payment_entry_blocked_while_terminal_payment_pending(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		inv = make_invoice()
		self.initiate(inv)
		pe = get_payment_entry("Sales Invoice", inv)
		pe.reference_no, pe.reference_date = "MANUAL-1", nowdate()
		with self.assertRaises(frappe.ValidationError):
			pe.insert(ignore_permissions=True)
			pe.submit()
		frappe.db.rollback()

	def test_manual_payment_entry_allowed_again_after_decline(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		inv = make_invoice()
		res = self.initiate(inv)
		resolve_in_sim(res["transaction_name"], "declined")
		poll_until_final(res["transaction_name"], "poll_terminal_status")
		pe = get_payment_entry("Sales Invoice", inv)
		pe.reference_no, pe.reference_date = "MANUAL-2", nowdate()
		pe.insert(ignore_permissions=True)
		pe.submit()
		frappe.db.commit()
		self.assertEqual(flt(invoice(inv).outstanding_amount), 0)


class TestGatewayMisbehaviour(AppCase):
	def test_gateway_down_does_not_leave_an_active_transaction(self):
		inv = make_invoice()
		with patch("pinelabs_fin.api.pinelabs_client.PineLabsClient.upload_billed_raw", side_effect=ConnectionError("down")):
			try:
				self.initiate(inv)
			except Exception:  # noqa: BLE001
				frappe.db.rollback()
		active = frappe.get_all("Pinelabs Transaction", filters={"reference_name": inv, "status": ["in", ["INITIATED", "PENDING"]]})
		self.assertEqual(active, [], "a failed upload left the invoice locked by an active transaction")
		self.assertEqual(pes_for(inv), [])

	def test_unknown_ptrn_from_gateway_never_succeeds(self):
		from pinelabs_fin.api import terminal

		inv = make_invoice()
		res = self.initiate(inv)
		sim("POST", "/api/reset")  # simulator forgets the PTRN -> "invalid PTRN" on the next poll
		for _ in range(3):
			terminal.poll_terminal_status(res["transaction_name"])
		frappe.db.commit()
		self.assertNotEqual(txn(res["transaction_name"]).status, "SUCCESS")
		self.assertEqual(pes_for(inv), [])


# ═════════════════════════════ Pay-by-Link (Plural) ═════════════════════════════
def _sim_plural():
	"""Point the Plural client at the simulator, with demo credentials, for this process only."""
	from contextlib import ExitStack, contextmanager

	@contextmanager
	def _ctx():
		with ExitStack() as st:
			st.enter_context(patch("pinelabs_fin.api.plural_client.PluralClient._get_base_url", return_value=SIM))
			# The site has no saved Plural credentials; give the client the simulator's demo pair, in memory only.
			st.enter_context(
				patch(
					"pinelabs_fin.api.plural_client.PluralClient._get_credentials",
					return_value=("SIM_CLIENT_ID_DEMO", "SIM_CLIENT_SECRET_DEMO"),
				)
			)
			yield

	return _ctx()


class TestPaymentLinkPreflight(AppCase):
	@classmethod
	def setUpClass(cls):
		ensure_payment_links_enabled()

	def test_link_refused_before_plural_is_called_when_mode_has_no_account(self):
		"""Fail-fast guard: no default account for the company -> refuse BEFORE a customer could be sent a link."""
		from pinelabs_fin.api import plural

		if any(a.company == COMPANY for a in frappe.get_doc("Mode of Payment", LINK).accounts):
			self.skipTest("Payment Link mode already has an account on this site")
		inv = make_invoice()
		with _sim_plural(), self.assertRaises(frappe.ValidationError) as cm:
			plural.initiate_payment_link("Sales Invoice", inv, customer_mobile="9876543210", customer_email="e2e@example.com")
		frappe.db.rollback()
		self.assertIn("no default account", str(cm.exception))
		self.assertEqual(frappe.get_all("Pinelabs Transaction", filters={"reference_name": inv}), [])


class TestPaymentLinkFlow(AppCase):
	@classmethod
	def setUpClass(cls):
		ensure_payment_links_enabled()
		ensure_link_mode_account()

	def _initiate(self, inv, **kw):
		from pinelabs_fin.api import plural

		with _sim_plural():
			res = plural.initiate_payment_link("Sales Invoice", inv, **kw)
		frappe.db.commit()
		return res

	def _refresh(self, name):
		from pinelabs_fin.api import plural

		with _sim_plural():
			out = plural.refresh_payment_link_status(name)
		frappe.db.commit()
		return out

	def _sim_link_id(self, name):
		return txn(name).order_id

	def _customer_has_contact(self):
		from pinelabs_fin.api import plural

		return plural

	def test_link_created_and_paid_creates_payment_entry(self):
		inv = make_invoice()
		res = self._initiate(inv, customer_mobile="9876543210", customer_email="e2e@example.com")
		self.assertTrue(res.get("success"), res)
		self.assertIn("/sim/pay-link/", res["payment_link_url"])
		name = res["transaction_name"]
		self.assertEqual(txn(name).status, "PENDING")
		link_id = res["payment_link_id"]
		sim("POST", f"/api/pay/v1/payment-links/{link_id}/sim-initiate", {"payment_method": "UPI"})
		sim("POST", f"/api/pay/v1/payment-links/{link_id}/sim-resolve", {"outcome": "SUCCESS", "payment_method": "UPI"})
		self._refresh(name)
		self.assertEqual(txn(name).status, "SUCCESS")
		pe = frappe.get_doc("Payment Entry", txn(name).payment_entry)
		self.assertEqual((pe.docstatus, flt(pe.paid_amount), pe.mode_of_payment), (1, 1000, LINK))
		self.assertEqual(flt(invoice(inv).outstanding_amount), 0)

	def test_repeated_refresh_after_success_is_idempotent(self):
		inv = make_invoice()
		res = self._initiate(inv, customer_mobile="9876543210", customer_email="e2e@example.com")
		link_id = res["payment_link_id"]
		sim("POST", f"/api/pay/v1/payment-links/{link_id}/sim-resolve", {"outcome": "SUCCESS", "payment_method": "CARD"})
		for _ in range(4):
			self._refresh(res["transaction_name"])
		self.assertEqual(len(pes_for(inv)), 1)

	def test_failed_payment_does_not_close_the_link_or_pay_the_invoice(self):
		inv = make_invoice()
		res = self._initiate(inv, customer_mobile="9876543210", customer_email="e2e@example.com")
		link_id = res["payment_link_id"]
		sim("POST", f"/api/pay/v1/payment-links/{link_id}/sim-resolve", {"outcome": "FAILED", "payment_method": "CARD"})
		self._refresh(res["transaction_name"])
		self.assertEqual(pes_for(inv), [])
		self.assertNotEqual(txn(res["transaction_name"]).status, "SUCCESS")
		self.assertEqual(flt(invoice(inv).outstanding_amount), 1000)

	def test_missing_contact_refused_before_plural_is_called(self):
		inv = make_invoice(customer="No Contact Customer") if frappe.db.exists("Customer", "No Contact Customer") else None
		if not inv:
			self.skipTest("fixture customer 'No Contact Customer' not present")
		before = len(sim("GET", "/api/transactions") if isinstance(sim("GET", "/api/transactions"), list) else [])
		with self.assertRaises(frappe.ValidationError):
			self._initiate(inv)
		frappe.db.rollback()

	def test_second_link_on_same_invoice_refused_while_first_is_active(self):
		inv = make_invoice()
		self._initiate(inv, customer_mobile="9876543210", customer_email="e2e@example.com")
		with self.assertRaises(frappe.ValidationError):
			self._initiate(inv, customer_mobile="9876543210", customer_email="e2e@example.com")
		frappe.db.rollback()

	def test_terminal_payment_refused_while_link_is_active(self):
		inv = make_invoice()
		self._initiate(inv, customer_mobile="9876543210", customer_email="e2e@example.com")
		with self.assertRaises(frappe.ValidationError):
			self.initiate(inv, CARD)
		frappe.db.rollback()

	def test_invalid_mobile_values_do_not_crash_the_app(self):
		for mobile in ("123", "abcdefghij", "+91 98765 43210", ""):
			with self.subTest(mobile=mobile):
				inv = make_invoice()
				try:
					self._initiate(inv, customer_mobile=mobile, customer_email="e2e@example.com")
				except frappe.ValidationError:
					frappe.db.rollback()
				except Exception as e:  # noqa: BLE001
					self.fail(f"unhandled {type(e).__name__} for mobile={mobile!r}: {e}")


def main():
	frappe.init(site=SITE, sites_path=SITES_PATH)
	frappe.connect()
	frappe.set_user("Administrator")
	frappe.flags.in_test = True  # like `bench run-tests`: queued jobs run inline (no Redis queue needed)
	status = sim("GET", "/api/status")
	if "outcome_mode" not in status:
		print(f"simulator not reachable on {SIM}", file=sys.stderr)
		sys.exit(2)
	for k in ("outcome_mode", "manual_outcome", "random_weights"):
		_saved_sim[k] = status[k]
	argv = [a for a in sys.argv[1:] if not a.startswith("-")]
	loader = unittest.TestLoader()
	if argv:
		suite = loader.loadTestsFromNames(argv, sys.modules[__name__])
	else:
		suite = unittest.TestSuite(
			loader.loadTestsFromTestCase(c)
			for c in (TestTerminalFlow, TestPreflightGuards, TestGatewayMisbehaviour, TestPaymentLinkPreflight, TestPaymentLinkFlow)
		)
	try:
		result = unittest.TextTestRunner(verbosity=2).run(suite)
	finally:
		cleanup()
		remove_link_mode_account()
		restore_payment_links_flag()
		sim("POST", "/api/config", {**_saved_sim, "webhook_enabled": True})
		sim("POST", "/api/reset")
	sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
	main()
