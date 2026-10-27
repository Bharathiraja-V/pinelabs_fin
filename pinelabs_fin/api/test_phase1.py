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

# The app commits inside mark_success (the customer has already been charged,
# so the Payment Entry must survive), which means a test's tearDown rollback
# cannot undo everything. Settings the tests flip — payment links on, a known
# webhook secret, the Plural base URL — would otherwise be left committed on
# whatever site the suite ran against. Snapshot them up front, restore after.
_SETTINGS_SCALARS = (
	"enabled",
	"enable_payment_links",
	"plural_client_id",
	"plural_base_url",
	"payment_link_expiry_minutes",
	"terminal_routing_mode",
	"auto_cancel_duration_minutes",
)
_SETTINGS_PASSWORDS = ("plural_client_secret", "payment_link_webhook_secret")
_settings_snapshot = {}


def setUpModule():
	settings = frappe.get_single("Pinelabs Settings")
	_settings_snapshot.clear()
	_settings_snapshot["scalars"] = {f: settings.get(f) for f in _SETTINGS_SCALARS}
	_settings_snapshot["passwords"] = {
		f: settings.get_password(f, raise_exception=False) for f in _SETTINGS_PASSWORDS
	}


def tearDownModule():
	if not _settings_snapshot:
		return
	frappe.set_user("Administrator")
	frappe.db.rollback()
	settings = frappe.get_single("Pinelabs Settings")
	for field, value in _settings_snapshot["scalars"].items():
		settings.set(field, value)
	for field, value in _settings_snapshot["passwords"].items():
		settings.set(field, value or "")
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")


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

	def setUp(self):
		# A FRESH invoice per test, not a class-level shared one. mark_success
		# calls frappe.db.commit() (the customer is already charged, so the PE
		# must be durable), which means tearDown's rollback cannot undo a PE
		# posted by an earlier test. Sharing one invoice let the alphabetically
		# earlier test_existing_pe_… fully pay it, so this class's other tests
		# then hit the zero-outstanding conflict guard and created no PE at all.
		self._invoice = _make_test_invoice()

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


