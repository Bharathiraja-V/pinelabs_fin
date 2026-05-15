# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Phase 1 happy-path + state-machine tests.

Run with:
  bench --site <site> run-tests --app pinelabs_fin --module pinelabs_fin.api.test_phase1
"""

import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import cint


class TestTransactionStateMachine(unittest.TestCase):
	"""Pinelabs Transaction controller + transaction.py state transitions."""

	def setUp(self):
		# Per-test invoice + transaction so each test fully tears down via
		# rollback. Moving this off `setUpClass` matters because tearDown's
		# rollback unwinds the invoice along with everything else — running
		# the invoice creation once at class-level leaves later tests staring
		# at a LinkValidationError when they reference a long-rolled-back row.
		from pinelabs_fin.api import transaction as txn_service
		self._invoice = _make_test_invoice()
		self.txn = txn_service.create_transaction(
			reference_doctype="Sales Invoice",
			reference_name=self._invoice.name,
			flow_type="Plural",
			amount=100,
		)

	def tearDown(self):
		frappe.db.rollback()

	def test_initial_status_is_initiated(self):
		self.assertEqual(self.txn.status, "INITIATED")

	def test_flow_type_is_locked_after_creation(self):
		self.txn.flow_type = "Terminal"
		with self.assertRaises(frappe.ValidationError):
			self.txn.save(ignore_permissions=True)

	def test_manual_status_to_success_is_blocked(self):
		"""Direct UI edit to SUCCESS must be rejected; only the service may transition."""
		self.txn.status = "SUCCESS"
		with self.assertRaises(frappe.ValidationError):
			self.txn.save(ignore_permissions=True)

	def test_illegal_transition_initiated_to_initiated_silent(self):
		"""No-op save (same status) is allowed."""
		self.txn.save(ignore_permissions=True)
		self.assertEqual(self.txn.status, "INITIATED")

	def test_pending_to_initiated_is_rejected(self):
		from pinelabs_fin.api import transaction as txn_service
		txn_service.mark_pending(self.txn, order_id="ORD-1")
		self.txn = frappe.get_doc("Pinelabs Transaction", self.txn.name)
		self.txn.status = "INITIATED"
		self.txn.flags.pinelabs_internal_transition = True
		with self.assertRaises(frappe.ValidationError):
			self.txn.save(ignore_permissions=True)


class TestPaymentEntryGuards(unittest.TestCase):
	"""mark_success() respects the 7 PE creation rules from PHASE_1_PLAN §8."""

	@classmethod
	def setUpClass(cls):
		cls._invoice = _make_test_invoice()

	def tearDown(self):
		frappe.db.rollback()

	def _make_txn_in_pending(self):
		from pinelabs_fin.api import transaction as txn_service
		txn = txn_service.create_transaction(
			reference_doctype="Sales Invoice",
			reference_name=self._invoice.name,
			flow_type="Plural",
			amount=100,
			payment_method="UPI",
		)
		return txn_service.mark_pending(txn, order_id="ORD-PE-TEST")

	def test_idempotent_double_mark_success(self):
		from pinelabs_fin.api import transaction as txn_service
		txn = self._make_txn_in_pending()
		txn1 = txn_service.mark_success(txn, payment_id="PAY-DEDUPE-1", payment_method="UPI")
		txn2 = txn_service.mark_success(txn1, payment_id="PAY-DEDUPE-1", payment_method="UPI")
		self.assertEqual(txn1.payment_entry, txn2.payment_entry)
		# Exactly one PE was created.
		count = frappe.db.count("Payment Entry", {"reference_no": "PAY-DEDUPE-1"})
		self.assertEqual(count, 1)

	def test_existing_pe_with_same_reference_no_is_reused(self):
		"""Rule 2: pre-existing PE with same reference_no → no duplicate."""
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		from pinelabs_fin.api import transaction as txn_service

		# Per-run unique reference_no so this test is isolated from prior
		# runs. ``mark_success`` commits internally, so a hardcoded value
		# would accumulate one extra PE in the DB per run and the count
		# assertion below would drift away from 1 forever.
		ref_no = f"PAY-PRE-EXISTING-{frappe.generate_hash(length=8)}"

		# Pre-create a PE manually.
		pe = get_payment_entry("Sales Invoice", self._invoice.name)
		pe.mode_of_payment = "PineLabs - UPI"
		pe.reference_no = ref_no
		pe.reference_date = frappe.utils.nowdate()
		pe.flags.ignore_permissions = True
		pe.insert()
		pe.submit()

		# Now mark the transaction success with the same reference_no.
		txn = self._make_txn_in_pending()
		txn = txn_service.mark_success(txn, payment_id=ref_no, payment_method="UPI")

		# The transaction should link to the existing PE, not a new one.
		self.assertEqual(txn.payment_entry, pe.name)
		count = frappe.db.count("Payment Entry", {"reference_no": ref_no})
		self.assertEqual(count, 1)


class TestWebhookSignature(unittest.TestCase):
	"""webhook.handle_webhook validates HMAC before parsing JSON."""

	def setUp(self):
		settings = frappe.get_single("Pinelabs Settings")
		settings.enable_payment_links = 1
		settings.payment_link_webhook_secret = "test-secret-1234567890abcdef"
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()
		frappe.local.request = None

	def _request(self, body, signature):
		class _Req:
			method = "POST"
			headers = {"X-Verify": signature}

			def __init__(self, raw):
				self._raw = raw

			def get_json(self):
				return json.loads(self._raw)

			def get_data(self, as_text=False):
				return self._raw if as_text else self._raw.encode("utf-8")

		frappe.local.request = _Req(body)

	def test_rejects_invalid_signature(self):
		from pinelabs_fin.api import webhook as webhook_module
		body = json.dumps({"event_type": "ORDER_PROCESSED", "data": {"order_id": "x"}})
		self._request(body, signature="0" * 64)
		result = webhook_module.handle_webhook()
		self.assertFalse(result.get("success"))
		self.assertIn("signature", (result.get("message") or "").lower())

	def test_accepts_valid_signature(self):
		from pinelabs_fin.api import webhook as webhook_module
		body = json.dumps({"event_type": "ORDER_PROCESSED", "data": {"order_id": "no-such-order"}})
		sig = hmac.new(b"test-secret-1234567890abcdef", body.encode("utf-8"), hashlib.sha256).hexdigest()
		self._request(body, signature=sig)
		result = webhook_module.handle_webhook()
		# 404 (unknown order) is a "success" outcome from a signature-validation perspective.
		self.assertIn("matches", (result.get("message") or "").lower() + (result.get("status") or ""))


class TestTerminalInitiate(unittest.TestCase):
	"""terminal.initiate_terminal_payment with the Pine Labs HTTP client mocked."""

	def setUp(self):
		# Per-test invoice — same reason as TestTransactionStateMachine.
		# `_ensure_default_machine` is idempotent so re-running it per-test
		# costs effectively nothing and keeps the fixture intact across
		# the tearDown rollback.
		self._invoice = _make_test_invoice()
		_ensure_default_machine()

	def tearDown(self):
		frappe.db.rollback()

	def test_pending_on_response_code_zero(self):
		from pinelabs_fin.api import terminal as terminal_module

		fake = {
			"success": True,
			"data": {"ResponseCode": 0, "ResponseMessage": "ok", "PlutusTransactionReferenceID": 12345},
		}
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.upload_billed_raw",
			return_value=fake,
		):
			result = terminal_module.initiate_terminal_payment(
				reference_doctype="Sales Invoice",
				reference_name=self._invoice.name,
				mode_of_payment="PineLabs - Card",
			)

		self.assertTrue(result["success"])
		txn = frappe.get_doc("Pinelabs Transaction", result["transaction_name"])
		self.assertEqual(txn.status, "PENDING")
		self.assertEqual(str(txn.payment_id), "12345")

	def test_failed_on_response_code_two(self):
		from pinelabs_fin.api import terminal as terminal_module

		fake = {
			"success": True,
			"data": {"ResponseCode": 2, "ResponseMessage": "Declined"},
		}
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.upload_billed_raw",
			return_value=fake,
		):
			result = terminal_module.initiate_terminal_payment(
				reference_doctype="Sales Invoice",
				reference_name=self._invoice.name,
				mode_of_payment="PineLabs - Card",
			)

		self.assertFalse(result["success"])
		txn = frappe.get_doc("Pinelabs Transaction", result["transaction_name"])
		self.assertEqual(txn.status, "FAILED")


class TestRouting(unittest.TestCase):
	"""resolve_machine_for_context honours terminal_routing_mode + mappings."""

	@classmethod
	def setUpClass(cls):
		cls._invoice = _make_test_invoice()

	def setUp(self):
		# `tearDown` rolls back, so re-apply the fixture per-test — both for
		# the routing machines and for clearing out any stray mappings from a
		# prior partial run. _ensure_routing_machines is idempotent against
		# rows already present.
		_ensure_routing_machines()
		settings = frappe.get_single("Pinelabs Settings")
		settings.set("machine_mappings", [])
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def _set_mode(self, mode):
		settings = frappe.get_single("Pinelabs Settings")
		settings.terminal_routing_mode = mode
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)

	_MAPPING_TYPE_TO_DOCTYPE = {
		"User": "User",
		"Warehouse": "Warehouse",
		"Company": "Company",
		"POS Profile": "POS Profile",
	}

	def _add_mapping(self, *, mapping_type, reference_name, machine):
		# `reference_doctype` is set explicitly: Frappe runs `_validate_links`
		# on child rows before any controller hook, so a missing value here
		# would raise "Reference DocType must be set first".
		settings = frappe.get_single("Pinelabs Settings")
		settings.append("machine_mappings", {
			"mapping_type": mapping_type,
			"reference_doctype": self._MAPPING_TYPE_TO_DOCTYPE[mapping_type],
			"reference_name": reference_name,
			"machine": machine,
			"is_enabled": 1,
		})
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)
		return settings.machine_mappings[-1]

	def test_single_mode_uses_default(self):
		from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
			resolve_machine_for_context,
		)

		self._set_mode("Single Machine")
		# Even with a mapping that would otherwise match, Single mode ignores it.
		self._add_mapping(
			mapping_type="User",
			reference_name=frappe.session.user,
			machine="Routing Machine A",
		)
		machine = resolve_machine_for_context("Sales Invoice", self._invoice.name)
		self.assertIsNotNone(machine)
		self.assertEqual(machine.machine_name, "Routing Default")

	def test_mapping_match_uses_mapped_machine(self):
		from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
			resolve_machine_for_context,
		)

		self._set_mode("Mapping")
		self._add_mapping(
			mapping_type="User",
			reference_name=frappe.session.user,
			machine="Routing Machine A",
		)
		machine = resolve_machine_for_context("Sales Invoice", self._invoice.name)
		self.assertIsNotNone(machine)
		self.assertEqual(machine.machine_name, "Routing Machine A")

	def test_mapping_no_match_falls_back_to_default(self):
		from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
			resolve_machine_for_context,
		)

		self._set_mode("Mapping")
		# Mapping for Guest — exists in every Frappe site, but the test runs
		# as Administrator, so this mapping must not match.
		self._add_mapping(
			mapping_type="User",
			reference_name="Guest",
			machine="Routing Machine A",
		)
		machine = resolve_machine_for_context("Sales Invoice", self._invoice.name)
		self.assertIsNotNone(machine)
		self.assertEqual(machine.machine_name, "Routing Default")

	def test_multiple_matches_pick_alphabetically_first_machine(self):
		from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
			resolve_machine_for_context,
		)

		self._set_mode("Mapping")
		# Two mappings that both match the current user.
		self._add_mapping(
			mapping_type="User",
			reference_name=frappe.session.user,
			machine="Routing Machine B",
		)
		self._add_mapping(
			mapping_type="User",
			reference_name=frappe.session.user,
			machine="Routing Machine A",
		)
		machine = resolve_machine_for_context("Sales Invoice", self._invoice.name)
		self.assertIsNotNone(machine)
		# Sort is alphabetical by machine name → A wins.
		self.assertEqual(machine.machine_name, "Routing Machine A")


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


def _make_test_invoice():
	settings = frappe.get_single("Pinelabs Settings")
	settings.enabled = 1
	settings.flags.ignore_mandatory = True

	# Make sure Sales Invoice is in payable_doctypes.
	if not any(r.doctype_name == "Sales Invoice" for r in (settings.payable_doctypes or [])):
		settings.append("payable_doctypes", {
			"doctype_name": "Sales Invoice",
			"is_enabled": 1,
			"amount_field": "outstanding_amount",
		})
	settings.save(ignore_permissions=True)

	invoice = frappe.get_doc({
		"doctype": "Sales Invoice",
		"customer": "_Test Customer",
		"company": "_Test Company",
		"debit_to": "_Test Receivable - _TC",
		"items": [{"item_code": "_Test Item", "qty": 1, "rate": 100}],
		"due_date": frappe.utils.nowdate(),
	})
	invoice.insert(ignore_permissions=True)
	invoice.submit()
	return invoice


def _ensure_default_machine():
	settings = frappe.get_single("Pinelabs Settings")
	if not settings.machines:
		settings.append("machines", {
			"machine_name": "Test Machine",
			"merchant_id": "MID-TEST",
			"client_id": "CID-TEST",
			"security_token": "ST-TEST",
			"store_id": "STORE-1",
			"enabled": 1,
			"is_default": 1,
		})
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)
	_ensure_pinelabs_mop_accounts()


def _ensure_pinelabs_mop_accounts():
	"""Map a default account on each PineLabs Mode of Payment for _Test Company.

	The initiate-time account-mapping pre-flight (transaction.assert_mode_has_account_mapped)
	would otherwise throw before any of the mocked HTTP calls is reached.
	"""
	company = "_Test Company"
	default_account = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Bank"}, "name"
	) or "_Test Bank - _TC"
	for mode in ("PineLabs - Card", "PineLabs - UPI", "PineLabs - Payment Link"):
		if not frappe.db.exists("Mode of Payment", mode):
			continue
		already = frappe.db.exists(
			"Mode of Payment Account", {"parent": mode, "company": company}
		)
		if already:
			continue
		mode_doc = frappe.get_doc("Mode of Payment", mode)
		mode_doc.append("accounts", {"company": company, "default_account": default_account})
		mode_doc.flags.ignore_permissions = True
		mode_doc.save()


def _ensure_routing_machines():
	"""Three enabled machines for TestRouting: one default + two non-default."""
	settings = frappe.get_single("Pinelabs Settings")
	wanted = {
		"Routing Default": {"is_default": 1},
		"Routing Machine A": {"is_default": 0},
		"Routing Machine B": {"is_default": 0},
	}
	existing_names = {m.machine_name for m in (settings.machines or [])}
	added = False
	for name, attrs in wanted.items():
		if name in existing_names:
			continue
		settings.append("machines", {
			"machine_name": name,
			"merchant_id": f"MID-{name.replace(' ', '-')}",
			"client_id": f"CID-{name.replace(' ', '-')}",
			"security_token": f"ST-{name.replace(' ', '-')}",
			"store_id": "STORE-1",
			"enabled": 1,
			"is_default": attrs["is_default"],
		})
		added = True
	if added:
		# Force exactly one default — drop is_default on any other rows.
		for m in settings.machines:
			if m.machine_name not in wanted and cint(m.is_default):
				m.is_default = 0
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)