class TestTerminalEdgeCases(unittest.TestCase):
	"""Pine Labs response edge cases — race windows, age-outs, hard fails."""

	def setUp(self):
		self._invoice = _make_test_invoice()
		_ensure_default_machine()

	def tearDown(self):
		frappe.db.rollback()

	def _create_pending_txn(self, plutus_ref="999"):
		"""Drive a txn to PENDING via the normal initiate path (mocked)."""
		from pinelabs_fin.api import terminal as terminal_module
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.upload_billed_raw",
			return_value={
				"success": True,
				"data": {
					"ResponseCode": 0,
					"ResponseMessage": "ok",
					"PlutusTransactionReferenceID": plutus_ref,
				},
			},
		):
			result = terminal_module.initiate_terminal_payment(
				reference_doctype="Sales Invoice",
				reference_name=self._invoice.name,
				mode_of_payment="PineLabs - Card",
			)
		return frappe.get_doc("Pinelabs Transaction", result["transaction_name"])

	def test_gap4_missing_plutus_ref_marks_failed(self):
		"""Gap 4: ResponseCode 0 without PlutusTransactionReferenceID must hard-fail."""
		from pinelabs_fin.api import terminal as terminal_module

		fake = {
			"success": True,
			"data": {"ResponseCode": 0, "ResponseMessage": "OK"},  # no PlutusTransactionReferenceID
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
		self.assertIn("PlutusTransactionReferenceID", txn.error_message or "")

	def test_gap1_2_age_out_catches_paid_race(self):
		"""Gap 1+2: 1001 past cutoff → settlement detects customer paid → SUCCESS."""
		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="888")

		# First poll = 1001 (still waiting). Settlement final poll = 0 (paid in race).
		status_responses = [
			{"success": True, "data": {"ResponseCode": 1001, "ResponseMessage": "TXN UPLOADED"}},
			{"success": True, "data": {
				"ResponseCode": 0, "ResponseMessage": "APPROVED",
				"PlutusTransactionReferenceID": "888",
			}},
		]

		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			side_effect=status_responses,
		), patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.cancel_raw",
			return_value={"success": True, "data": {"ResponseCode": 0, "ResponseMessage": "CANCELLED"}},
		), patch(
			"pinelabs_fin.api.terminal._auto_cancel_minutes",
			return_value=0,  # any positive age trips the cutoff
		):
			result = terminal_module.check_terminal_status(txn.name)

		self.assertEqual(result["status"], "SUCCESS")
		reloaded = frappe.get_doc("Pinelabs Transaction", txn.name)
		self.assertEqual(reloaded.status, "SUCCESS")

	def test_gap1_2_age_out_no_race_marks_failed(self):
		"""Gap 1+2: 1001 past cutoff with no race → FAILED with 'Aged out' reason."""
		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="777")

		# First poll = 1001. Settlement final poll = 1 (declined / no race).
		status_responses = [
			{"success": True, "data": {"ResponseCode": 1001, "ResponseMessage": "TXN UPLOADED"}},
			{"success": True, "data": {"ResponseCode": 1, "ResponseMessage": "Declined"}},
		]

		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			side_effect=status_responses,
		), patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.cancel_raw",
			return_value={"success": True, "data": {"ResponseCode": 0, "ResponseMessage": "CANCELLED"}},
		), patch(
			"pinelabs_fin.api.terminal._auto_cancel_minutes",
			return_value=0,
		):
			result = terminal_module.check_terminal_status(txn.name)

		self.assertEqual(result["status"], "FAILED")
		reloaded = frappe.get_doc("Pinelabs Transaction", txn.name)
		self.assertEqual(reloaded.status, "FAILED")
		self.assertIn("Aged out", reloaded.error_message or "")

	def test_gap3_cancel_rejected_but_customer_paid_marks_success(self):
		"""Gap 3: cancel API rejects ('already processed') → verify poll → SUCCESS."""
		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="666")

		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.cancel_raw",
			return_value={"success": True, "data": {
				"ResponseCode": 1, "ResponseMessage": "Transaction already processed",
			}},
		), patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			return_value={"success": True, "data": {
				"ResponseCode": 0, "ResponseMessage": "APPROVED",
				"PlutusTransactionReferenceID": "666",
			}},
		):
			result = terminal_module.cancel_terminal_payment(txn.name, reason="user")

		self.assertEqual(result["status"], "SUCCESS")
		reloaded = frappe.get_doc("Pinelabs Transaction", txn.name)
		self.assertEqual(reloaded.status, "SUCCESS")

	def test_gap3_cancel_rejected_and_not_paid_marks_cancelled(self):
		"""Gap 3 fallback: cancel rejected + status also non-zero → CANCELLED (no payment dropped)."""
		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="555")

		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.cancel_raw",
			return_value={"success": True, "data": {
				"ResponseCode": 1, "ResponseMessage": "Cannot cancel",
			}},
		), patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			return_value={"success": True, "data": {
				"ResponseCode": 1001, "ResponseMessage": "TXN UPLOADED",
			}},
		):
			result = terminal_module.cancel_terminal_payment(txn.name, reason="user")

		self.assertEqual(result["status"], "CANCELLED")
		reloaded = frappe.get_doc("Pinelabs Transaction", txn.name)
		self.assertEqual(reloaded.status, "CANCELLED")

	def test_response_status_matches_db_after_initiate(self):
		"""Regression: API response `status` must reflect the DB state, not
		the pre-call state. Prior bug: `_shape` read txn.status BEFORE the
		caller captured the reloaded txn from mark_*, so accepted txns
		reported "INITIATED" instead of "PENDING"."""
		from pinelabs_fin.api import terminal as terminal_module

		# Accepted by Pine Labs → DB should be PENDING.
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.upload_billed_raw",
			return_value={
				"success": True,
				"data": {
					"ResponseCode": 0, "ResponseMessage": "ok",
					"PlutusTransactionReferenceID": 333,
				},
			},
		):
			result = terminal_module.initiate_terminal_payment(
				reference_doctype="Sales Invoice",
				reference_name=self._invoice.name,
				mode_of_payment="PineLabs - Card",
			)
		db_txn = frappe.get_doc("Pinelabs Transaction", result["transaction_name"])
		self.assertEqual(result["status"], db_txn.status)
		self.assertEqual(result["status"], "PENDING")

	def test_response_status_matches_db_after_sync_decline(self):
		"""Same regression: sync decline → response should say FAILED, not INITIATED."""
		from pinelabs_fin.api import terminal as terminal_module

		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.upload_billed_raw",
			return_value={
				"success": True,
				"data": {"ResponseCode": 2, "ResponseMessage": "Declined"},
			},
		):
			result = terminal_module.initiate_terminal_payment(
				reference_doctype="Sales Invoice",
				reference_name=self._invoice.name,
				mode_of_payment="PineLabs - Card",
			)
		db_txn = frappe.get_doc("Pinelabs Transaction", result["transaction_name"])
		self.assertEqual(result["status"], db_txn.status)
		self.assertEqual(result["status"], "FAILED")

	def test_draft_source_doc_accepted_and_auto_submitted_on_success(self):
		"""Drafts (docstatus=0) are now accepted at initiate. On SUCCESS, the
		source doc is auto-submitted before the Payment Entry is created."""
		from pinelabs_fin.api import terminal as terminal_module

		# Build a fresh DRAFT invoice (no submit) — same shape as _make_test_invoice
		# but stops at insert().
		fx = _fixture()
		draft = frappe.get_doc({
			"doctype": "Sales Invoice",
			"customer": fx["customer"],
			"company": fx["company"],
			"debit_to": fx["debit_to"],
			"items": [{"item_code": fx["item"], "qty": 1, "rate": 100}],
			"due_date": frappe.utils.nowdate(),
		})
		draft.insert(ignore_permissions=True)
		self.assertEqual(draft.docstatus, 0)

		# Unique Plutus ref per run — mark_success commits the resulting PE
		# (real-world durability requirement) and tearDown's rollback can't
		# revert committed data. If we reused a fixed ref, the next run
		# would find the previous PE via _create_payment_entry_idempotent's
		# Rule-2 early return and short-circuit BEFORE the auto-submit step,
		# making this assertion flaky.
		plutus_ref = int(frappe.generate_hash(length=10), 16) % (10 ** 8)

		# Initiate against the draft — must NOT throw "must be submitted".
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.upload_billed_raw",
			return_value={
				"success": True,
				"data": {
					"ResponseCode": 0, "ResponseMessage": "ok",
					"PlutusTransactionReferenceID": plutus_ref,
				},
			},
		):
			result = terminal_module.initiate_terminal_payment(
				reference_doctype="Sales Invoice",
				reference_name=draft.name,
				mode_of_payment="PineLabs - Card",
				amount=100,
			)
		self.assertTrue(result["success"])
		self.assertEqual(result["status"], "PENDING")

		# Customer pays → poll returns APPROVED → mark_success kicks in.
		# Auto-submit must promote the draft to docstatus=1 before PE creation.
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			return_value={
				"success": True,
				"data": {
					"ResponseCode": 0, "ResponseMessage": "APPROVED",
					"PlutusTransactionReferenceID": plutus_ref,
				},
			},
		):
			poll_result = terminal_module.check_terminal_status(result["transaction_name"])

		self.assertEqual(poll_result["status"], "SUCCESS")
		reloaded_invoice = frappe.get_doc("Sales Invoice", draft.name)
		self.assertEqual(reloaded_invoice.docstatus, 1)  # auto-submitted ✓
		txn = frappe.get_doc("Pinelabs Transaction", result["transaction_name"])
		self.assertEqual(txn.status, "SUCCESS")
		self.assertTrue(txn.payment_entry)  # PE was created against the now-submitted doc

	def test_response_code_1006_timeout_marks_failed(self):
		"""ResponseCode 1006 ('NO RESPONSE FROM PLUTUS' — Pine Labs definitive
		timeout) must mark the txn FAILED immediately, not sit in the
		unknown-code branch waiting for age-out."""
		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="606")
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			return_value={
				"success": True,
				"data": {
					"ResponseCode": 1006,
					"ResponseMessage": "NO RESPONSE FROM PLUTUS",
				},
			},
		):
			result = terminal_module.check_terminal_status(txn.name)

		self.assertEqual(result["status"], "FAILED")
		self.assertEqual(result["response_code"], 1006)
		reloaded = frappe.get_doc("Pinelabs Transaction", txn.name)
		self.assertEqual(reloaded.status, "FAILED")
		self.assertIn("NO RESPONSE FROM PLUTUS", reloaded.error_message or "")

	def test_response_code_1008_voided_marks_cancelled(self):
		"""ResponseCode 1008 ('TXN VOIDED' — cancelled at terminal UI or
		via another system call) must transition the txn to CANCELLED so
		the dialog closes and the source doc reconciles correctly."""
		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="608")
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			return_value={
				"success": True,
				"data": {
					"ResponseCode": 1008,
					"ResponseMessage": "TXN VOIDED",
				},
			},
		):
			result = terminal_module.check_terminal_status(txn.name)

		self.assertEqual(result["status"], "CANCELLED")
		self.assertEqual(result["response_code"], 1008)
		reloaded = frappe.get_doc("Pinelabs Transaction", txn.name)
		self.assertEqual(reloaded.status, "CANCELLED")

	def test_poll_terminal_status_delegates_to_check_terminal_status(self):
		"""poll_terminal_status is a thin alias: the dialog's internal poll
		hits it to bypass consumer-app shims that filter on the original
		method name. Behaviour must be identical to check_terminal_status."""
		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="811")
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			return_value={
				"success": True,
				"data": {
					"ResponseCode": 0, "ResponseMessage": "APPROVED",
					"PlutusTransactionReferenceID": 811,
				},
			},
		):
			result = terminal_module.poll_terminal_status(txn.name)

		# Alias must produce identical outcome to check_terminal_status —
		# SUCCESS, response_code=0, txn flipped in DB.
		self.assertEqual(result["status"], "SUCCESS")
		self.assertEqual(result["response_code"], 0)
		reloaded = frappe.get_doc("Pinelabs Transaction", txn.name)
		self.assertEqual(reloaded.status, "SUCCESS")

	def test_poll_terminal_status_tolerates_concurrent_race(self):
		"""TimestampMismatchError from a concurrent poll must not propagate
		to the caller — the alias returns the DB-cached state instead so
		the dialog doesn't surface an error popup to the operator."""
		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="812")
		# Simulate the inner check_terminal_status raising mid-save (another
		# poller already committed the transition between our load and save).
		with patch(
			"pinelabs_fin.api.terminal.check_terminal_status",
			side_effect=frappe.TimestampMismatchError("simulated race"),
		):
			result = terminal_module.poll_terminal_status(txn.name)

		# Should NOT raise. Should return the persisted (pre-race) state.
		self.assertTrue(result["success"])
		self.assertIn(result["status"], ("PENDING", "INITIATED", "SUCCESS", "FAILED", "CANCELLED"))

	def test_retry_failed_terminal_payment_single_round_trip(self):
		"""retry_failed_terminal_payment should derive everything it needs
		from the original txn's reference + payment_method and re-run
		initiate_terminal_payment — no separate get_value lookup from the
		browser."""
		from pinelabs_fin.api import terminal as terminal_module

		# Unique plutus_refs per run — see test_retry_failed_terminal_payment_rejects_unknown_method
		# for the same isolation reasoning.
		base = int(frappe.generate_hash(length=10), 16) % (10 ** 7)
		original_ref = str(base)
		retry_ref = base + 1
		original = self._create_pending_txn(plutus_ref=original_ref)
		# Force it to FAILED so retry has something to recover from.
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			return_value={
				"success": True,
				"data": {"ResponseCode": 1, "ResponseMessage": "Declined"},
			},
		):
			terminal_module.check_terminal_status(original.name)
		reloaded = frappe.get_doc("Pinelabs Transaction", original.name)
		self.assertEqual(reloaded.status, "FAILED")

		# Now retry — server-side lookup should pull
		# reference_doctype/reference_name/payment_method from the original
		# txn and forward to initiate_terminal_payment automatically.
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.upload_billed_raw",
			return_value={
				"success": True,
				"data": {
					"ResponseCode": 0, "ResponseMessage": "ok",
					"PlutusTransactionReferenceID": retry_ref,
				},
			},
		):
			result = terminal_module.retry_failed_terminal_payment(original.name)

		self.assertTrue(result["success"])
		self.assertEqual(result["status"], "PENDING")
		# A NEW txn row must have been created — the original FAILED row
		# is preserved.
		self.assertNotEqual(result["transaction_name"], original.name)
		new_txn = frappe.get_doc("Pinelabs Transaction", result["transaction_name"])
		self.assertEqual(new_txn.reference_name, original.reference_name)
		self.assertEqual(new_txn.payment_method, original.payment_method)

	def test_retry_failed_terminal_payment_rejects_unknown_method(self):
		"""If the original txn has an unrecognised payment_method (data
		corruption / external tampering), retry must throw with a clear
		error rather than silently issuing initiate with garbage."""
		from pinelabs_fin.api import terminal as terminal_module

		# Unique plutus_ref per run — _create_pending_txn → mark_pending →
		# commits payment_id to the DB. tearDown's rollback can't revert
		# committed rows, so a fixed ref would collide with prior runs
		# (active-txn guard: "Two active transactions cannot share a
		# provider id").
		plutus_ref = int(frappe.generate_hash(length=10), 16) % (10 ** 8)
		original = self._create_pending_txn(plutus_ref=str(plutus_ref))
		# Stomp the payment_method to a value not in PINELABS_METHOD_TO_MODE.
		# Use db_set to bypass the doctype's status state machine.
		frappe.db.set_value(
			"Pinelabs Transaction",
			original.name,
			{"payment_method": "BITCOIN", "status": "FAILED"},
		)
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError) as cm:
			terminal_module.retry_failed_terminal_payment(original.name)
		self.assertIn("recognised payment method", str(cm.exception))

	def test_cancelled_source_doc_still_rejected(self):
		"""Cancelled docs (docstatus=2) are immutable in Frappe — initiate must
		still refuse with a clear error."""
		from pinelabs_fin.api import terminal as terminal_module

		# Submit, then cancel the test invoice.
		self._invoice.cancel()
		self.assertEqual(frappe.get_doc("Sales Invoice", self._invoice.name).docstatus, 2)

		with self.assertRaises(frappe.ValidationError) as cm:
			terminal_module.initiate_terminal_payment(
				reference_doctype="Sales Invoice",
				reference_name=self._invoice.name,
				mode_of_payment="PineLabs - Card",
				amount=100,
			)
		self.assertIn("cancelled", str(cm.exception).lower())

	def test_force_cancel_fallback_fires_when_regular_cancel_rejects(self):
		"""Age-out path: regular cancel returns non-zero → force_cancel_raw must be
		called as a fallback to stop a frozen terminal before the final-poll
		settlement decision."""
		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="222")

		# Sequence Pine Labs will return:
		#  1st poll (in check_terminal_status): 1001 → triggers age-out
		#  inside _attempt_age_out_settlement:
		#    cancel_raw → ResponseCode 1 (rejected) → fallback fires
		#    force_cancel_raw → called (spy)
		#    final get_status_raw → ResponseCode 1 (declined / no race) → mark FAILED
		status_responses = [
			{"success": True, "data": {"ResponseCode": 1001, "ResponseMessage": "TXN UPLOADED"}},
			{"success": True, "data": {"ResponseCode": 1, "ResponseMessage": "Declined"}},
		]

		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			side_effect=status_responses,
		), patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.cancel_raw",
			return_value={"success": True, "data": {"ResponseCode": 1, "ResponseMessage": "Cannot cancel"}},
		), patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.force_cancel_raw",
			return_value={"success": True, "data": {"ResponseCode": 0, "ResponseMessage": "Force cancelled"}},
		) as force_cancel_spy, patch(
			"pinelabs_fin.api.terminal._auto_cancel_minutes",
			return_value=0,
		):
			result = terminal_module.check_terminal_status(txn.name)

		# Force Cancel must have been invoked exactly once (the fallback path).
		self.assertEqual(force_cancel_spy.call_count, 1)
		self.assertEqual(result["status"], "FAILED")
		reloaded = frappe.get_doc("Pinelabs Transaction", txn.name)
		self.assertEqual(reloaded.status, "FAILED")

	def test_force_cancel_not_called_when_regular_cancel_succeeds(self):
		"""Symmetry test: when cancel_raw returns 0, force_cancel_raw must NOT
		be called (avoid two redundant network calls on the happy path)."""
		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="111")

		status_responses = [
			{"success": True, "data": {"ResponseCode": 1001, "ResponseMessage": "TXN UPLOADED"}},
			{"success": True, "data": {"ResponseCode": 1, "ResponseMessage": "Declined"}},
		]

		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			side_effect=status_responses,
		), patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.cancel_raw",
			return_value={"success": True, "data": {"ResponseCode": 0, "ResponseMessage": "Cancelled"}},
		), patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.force_cancel_raw",
			return_value={"success": True, "data": {"ResponseCode": 0}},
		) as force_cancel_spy, patch(
			"pinelabs_fin.api.terminal._auto_cancel_minutes",
			return_value=0,
		):
			terminal_module.check_terminal_status(txn.name)

		self.assertEqual(force_cancel_spy.call_count, 0)

	def test_upi_reconciliation_fields_captured_on_success(self):
		"""HostTxnID and VPA must land on the response_payload after parsing
		so UPI reconciliation queries can read them as named keys."""
		import json as _json

		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="333")

		approved_response = {
			"success": True,
			"data": {
				"ResponseCode": 0,
				"ResponseMessage": "APPROVED",
				"PlutusTransactionReferenceID": 333,
				"TransactionData": [
					{"Tag": "PaymentMode", "Value": "UPI"},
					{"Tag": "HostTxnID", "Value": "316524112233"},
					{"Tag": "VPA", "Value": "customer@upi"},
					{"Tag": "RRN", "Value": "611234567890"},
				],
				# _parse_response also merges flattened tags onto the response
				# dict before we see it — simulate that here.
				"HostTxnID": "316524112233",
				"VPA": "customer@upi",
				"RRN": "611234567890",
			},
		}

		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			return_value=approved_response,
		):
			result = terminal_module.check_terminal_status(txn.name)

		self.assertEqual(result["status"], "SUCCESS")
		reloaded = frappe.get_doc("Pinelabs Transaction", txn.name)
		payload = _json.loads(reloaded.response_payload)
		self.assertEqual(payload.get("HostTxnID"), "316524112233")
		self.assertEqual(payload.get("VPA"), "customer@upi")

	def test_gap5_network_error_message_propagated(self):
		"""Gap 5: timeout/HTTP error during poll → captured error_message includes the reason."""
		from pinelabs_fin.api import terminal as terminal_module

		txn = self._create_pending_txn(plutus_ref="444")

		# `_handle_request_exception` shape: success=False with the error text.
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.get_status_raw",
			return_value={
				"success": False,
				"error": "Request timeout - Pine Labs server did not respond",
				"data": {},
				"status_code": 0,
			},
		):
			terminal_module.check_terminal_status(txn.name)

		reloaded = frappe.get_doc("Pinelabs Transaction", txn.name)
		self.assertIn("Request timeout", reloaded.error_message or "")


class TestPluralMerchantReference(unittest.TestCase):
	"""BUG-02: each Plural payment link uses the unique Pinelabs Transaction name
	(PLT-...) as its merchant reference — never the invoice name — so two links
	for the same invoice can be told apart on status refresh.
	"""

	def setUp(self):
		self._invoice = _make_test_invoice()
		# Maps the "PineLabs - Payment Link" Mode of Payment account so the
		# initiate-time account pre-flight passes.
		_ensure_default_machine()
		# The Plural flow is now gated on both Settings switches (previously
		# neither reached the API), so the fixture has to turn them on.
		_set_settings(enabled=1, enable_payment_links=1)

	def tearDown(self):
		frappe.db.rollback()

	@staticmethod
	def _fake_create(ok=True, link_id="PLINK-1"):
		if ok:
			return {
				"success": True,
				"payment_link_url": "https://plural.example/pay/x",
				"payment_link_id": link_id,
				"raw": {},
			}
		return {"success": False, "error": "gateway boom", "raw": {}}

	def test_payload_uses_txn_ref_and_keeps_invoice_in_description(self):
		from pinelabs_fin.api.plural import _build_payment_link_payload

		body = _build_payment_link_payload(
			merchant_ref="PLT-2026-00042",
			description_ref="ACC-SINV-2026-00001",
			amount=100,
			expiry_minutes=1440,
			customer={},
			allowed_methods=None,
		)
		self.assertEqual(body["merchant_payment_link_reference"], "PLT-2026-00042")
		self.assertEqual(body["description"], "Payment for ACC-SINV-2026-00001")

	def test_two_attempts_same_invoice_get_distinct_references(self):
		from pinelabs_fin.api import plural

		# First attempt fails at the gateway → txn goes FAILED, so the
		# active-transaction guard allows a second attempt on the same invoice.
		with patch(
			"pinelabs_fin.api.plural_client.PluralClient.create_payment_link",
			return_value=self._fake_create(ok=False),
		):
			r1 = plural.initiate_payment_link(
				reference_doctype="Sales Invoice",
				reference_name=self._invoice.name,
				customer_email="buyer@example.com",
				customer_mobile="9876543210",
			)
		self.assertFalse(r1["success"])

		with patch(
			"pinelabs_fin.api.plural_client.PluralClient.create_payment_link",
			return_value=self._fake_create(ok=True, link_id="PLINK-2"),
		):
			r2 = plural.initiate_payment_link(
				reference_doctype="Sales Invoice",
				reference_name=self._invoice.name,
				customer_email="buyer@example.com",
				customer_mobile="9876543210",
			)
		self.assertTrue(r2["success"])

		t1 = frappe.get_doc("Pinelabs Transaction", r1["transaction_name"])
		t2 = frappe.get_doc("Pinelabs Transaction", r2["transaction_name"])
		ref1 = json.loads(t1.request_payload)["merchant_payment_link_reference"]
		ref2 = json.loads(t2.request_payload)["merchant_payment_link_reference"]

		# Core of BUG-02: each reference is the txn's own name, they differ, and
		# neither is the (shared) invoice name.
		self.assertEqual(ref1, t1.name)
		self.assertEqual(ref2, t2.name)
		self.assertNotEqual(ref1, ref2)
		self.assertNotEqual(ref1, self._invoice.name)
		self.assertNotEqual(ref2, self._invoice.name)

	def test_status_refresh_queries_by_txn_reference_not_invoice(self):
		from pinelabs_fin.api import plural

		with patch(
			"pinelabs_fin.api.plural_client.PluralClient.create_payment_link",
			return_value=self._fake_create(ok=True, link_id="PLINK-3"),
		):
			r = plural.initiate_payment_link(
				reference_doctype="Sales Invoice",
				reference_name=self._invoice.name,
				customer_email="buyer@example.com",
				customer_mobile="9876543210",
			)
		txn_name = r["transaction_name"]

		captured = {}

		def fake_status(merchant_ref):
			captured["ref"] = merchant_ref
			return {"success": True, "status": "PENDING", "data": {"status": "PENDING"}}

		with patch(
			"pinelabs_fin.api.plural_client.PluralClient.get_payment_link_status",
			side_effect=fake_status,
		):
			plural.refresh_payment_link_status(txn_name)

		# The status API must be queried by the reference we actually sent
		# (the txn name), not the invoice name.
		self.assertEqual(captured["ref"], txn_name)
		self.assertNotEqual(captured["ref"], self._invoice.name)


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


def _make_test_invoice():
	settings = frappe.get_single("Pinelabs Settings")
	settings.enabled = 1
	settings.flags.ignore_mandatory = True

	# Make sure Sales Invoice is in payable_doctypes.
	row = None
	for candidate in (settings.payable_doctypes or []):
		if candidate.doctype_name == "Sales Invoice":
			row = candidate
			break
	if row is None:
		row = settings.append("payable_doctypes", {
			"doctype_name": "Sales Invoice",
			"amount_field": "outstanding_amount",
		})
	# Force the switches on whether the row is new or pre-existing. These are
	# now enforced server-side, and a site's real row may legitimately have
	# one of them off — which would fail every payment test for a reason that
	# has nothing to do with what is under test. amount_field is deliberately
	# left as configured: the test invoice has no taxes, so total /
	# grand_total / outstanding_amount all agree.
	row.is_enabled = 1
	row.enable_pay_on_machine = 1
	row.enable_send_payment_link = 1
	settings.save(ignore_permissions=True)

	fx = _fixture()
	invoice = frappe.get_doc({
		"doctype": "Sales Invoice",
		"customer": fx["customer"],
		"company": fx["company"],
		"debit_to": fx["debit_to"],
		"items": [{"item_code": fx["item"], "qty": 1, "rate": 100}],
		"due_date": frappe.utils.nowdate(),
	})
	invoice.insert(ignore_permissions=True)
	invoice.submit()
	return invoice


def _ensure_default_machine():
	settings = frappe.get_single("Pinelabs Settings")
	dirty = False
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
		dirty = True
	# The integration master switch and an ACTIVE API endpoint are both
	# required before PineLabsClient will build. A site that has been used
	# by hand already has them, which is why this fixture got away without
	# them for a long time — on a fresh site (a new bench, or CI) every
	# terminal test failed with "No active API endpoint configured".
	if not cint(settings.enabled):
		settings.enabled = 1
		dirty = True
	if not any(cint(e.is_active) for e in (settings.api_endpoints or [])):
		settings.append("api_endpoints", {
			"endpoints_name": "Test Endpoint",
			"base_api_url": "https://www.plutuscloudserviceuat.in:8201",
			"is_active": 1,
			"verify_ssl": 0,
		})
		dirty = True
	if dirty:
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")
	_ensure_pinelabs_mop_accounts()


def _fixture():
	"""Company / customer / item / receivable account the tests invoice against.

	Prefers ERPNext's ``_Test …`` records when the site has them (a site set up
	by ``bench run-tests`` for ERPNext), otherwise falls back to whatever the
	site already has, so the suite runs on an ordinary dev site as well. The
	item is a non-stock item where possible so no warehouse is needed.
	"""
	company = (
		"_Test Company"
		if frappe.db.exists("Company", "_Test Company")
		else frappe.db.get_value("Company", {}, "name")
	)
	if not company:
		raise unittest.SkipTest("No Company on this site; cannot build a Sales Invoice fixture.")

	customer = (
		"_Test Customer"
		if frappe.db.exists("Customer", "_Test Customer")
		else frappe.db.get_value("Customer", {"disabled": 0}, "name")
	)
	item = (
		"_Test Item"
		if frappe.db.exists("Item", "_Test Item")
		else (
			frappe.db.get_value("Item", {"disabled": 0, "is_stock_item": 0, "is_sales_item": 1}, "name")
			or frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1}, "name")
		)
	)
	debit_to = frappe.get_cached_value("Company", company, "default_receivable_account")
	if not (customer and item and debit_to):
		raise unittest.SkipTest("Site lacks a Customer / sales Item / receivable account for the fixture.")
	return {"company": company, "customer": customer, "item": item, "debit_to": debit_to}


def _ensure_pinelabs_mop_accounts():
	"""Map a default account on each PineLabs Mode of Payment for the fixture company.

	The initiate-time account-mapping pre-flight (transaction.assert_mode_has_account_mapped)
	would otherwise throw before any of the mocked HTTP calls is reached.
	"""
	company = _fixture()["company"]
	default_account = (
		frappe.db.get_value("Account", {"company": company, "account_type": "Bank", "is_group": 0}, "name")
		or frappe.db.get_value("Account", {"company": company, "account_type": "Cash", "is_group": 0}, "name")
	)
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


# ──────────────────────────────────────────────────────────────────────────
# Settings gates + authorization (api/access.py)
#
# Every Pinelabs Settings switch used to be enforced ONLY in the browser, so
# turning one off hid the button but left the whitelisted endpoint callable.
# These tests pin each switch to a server-side refusal.
# ──────────────────────────────────────────────────────────────────────────


class _StubDoc:
	"""Minimal stand-in for a source document (attribute access only)."""

	def __init__(self, **kwargs):
		self.__dict__.update(kwargs)


def _set_payable_row(doctype_name, **fields):
	"""Set fields on the Payable Documents row for ``doctype_name``.

	Rolled back by each test's tearDown.
	"""
	settings = frappe.get_single("Pinelabs Settings")
	row = None
	for candidate in (settings.payable_doctypes or []):
		if candidate.doctype_name == doctype_name:
			row = candidate
			break
	if row is None:
		row = settings.append("payable_doctypes", {
			"doctype_name": doctype_name,
			"is_enabled": 1,
			"amount_field": "outstanding_amount",
		})
	row.update(fields)
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)
	frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")
	return row


def _set_settings(**fields):
	settings = frappe.get_single("Pinelabs Settings")
	settings.update(fields)
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)
	frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")


class TestSettingsGates(unittest.TestCase):
	"""The Settings switches must refuse the API, not just hide the button."""

	def setUp(self):
		self.invoice = _make_test_invoice()
		_ensure_default_machine()

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")

	def _initiate_terminal(self):
		from pinelabs_fin.api import terminal

		return terminal.initiate_terminal_payment(
			reference_doctype="Sales Invoice",
			reference_name=self.invoice.name,
			mode_of_payment="PineLabs - Card",
		)

	def _initiate_plural(self):
		from pinelabs_fin.api import plural

		return plural.initiate_payment_link(
			reference_doctype="Sales Invoice",
			reference_name=self.invoice.name,
			customer_email="buyer@example.com",
			customer_mobile="9876543210",
		)

	# ── master switch ────────────────────────────────────────────────────

	def test_master_switch_off_blocks_terminal(self):
		_set_settings(enabled=0)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._initiate_terminal()
		self.assertIn("not enabled", str(ctx.exception))

	def test_master_switch_off_blocks_payment_link(self):
		"""The regression: 'enabled' gated Terminal via pinelabs_client but was
		never read by the Plural flow, so a link was still created and sent."""
		_set_settings(enabled=0, enable_payment_links=1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._initiate_plural()
		self.assertIn("not enabled", str(ctx.exception))

	# ── enable_payment_links ─────────────────────────────────────────────

	def test_payment_links_switch_off_blocks_plural(self):
		_set_settings(enabled=1, enable_payment_links=0)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._initiate_plural()
		self.assertIn("Payment Links are not enabled", str(ctx.exception))

	def test_payment_links_switch_off_does_not_block_terminal(self):
		"""It's a Plural-only switch — the terminal flow must be unaffected."""
		from pinelabs_fin.api import access

		_set_settings(enabled=1, enable_payment_links=0)
		# Gate only; the full initiate would need a live terminal.
		access.assert_integration_enabled("Terminal")

	# ── Payable Documents row ────────────────────────────────────────────

	def test_disabled_payable_row_blocks_both_flows(self):
		_set_settings(enabled=1, enable_payment_links=1)
		_set_payable_row("Sales Invoice", is_enabled=0)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._initiate_terminal()
		self.assertIn("disabled for Sales Invoice", str(ctx.exception))
		with self.assertRaises(frappe.ValidationError):
			self._initiate_plural()

	def test_per_flow_toggle_pay_on_machine(self):
		_set_settings(enabled=1, enable_payment_links=1)
		_set_payable_row(
			"Sales Invoice", is_enabled=1,
			enable_pay_on_machine=0, enable_send_payment_link=1,
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._initiate_terminal()
		self.assertIn("Pay on Machine", str(ctx.exception))

	def test_per_flow_toggle_send_payment_link(self):
		_set_settings(enabled=1, enable_payment_links=1)
		_set_payable_row(
			"Sales Invoice", is_enabled=1,
			enable_pay_on_machine=1, enable_send_payment_link=0,
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._initiate_plural()
		self.assertIn("Send Payment Link", str(ctx.exception))

	def test_no_payable_row_is_refused(self):
		"""Configuration is the allow-list: a doctype with NO Payable Documents
		row cannot start a payment (the old permissive path let any writable
		doctype arm the terminal)."""
		from pinelabs_fin.api import access

		_set_settings(enabled=1, enable_payment_links=1)
		# "ToDo" has no Payable Documents row.
		with self.assertRaises(frappe.ValidationError) as ctx:
			access.assert_doctype_payable("ToDo", "Terminal")
		self.assertIn("not configured", str(ctx.exception))
		with self.assertRaises(frappe.ValidationError):
			access.assert_doctype_payable("ToDo", "Plural")


class TestAuthorizationGates(unittest.TestCase):
	"""Whitelisted payment endpoints must check permissions."""

	def setUp(self):
		self.invoice = _make_test_invoice()
		_ensure_default_machine()
		_set_settings(enabled=1, enable_payment_links=1)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")

	def _make_powerless_user(self):
		email = "pinelabs_noperm@example.com"
		if not frappe.db.exists("User", email):
			user = frappe.get_doc({
				"doctype": "User",
				"email": email,
				"first_name": "NoPerm",
				"send_welcome_email": 0,
				"roles": [],
			})
			user.insert(ignore_permissions=True)
		return email

	def test_initiate_terminal_refuses_user_without_write_on_source(self):
		from pinelabs_fin.api import terminal

		frappe.set_user(self._make_powerless_user())
		with self.assertRaises(frappe.PermissionError):
			terminal.initiate_terminal_payment(
				reference_doctype="Sales Invoice",
				reference_name=self.invoice.name,
				mode_of_payment="PineLabs - Card",
			)

	def test_initiate_payment_link_refuses_user_without_write_on_source(self):
		from pinelabs_fin.api import plural

		frappe.set_user(self._make_powerless_user())
		with self.assertRaises(frappe.PermissionError):
			plural.initiate_payment_link(
				reference_doctype="Sales Invoice",
				reference_name=self.invoice.name,
				customer_email="buyer@example.com",
				customer_mobile="9876543210",
			)

	def test_txn_scoped_endpoint_refuses_foreign_user(self):
		"""IDOR: reading someone else's transaction by guessing its PLT- name."""
		from pinelabs_fin.api import terminal
		from pinelabs_fin.api import transaction as txn_service

		txn = txn_service.create_transaction(
			reference_doctype="Sales Invoice",
			reference_name=self.invoice.name,
			flow_type="Terminal",
			amount=100,
			payment_method="CARD",
		)
		frappe.set_user(self._make_powerless_user())
		with self.assertRaises(frappe.PermissionError):
			terminal.check_terminal_status(txn.name)

	def test_administrator_bypasses_the_gate(self):
		"""The cron reconciler runs as Administrator and must keep working."""
		from pinelabs_fin.api import access

		frappe.set_user("Administrator")
		access.assert_can_pay("Sales Invoice", self.invoice.name)


class TestConfiguredAmountField(unittest.TestCase):
	"""The Payable Documents 'Amount Field' must actually drive the charge.

	Settings made it mandatory, but nothing ever read it — both flows
	hardcoded outstanding_amount / grand_total, so a configured field was
	silently ignored and the customer was charged the full total.
	"""

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")

	def test_configured_field_wins_over_grand_total(self):
		from pinelabs_fin.api.transaction import resolve_payment_amount

		_set_payable_row("Sales Invoice", is_enabled=1, amount_field="custom_part_payment")
		doc = _StubDoc(
			doctype="Sales Invoice", docstatus=1,
			custom_part_payment=250, outstanding_amount=1000, grand_total=1000,
		)
		self.assertEqual(resolve_payment_amount(doc), 250.0)

	def test_falls_back_when_configured_field_is_empty(self):
		from pinelabs_fin.api.transaction import resolve_payment_amount

		_set_payable_row("Sales Invoice", is_enabled=1, amount_field="custom_part_payment")
		doc = _StubDoc(
			doctype="Sales Invoice", docstatus=1,
			custom_part_payment=0, outstanding_amount=600, grand_total=1000,
		)
		self.assertEqual(resolve_payment_amount(doc), 600.0)

	def test_settled_invoice_still_refused_even_with_configured_field(self):
		"""The configured field must not resurrect a fully-paid document."""
		from pinelabs_fin.api.transaction import resolve_payment_amount

		_set_payable_row("Sales Invoice", is_enabled=1, amount_field="custom_part_payment")
		doc = _StubDoc(
			doctype="Sales Invoice", docstatus=1,
			custom_part_payment=250, outstanding_amount=0, grand_total=1000,
		)
		self.assertEqual(resolve_payment_amount(doc), 0.0)

	def test_submitted_outstanding_is_authoritative_without_config(self):
		from pinelabs_fin.api.transaction import resolve_payment_amount

		_set_payable_row("Sales Invoice", is_enabled=1, amount_field="outstanding_amount")
		doc = _StubDoc(
			doctype="Sales Invoice", docstatus=1,
			outstanding_amount=400, grand_total=1000,
		)
		self.assertEqual(resolve_payment_amount(doc), 400.0)

	def test_draft_falls_back_to_rounded_total(self):
		from pinelabs_fin.api.transaction import resolve_payment_amount

		_set_payable_row("Sales Invoice", is_enabled=1, amount_field="outstanding_amount")
		doc = _StubDoc(
			doctype="Sales Invoice", docstatus=0,
			outstanding_amount=0, rounded_total=990, grand_total=987.65,
		)
		self.assertEqual(resolve_payment_amount(doc), 990.0)

	def test_both_flows_share_one_resolver(self):
		"""plural._amount_for used to be a separate, drifted copy."""
		from pinelabs_fin.api import plural, terminal

		_set_payable_row("Sales Invoice", is_enabled=1, amount_field="custom_part_payment")
		doc = _StubDoc(
			doctype="Sales Invoice", docstatus=1,
			custom_part_payment=125, outstanding_amount=1000, grand_total=1000,
		)
		self.assertEqual(terminal._amount_for(doc), 125.0)
		self.assertEqual(plural._amount_for(doc), 125.0)


class TestTransactionReferenceField(unittest.TestCase):
	"""The Payable Documents 'Transaction Reference Field' must be written on
	EVERY finalization path.

	It used to be written only inside the (now removed) status-mapping path,
	so with 'Create Payment Entry' ticked — the common case — a configured
	reference field was silently never filled.
	"""

	# A real, writable field on Sales Invoice that isn't used by these tests.
	REF_FIELD = "remarks"

	def setUp(self):
		self.invoice = _make_test_invoice()
		_ensure_default_machine()
		_set_settings(enabled=1, enable_payment_links=1)
		# mark_success calls frappe.db.commit() (the customer is already
		# charged, so the PE must be durable) — which also commits whatever
		# this test did to the Pinelabs Settings single. tearDown's rollback
		# cannot undo that, so snapshot the field we mutate and put it back
		# explicitly. Without this the suite leaves a real reference field
		# configured on the site and live payments start writing to it.
		self._original_ref_field = frappe.db.get_value(
			"Pinelabs Payable Doctype",
			{"parent": "Pinelabs Settings", "doctype_name": "Sales Invoice"},
			"link_payment_entry_field",
		)

	def tearDown(self):
		frappe.db.rollback()
		self._restore_ref_field()
		frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")

	def _restore_ref_field(self):
		current = frappe.db.get_value(
			"Pinelabs Payable Doctype",
			{"parent": "Pinelabs Settings", "doctype_name": "Sales Invoice"},
			"link_payment_entry_field",
		)
		if current == self._original_ref_field:
			return
		_set_payable_row("Sales Invoice", link_payment_entry_field=self._original_ref_field)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit

	def _make_pending_txn(self, create_payment_entry=None):
		from pinelabs_fin.api import transaction as txn_service

		txn = txn_service.create_transaction(
			reference_doctype="Sales Invoice",
			reference_name=self.invoice.name,
			flow_type="Terminal",
			amount=100,
			payment_method="CARD",
			create_payment_entry=create_payment_entry,
		)
		return txn_service.mark_pending(txn, order_id="ORD-REF-TEST")

	def _ref_value(self):
		return frappe.db.get_value("Sales Invoice", self.invoice.name, self.REF_FIELD)

	def test_reference_written_on_payment_entry_path(self):
		"""The regression: PE ticked used to skip the reference write entirely."""
		from pinelabs_fin.api import transaction as txn_service

		_set_payable_row(
			"Sales Invoice", is_enabled=1,
			finalize_via_payment_entry=1,
			link_payment_entry_field=self.REF_FIELD,
		)
		txn = self._make_pending_txn()
		txn = txn_service.mark_success(txn, payment_id="PAY-REF-PE", payment_method="CARD")

		self.assertEqual(txn.status, "SUCCESS")
		self.assertTrue(txn.payment_entry, "a Payment Entry should have been created")
		self.assertEqual(self._ref_value(), txn.name)

	def test_reference_written_on_no_payment_entry_path(self):
		from pinelabs_fin.api import transaction as txn_service

		_set_payable_row(
			"Sales Invoice", is_enabled=1,
			finalize_via_payment_entry=1,
			link_payment_entry_field=self.REF_FIELD,
		)
		# Per-call override wins over the row's setting.
		txn = self._make_pending_txn(create_payment_entry=0)
		txn = txn_service.mark_success(txn, payment_id="PAY-REF-NOPE", payment_method="CARD")

		self.assertEqual(txn.status, "SUCCESS")
		self.assertFalse(txn.payment_entry, "no Payment Entry should have been created")
		self.assertEqual(self._ref_value(), txn.name)

	def test_no_reference_field_configured_is_a_no_op(self):
		from pinelabs_fin.api import transaction as txn_service

		_set_payable_row(
			"Sales Invoice", is_enabled=1,
			finalize_via_payment_entry=1,
			link_payment_entry_field=None,
		)
		before = self._ref_value()
		txn = self._make_pending_txn()
		txn = txn_service.mark_success(txn, payment_id="PAY-REF-NONE", payment_method="CARD")

		self.assertEqual(txn.status, "SUCCESS")
		self.assertEqual(self._ref_value(), before)

	def test_bad_field_name_does_not_break_success(self):
		"""The customer has already been charged — a typo in config must not
		roll back the SUCCESS or the Payment Entry."""
		from pinelabs_fin.api import transaction as txn_service

		_set_payable_row(
			"Sales Invoice", is_enabled=1,
			finalize_via_payment_entry=1,
			link_payment_entry_field="no_such_field_xyz",
		)
		txn = self._make_pending_txn()
		txn = txn_service.mark_success(txn, payment_id="PAY-REF-BAD", payment_method="CARD")

		self.assertEqual(txn.status, "SUCCESS")
		self.assertTrue(txn.payment_entry)

	def test_mapped_finalization_is_gone(self):
		"""The status-mapping path and its config keys no longer exist."""
		from pinelabs_fin.api import transaction as txn_service

		self.assertFalse(hasattr(txn_service, "_apply_mapped_finalization"))
		config = txn_service._get_payable_config("Sales Invoice")
		self.assertNotIn("status_field", config)
		self.assertNotIn("paid_status_value", config)
		self.assertIn("link_payment_entry_field", config)

	def test_unticking_create_pe_needs_no_extra_config(self):
		"""Settings used to refuse to save unless status_field + paid_status_value
		were filled whenever Create Payment Entry was unticked."""
		_set_payable_row(
			"Sales Invoice", is_enabled=1,
			finalize_via_payment_entry=0,
			link_payment_entry_field=None,
		)
		settings = frappe.get_single("Pinelabs Settings")
		row = next(r for r in settings.payable_doctypes if r.doctype_name == "Sales Invoice")
		self.assertEqual(cint(row.finalize_via_payment_entry), 0)


class TestAutoCancelResolution(unittest.TestCase):
	"""The gateway timeout and the app-side age-out must never contradict.

	Regression cover for three defects: the app-side lookup queried the
	Pinelabs Machine Config child table by ``name`` (a random hash) using
	``machine_name``, so it never matched; the app-side value never read the
	Settings field at all; and three different hardcoded fallbacks (1 / 3 / 5)
	were in play for one concept.
	"""

	MACHINE = "Test Machine"

	def setUp(self):
		_ensure_default_machine()
		_ensure_named_machine(self.MACHINE)

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")

	@staticmethod
	def _set_machine_override(minutes):
		settings = frappe.get_single("Pinelabs Settings")
		for m in settings.machines:
			if m.machine_name == TestAutoCancelResolution.MACHINE:
				m.auto_cancel_duration_in_minutes = minutes
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")

	class _Txn:
		def __init__(self, machine):
			self.machine = machine

	def test_default_when_nothing_configured(self):
		from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
			DEFAULT_AUTO_CANCEL_MINUTES,
			resolve_auto_cancel_minutes,
		)

		self._set_machine_override(0)
		_set_settings(auto_cancel_duration_minutes=0)
		self.assertEqual(resolve_auto_cancel_minutes(self.MACHINE), DEFAULT_AUTO_CANCEL_MINUTES)

	def test_settings_value_is_used(self):
		from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
			resolve_auto_cancel_minutes,
		)

		self._set_machine_override(0)
		_set_settings(auto_cancel_duration_minutes=20)
		self.assertEqual(resolve_auto_cancel_minutes(self.MACHINE), 20)

	def test_machine_override_beats_settings(self):
		from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
			resolve_auto_cancel_minutes,
		)

		_set_settings(auto_cancel_duration_minutes=20)
		self._set_machine_override(7)
		self.assertEqual(resolve_auto_cancel_minutes(self.MACHINE), 7)

	def test_machine_resolved_by_machine_name_not_row_name(self):
		"""The core bug: Pinelabs Machine Config is a child table, so its
		`name` is a hash. Looking it up by machine_name always returned None."""
		from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
			resolve_auto_cancel_minutes,
		)

		self._set_machine_override(9)
		# The old lookup — by `name` — still finds nothing, proving the row is
		# not addressable that way and the new resolution is doing real work.
		self.assertIsNone(
			frappe.db.get_value(
				"Pinelabs Machine Config", self.MACHINE, "auto_cancel_duration_in_minutes"
			)
		)
		self.assertEqual(resolve_auto_cancel_minutes(self.MACHINE), 9)

	def test_app_cutoff_always_exceeds_gateway_timeout(self):
		"""The invariant: the app must never give up before the terminal does.
		Checked across the full range Pine Labs uses in its own examples."""
		from pinelabs_fin.api import terminal
		from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
			resolve_auto_cancel_minutes,
		)

		for configured in (2, 5, 10, 20, 100, 120):
			self._set_machine_override(configured)
			gateway = resolve_auto_cancel_minutes(self.MACHINE)
			app = terminal._auto_cancel_minutes(self._Txn(self.MACHINE))
			self.assertEqual(gateway, configured)
			self.assertGreater(
				app, gateway,
				f"app cutoff {app} must exceed gateway timeout {gateway}",
			)

	def test_client_and_age_out_read_the_same_number(self):
		"""Both timers derive from one value — they cannot drift apart."""
		from pinelabs_fin.api import terminal
		from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

		self._set_machine_override(11)
		client = get_pinelabs_client(machine_name=self.MACHINE)
		self.assertEqual(client.auto_cancel_duration, 11)
		self.assertEqual(
			terminal._auto_cancel_minutes(self._Txn(self.MACHINE)),
			11 + terminal.AGE_OUT_GRACE_MINUTES,
		)

	def test_unknown_machine_falls_back_to_settings(self):
		from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
			resolve_auto_cancel_minutes,
		)

		_set_settings(auto_cancel_duration_minutes=15)
		self.assertEqual(resolve_auto_cancel_minutes("No Such Machine"), 15)
		self.assertEqual(resolve_auto_cancel_minutes(None), 15)


class TestSecretRedaction(unittest.TestCase):
	"""Gateway credentials must never reach a log file in cleartext."""

	def test_masks_secret_keys_at_any_depth(self):
		from pinelabs_fin.api.redact import MASK, redact

		payload = {
			"MerchantID": "1234",
			"SecurityToken": "70D7509C-0A90-4938-A7F9-DB99B9B841D9",
			"nested": {"client_secret": "sh-hh", "Authorization": "Bearer abc.def"},
			"rows": [{"access_token": "tok"}, {"Amount": 15900}],
		}
		out = redact(payload)

		self.assertEqual(out["MerchantID"], "1234")          # non-secrets survive
		self.assertEqual(out["rows"][1]["Amount"], 15900)
		self.assertEqual(out["SecurityToken"], MASK)
		self.assertEqual(out["nested"]["client_secret"], MASK)
		self.assertEqual(out["nested"]["Authorization"], MASK)
		self.assertEqual(out["rows"][0]["access_token"], MASK)

	def test_key_spelling_variants_are_caught(self):
		from pinelabs_fin.api.redact import MASK, redact

		for key in ("client_secret", "clientSecret", "Client-Secret", "SECURITYTOKEN", "x-verify"):
			self.assertEqual(redact({key: "live"})[key], MASK, key)

	def test_input_is_not_mutated(self):
		from pinelabs_fin.api.redact import redact

		original = {"SecurityToken": "live-token"}
		redact(original)
		self.assertEqual(original["SecurityToken"], "live-token")

	def test_upload_payload_log_line_carries_no_token(self):
		"""End-to-end: the actual logged string for a real upload payload."""
		import json as _json

		from pinelabs_fin.api.redact import redact

		_ensure_default_machine()
		_ensure_named_machine("Test Machine")
		from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

		client = get_pinelabs_client(machine_name="Test Machine")
		payload = client._build_transaction_payload(
			transaction_ref="PLT-TEST-1", amount=100, payment_mode="Card"
		)
		self.assertEqual(payload["SecurityToken"], client.security_token)
		logged = _json.dumps(redact(payload), indent=2)
		self.assertNotIn(str(client.security_token), logged)
		self.assertIn("REDACTED", logged)


def _ensure_named_machine(machine_name):
	"""Append an enabled, NON-default machine row if it isn't there already.

	_ensure_default_machine() only seeds a machine when the table is empty, so
	on a site that already has one (every real site) it is a no-op. Tests that
	need to address a machine by name have to add their own. is_default stays
	0 so the "exactly one default machine" validation keeps passing.
	"""
	settings = frappe.get_single("Pinelabs Settings")
	if not any(m.machine_name == machine_name for m in (settings.machines or [])):
		settings.append("machines", {
			"machine_name": machine_name,
			"merchant_id": "MID-TEST",
			"client_id": "CID-TEST",
			"security_token": "ST-TEST-SECRET",
			"store_id": "STORE-1",
			"enabled": 1,
			"is_default": 0,
		})
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)
	frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")


class TestSecurityHardening(unittest.TestCase):
	"""Regression tests for the security review fixes."""

	def setUp(self):
		self.invoice = _make_test_invoice()  # 100.00 outstanding
		_ensure_default_machine()
		_set_settings(enabled=1, enable_payment_links=1)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")

	def _powerless_user(self):
		email = "pinelabs_noperm@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc({
				"doctype": "User", "email": email, "first_name": "NoPerm",
				"send_welcome_email": 0, "roles": [],
			}).insert(ignore_permissions=True)
		return email

	@staticmethod
	def _accepted(ref):
		return {"success": True, "data": {"ResponseCode": 0, "ResponseMessage": "ok", "PlutusTransactionReferenceID": ref}}

	def _initiate(self, **kwargs):
		from pinelabs_fin.api import terminal

		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.upload_billed_raw",
			return_value=self._accepted(int(frappe.generate_hash(length=10), 16) % (10 ** 8)),
		) as upload:
			result = terminal.initiate_terminal_payment(
				reference_doctype="Sales Invoice",
				reference_name=self.invoice.name,
				mode_of_payment="PineLabs - Card",
				**kwargs,
			)
		return result, upload

	# ---- amount cap -------------------------------------------------------

	def test_amount_above_outstanding_is_refused_before_the_gateway(self):
		with patch(
			"pinelabs_fin.api.pinelabs_client.PineLabsClient.upload_billed_raw"
		) as upload:
			from pinelabs_fin.api import terminal

			with self.assertRaises(frappe.ValidationError) as ctx:
				terminal.initiate_terminal_payment(
					"Sales Invoice", self.invoice.name, "PineLabs - Card", amount=1000
				)
		self.assertIn("more than", str(ctx.exception))
		upload.assert_not_called()

	def test_zero_and_negative_amount_refused(self):
		from pinelabs_fin.api import terminal

		for bad in (-50, 0.001):
			with self.assertRaises(frappe.ValidationError):
				terminal.initiate_terminal_payment(
					"Sales Invoice", self.invoice.name, "PineLabs - Card", amount=bad
				)

	def test_part_payment_below_outstanding_is_allowed(self):
		result, _ = self._initiate(amount=40)
		self.assertTrue(result["success"])
		self.assertEqual(
			frappe.db.get_value("Pinelabs Transaction", result["transaction_name"], "amount"), 40
		)

	# ---- retry ------------------------------------------------------------

	def test_retry_refused_unless_failed_or_cancelled(self):
		from pinelabs_fin.api import terminal

		result, _ = self._initiate()
		with self.assertRaises(frappe.ValidationError) as ctx:  # PENDING
			terminal.retry_failed_terminal_payment(result["transaction_name"])
		self.assertIn("Only a failed or cancelled", str(ctx.exception))

	# ---- permissions ------------------------------------------------------

	def test_missing_and_forbidden_documents_are_indistinguishable(self):
		from pinelabs_fin.api import terminal

		frappe.set_user(self._powerless_user())
		messages = []
		for name in (self.invoice.name, "ACC-SINV-DOES-NOT-EXIST"):
			with self.assertRaises(frappe.PermissionError) as ctx:
				terminal.initiate_terminal_payment("Sales Invoice", name, "PineLabs - Card")
			messages.append(str(ctx.exception).replace(name, "<doc>"))
		self.assertEqual(messages[0], messages[1])

	def test_unknown_transaction_and_forbidden_transaction_look_alike(self):
		from pinelabs_fin.api import terminal
		from pinelabs_fin.api import transaction as txn_service

		txn = txn_service.create_transaction(
			reference_doctype="Sales Invoice", reference_name=self.invoice.name,
			flow_type="Terminal", amount=100, payment_method="CARD",
		)
		frappe.set_user(self._powerless_user())
		seen = []
		for name in (txn.name, "PLT-1999-99999"):
			with self.assertRaises(frappe.PermissionError) as ctx:
				terminal.check_terminal_status(name)
			seen.append(str(ctx.exception).replace(name, "<txn>"))
		self.assertEqual(seen[0], seen[1])

	def test_settings_metadata_endpoints_are_system_manager_only(self):
		from pinelabs_fin.api import customer_contact

		frappe.set_user(self._powerless_user())
		with self.assertRaises(frappe.PermissionError):
			customer_contact.list_doctype_fields("User")
		with self.assertRaises(frappe.PermissionError):
			customer_contact.get_link_field_target("User", "role_profile_name")

	def test_button_config_endpoint_works_without_settings_access(self):
		"""Cashiers cannot read Pinelabs Settings, but must still get buttons."""
		from pinelabs_fin.api import access

		frappe.set_user(self._powerless_user())
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("Pinelabs Settings").check_permission("read")
		out = access.get_payable_doctypes()
		self.assertIn("payable_doctypes", out)
		for row in out["payable_doctypes"]:  # flags only, nothing sensitive
			self.assertLessEqual(
				set(row),
				{"doctype_name", "is_enabled", "enable_pay_on_machine", "enable_send_payment_link", "show_on_docstatus"},
			)

	def test_payment_endpoints_accept_post_only(self):
		from pinelabs_fin.api import access, config, plural, terminal, webhook

		for fn in (
			terminal.initiate_terminal_payment, terminal.poll_terminal_status,
			terminal.check_terminal_status, terminal.retry_failed_terminal_payment,
			terminal.cancel_terminal_payment, plural.initiate_payment_link,
			plural.refresh_payment_link_status, config.test_plural_connection,
			webhook.handle_webhook, access.get_payable_doctypes,
		):
			self.assertEqual(
				frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"], fn.__name__
			)

	# ---- transaction integrity -------------------------------------------

	def test_service_owned_fields_cannot_be_edited_by_hand(self):
		result, _ = self._initiate()
		txn = frappe.get_doc("Pinelabs Transaction", result["transaction_name"])
		for field, value in (("amount", 99999), ("payment_id", "someone-elses"), ("reference_name", "OTHER")):
			doc = frappe.get_doc("Pinelabs Transaction", txn.name)
			doc.set(field, value)
			with self.assertRaises(frappe.ValidationError, msg=field):
				doc.save(ignore_permissions=True)

	def test_lock_and_reload_returns_the_latest_row(self):
		"""A stale in-memory copy must not be trusted: the SUCCESS written by a
		competing worker has to win, and no second Payment Entry may be built."""
		from pinelabs_fin.api import transaction as txn_service

		result, _ = self._initiate()
		stale = frappe.get_doc("Pinelabs Transaction", result["transaction_name"])
		self.assertEqual(stale.status, "PENDING")
		frappe.db.set_value("Pinelabs Transaction", stale.name, "status", "SUCCESS")

		fresh = txn_service.mark_success(stale, payment_id="whatever")
		self.assertEqual(fresh.status, "SUCCESS")
		self.assertFalse(fresh.payment_entry)  # idempotent return: nothing built

	# ---- stored payloads --------------------------------------------------

	def test_stored_payloads_never_contain_the_security_token(self):
		from pinelabs_fin.api.transaction import _to_json_str

		token = "70D7509C-0A90-4938-A7F9-DB99B9B841D9"
		for value in (
			{"SecurityToken": token, "Amount": 100},
			json.dumps({"request_payload": {"SecurityToken": token}}),
			{"success": False, "request_payload": {"SecurityToken": token}},
		):
			stored = _to_json_str(value)
			self.assertNotIn(token, stored)
			self.assertIn("REDACTED", stored)
		self.assertIn("100", _to_json_str({"SecurityToken": token, "Amount": 100}))

	def test_plural_header_secret_is_redacted(self):
		from pinelabs_fin.api.redact import MASK, redact

		out = redact({"x-plural-client-secret": "s3", "x-plural-client-id": "cid", "Authorization": "Bearer t"})
		self.assertEqual(out["x-plural-client-secret"], MASK)
		self.assertEqual(out["Authorization"], MASK)
		self.assertEqual(out["x-plural-client-id"], "cid")

	def test_customer_contact_masked_only_when_asked(self):
		from pinelabs_fin.api.redact import MASK, redact

		body = {"customer": {"email_id": "a@b.com", "mobile_number": "9999999999"}}
		self.assertEqual(redact(body)["customer"]["email_id"], "a@b.com")
		self.assertEqual(redact(body, pii=True)["customer"]["email_id"], MASK)
		self.assertEqual(redact(body, pii=True)["customer"]["mobile_number"], MASK)


class TestGatewayUrlGuard(unittest.TestCase):
	def test_rules(self):
		from pinelabs_fin.api.url_guard import assert_gateway_url

		# allowed
		assert_gateway_url("https://api.pluralpay.in", "u")
		assert_gateway_url("http://127.0.0.1:5000", "u", verify_ssl=False)  # local simulator
		assert_gateway_url("http://localhost:5000", "u")
		# refused
		for url, verify in (
			("http://evil.example.com", True),          # plain http, remote
			("https://evil.example.com", False),        # TLS verification off, remote
			("https://user:pw@api.pluralpay.in", True),  # credentials in URL
			("ftp://api.pluralpay.in", True),
			("not a url", True),
			("", True),
		):
			with self.assertRaises(frappe.ValidationError, msg=url):
				assert_gateway_url(url, "u", verify_ssl=verify)

	def test_settings_refuse_an_insecure_endpoint_row(self):
		settings = frappe.get_single("Pinelabs Settings")
		settings.append("api_endpoints", {
			"endpoints_name": "bad", "base_api_url": "http://evil.example.com",
			"upload_transaction_endpoint": "/u", "get_status_endpoint": "/s",
			"cancel_transaction_endpoint": "/c", "verify_ssl": 1,
		})
		settings.flags.ignore_mandatory = True
		try:
			with self.assertRaises(frappe.ValidationError):
				settings.save(ignore_permissions=True)
		finally:
			frappe.db.rollback()
			frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")


class TestWebhookHardening(unittest.TestCase):
	SECRET = "test-secret-1234567890abcdef"

	def setUp(self):
		self.invoice = _make_test_invoice()
		settings = frappe.get_single("Pinelabs Settings")
		settings.enabled = 1
		settings.enable_payment_links = 1
		settings.payment_link_webhook_secret = self.SECRET
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()
		frappe.local.request = None
		frappe.clear_document_cache("Pinelabs Settings", "Pinelabs Settings")

	def _post(self, body, signature=None):
		raw = json.dumps(body)
		if signature is None:
			signature = hmac.new(self.SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()

		class _Req:
			method = "POST"
			headers = {"X-Verify": signature}

			def get_json(self_inner):
				return json.loads(raw)

			def get_data(self_inner, as_text=False):
				return raw if as_text else raw.encode("utf-8")

		frappe.local.request = _Req()
		from pinelabs_fin.api import webhook

		return webhook.handle_webhook()

	def _plural_txn(self, amount=100):
		from pinelabs_fin.api import transaction as txn_service

		txn = txn_service.create_transaction(
			reference_doctype="Sales Invoice", reference_name=self.invoice.name,
			flow_type="Plural", amount=amount,
		)
		return txn_service.mark_pending(txn, order_id="v1-pl-" + frappe.generate_hash(length=10))

	def test_matches_on_our_merchant_reference_when_order_id_differs(self):
		"""Plural's ``order_id`` is its own order id, not the payment-link id we
		stored — the PLT- reference in the payload must still find the row."""
		from pinelabs_fin.api import webhook

		txn = self._plural_txn()
		payload = {"order_id": "v1-SIM-O-unrelated", "merchant_order_reference": txn.name}
		self.assertEqual(webhook._find_transaction(webhook._candidate_ids(payload)), txn.name)

	def test_candidate_ids_prefers_our_reference_and_dedupes(self):
		from pinelabs_fin.api import webhook

		ids = webhook._candidate_ids({
			"order_id": "o1", "merchant_order_reference": "PLT-1", "payment_link_id": "o1",
		})
		self.assertEqual(ids, ["PLT-1", "o1"])

	def test_non_ascii_signature_is_a_clean_401_not_a_crash(self):
		result = self._post({"event_type": "X", "data": {"order_id": "y"}}, signature="é" * 64)
		self.assertFalse(result["success"])
		self.assertIn("signature", result["message"].lower())

	def test_amount_mismatch_is_not_booked(self):
		from pinelabs_fin.api import webhook

		txn = self._plural_txn(amount=100)
		self.assertTrue(webhook._amount_matches({"order_amount": {"value": 10000, "currency": "INR"}}, txn))
		self.assertFalse(webhook._amount_matches({"order_amount": {"value": 1, "currency": "INR"}}, txn))
		self.assertFalse(webhook._amount_matches({"order_amount": {"value": 10000, "currency": "USD"}}, txn))
		self.assertTrue(webhook._amount_matches({}, txn))  # no amount stated -> cannot compare

		result = self._post({
			"event_type": "ORDER_PROCESSED",
			"data": {"merchant_order_reference": txn.name, "status": "PROCESSED",
					"order_amount": {"value": 1, "currency": "INR"}},
		})
		self.assertFalse(result["success"])
		self.assertEqual(frappe.db.get_value("Pinelabs Transaction", txn.name, "status"), "PENDING")

	def test_declined_attempt_leaves_a_payment_link_open(self):
		txn = self._plural_txn()
		result = self._post({
			"event_type": "PAYMENT_FAILED",
			"data": {"merchant_order_reference": txn.name, "status": "FAILED"},
		})
		self.assertTrue(result["success"])
		self.assertEqual(frappe.db.get_value("Pinelabs Transaction", txn.name, "status"), "PENDING")

	def test_unmatched_event_stays_retryable(self):
		result = self._post({"event_type": "ORDER_PROCESSED", "data": {"order_id": "no-such", "event_id": "ev-unmatched-1"}})
		self.assertFalse(result["success"])
		self.assertEqual(
			frappe.db.get_value("Pinelabs Webhook Event", {"event_id": "ev-unmatched-1"}, "status"), "failed"
		)
