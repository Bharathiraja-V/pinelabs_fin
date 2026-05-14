# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Core state machine for Pinelabs Transaction.

This module is the *only* place that writes Pinelabs Transaction rows
or creates Payment Entries. terminal.py / plural.py / webhook.py all
delegate to the helpers here so the state-transition rules and
Payment-Entry guard rails stay in one place.

State machine (enforced by both this module and the doctype controller):

    INITIATED ──▶ PENDING ──▶ SUCCESS
        │            │
        │            ├──▶ FAILED
        │            └──▶ CANCELLED
        │
        ├──▶ SUCCESS    (Terminal: response → success without PENDING)
        ├──▶ FAILED
        └──▶ CANCELLED

Payment Entry creation rules (from PHASE_1_PLAN §8):
  1. Created only from mark_success() in this module.
  2. Skipped if a PE with the same reference_no already exists.
  3. Skipped if the transaction already has a payment_entry linked.
  4. Built via erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry.
  5. mode_of_payment / reference_no / reference_date / remarks set explicitly.
  6. PE name is written back to the transaction; status flipped to SUCCESS.
  7. Wrapped in one DB transaction; rollback on any error.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

VALID_FLOW_TYPES = ("Terminal", "Plural")
ACTIVE_STATUSES = ("INITIATED", "PENDING")

# ERPNext's get_payment_entry helper natively supports these three. For any
# other doctype, mark_success will throw at PE creation time — see
# _create_payment_entry_idempotent. The validator at the bottom of this
# module checks this BEFORE the customer is asked to pay.
PE_NATIVE_DOCTYPES = ("Sales Invoice", "POS Invoice", "Payment Request")


def validate_finalization_ready(reference_doctype):
	"""Refuse to start a payment when the app cannot finish it CLEANLY.

	Three valid configurations:
	1. reference_doctype is one of PE_NATIVE_DOCTYPES → ERPNext's
	   ``get_payment_entry`` will build the PE on success. No setup needed.
	2. A Payable Documents row exists with finalize_via_payment_entry = 0
	   AND status_field AND paid_status_value populated → mark_success will
	   write to the configured field on the source doc.
	3. No Payable Documents row at all → permissive: the payment will
	   proceed and the Pinelabs Transaction will record SUCCESS, but the
	   source doc is **not** auto-updated. The caller (custom button,
	   external system) is responsible for reacting to the
	   ``pinelabs_txn_update`` realtime event or polling the Pinelabs
	   Transaction status if they want the source doc to reflect payment.

	The only case that still throws is (4) — a misconfigured Payable
	Documents row (finalize_via_pe = 1 on a non-PE-native doctype, or
	missing required mapping fields). That's an admin-side error worth
	surfacing loudly before the customer is asked to pay.
	"""
	if reference_doctype in PE_NATIVE_DOCTYPES:
		return

	config = _get_payable_config(reference_doctype)
	if not config:
		# Permissive — see docstring case (3). Payment proceeds; source
		# doc is not auto-finalized.
		return

	if cint(config.get("finalize_via_payment_entry", 1)):
		frappe.throw(
			_(
				"Cannot accept Pine Labs payments on {0}: it is not Sales Invoice "
				"/ POS Invoice / Payment Request, but its Payable Documents row "
				"has 'Create Payment Entry' ticked. ERPNext's Payment Entry "
				"builder does not support {0}, so finalization would fail after "
				"the customer paid.\n\n"
				"Fix: open the row in Pinelabs Settings → Payable Documents, "
				"untick 'Create Payment Entry', and set 'Status Field' + 'Paid "
				"Status Value'. Or delete the row entirely to use the permissive "
				"path (Pinelabs Transaction records the payment; source doc is "
				"left untouched)."
			).format(reference_doctype),
		)

	status_field = (config.get("status_field") or "").strip()
	paid_value = (config.get("paid_status_value") or "").strip()
	if not (status_field and paid_value):
		frappe.throw(
			_(
				"Cannot accept Pine Labs payments on {0}: its Payable Documents "
				"row has 'Create Payment Entry' unticked but is missing 'Status "
				"Field' or 'Paid Status Value'. Fill both, or delete the row to "
				"use the permissive path."
			).format(reference_doctype),
		)


# ──────────────────────────────────────────────────────────────────────────
# Creation
# ──────────────────────────────────────────────────────────────────────────


def create_transaction(
	reference_doctype,
	reference_name,
	flow_type,
	amount,
	*,
	payment_method=None,
	machine=None,
	order_id=None,
	request_payload=None,
):
	"""Insert a new Pinelabs Transaction in INITIATED state.

	Caller must have already validated eligibility (docstatus, outstanding,
	etc.) — this function only enforces structural invariants.
	"""
	if flow_type not in VALID_FLOW_TYPES:
		frappe.throw(
			_("Invalid flow_type {0}. Expected one of {1}.").format(flow_type, ", ".join(VALID_FLOW_TYPES)),
		)
	if not (reference_doctype and reference_name):
		frappe.throw(_("reference_doctype and reference_name are required."))
	if flt(amount) <= 0:
		frappe.throw(_("Transaction amount must be greater than zero."))

	doc = frappe.new_doc("Pinelabs Transaction")
	doc.reference_doctype = reference_doctype
	doc.reference_name = reference_name
	doc.flow_type = flow_type
	doc.payment_method = payment_method
	doc.amount = flt(amount)
	doc.machine = machine
	doc.order_id = order_id
	doc.created_at = now_datetime()
	doc.status = "INITIATED"
	if request_payload is not None:
		doc.request_payload = _to_json_str(request_payload)

	doc.insert(ignore_permissions=True)
	return doc


def get_active_for_doc(reference_doctype, reference_name):
	"""Return the open (INITIATED/PENDING) transaction for a source doc, or None.

	Used by the JS button-handlers to detect "already in flight" before
	starting a new attempt — prevents duplicate spends from rapid clicks.
	"""
	if not (reference_doctype and reference_name):
		return None
	name = frappe.db.get_value(
		"Pinelabs Transaction",
		{
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"status": ["in", ACTIVE_STATUSES],
		},
		"name",
		order_by="creation desc",
	)
	return frappe.get_doc("Pinelabs Transaction", name) if name else None


# ──────────────────────────────────────────────────────────────────────────
# Status transitions
# ──────────────────────────────────────────────────────────────────────────


def mark_pending(transaction, *, order_id=None, payment_id=None, response_payload=None):
	"""Move a transaction from INITIATED to PENDING.

	Called right after the gateway accepts the request but before the
	customer has actually paid. For the Terminal flow, callers must pass
	`payment_id` here (the Plutus reference) — `_reload` would otherwise
	drop any in-memory change set on the doc before this call.
	"""
	transaction = _lock_and_reload(transaction)
	if transaction.status == "PENDING":
		# Another worker raced ahead and already moved us here.
		return transaction
	if transaction.status != "INITIATED":
		frappe.throw(
			_("Cannot mark PENDING from status {0}.").format(transaction.status),
		)

	updates = {"status": "PENDING"}
	if order_id:
		updates["order_id"] = order_id
	if payment_id:
		updates["payment_id"] = payment_id
	if response_payload is not None:
		updates["response_payload"] = _to_json_str(response_payload)

	transaction.flags.pinelabs_internal_transition = True
	transaction.update(updates)
	transaction.save(ignore_permissions=True)
	_publish_status_update(transaction)
	return transaction


def mark_success(transaction, *, payment_id, payment_method=None, response_payload=None):
	"""Mark transaction SUCCESS and create the Payment Entry idempotently.

	This is the ONLY path that creates Payment Entries. Wraps the PE creation
	in a try/except so that a failure leaves the transaction in its pre-call
	state (PENDING or INITIATED) for retry.
	"""
	transaction = _lock_and_reload(transaction)
	if transaction.status == "SUCCESS":
		# Idempotent — caller is replaying a webhook or polling result.
		return transaction
	if transaction.status in ("FAILED", "CANCELLED"):
		frappe.throw(
			_("Cannot mark SUCCESS from terminal status {0}.").format(transaction.status),
		)
	if not payment_id:
		frappe.throw(_("payment_id is required to mark SUCCESS."))

	if payment_method:
		transaction.payment_method = payment_method
	transaction.payment_id = payment_id
	if response_payload is not None:
		transaction.response_payload = _to_json_str(response_payload)
	transaction.completed_at = now_datetime()

	mode_of_payment = _resolve_mode_of_payment(transaction)
	transaction.mode_of_payment = mode_of_payment

	# Wrap PE creation + status flip in a savepoint so partial failures
	# don't leave the transaction "successful but no PE".
	savepoint = "pinelabs_mark_success"
	frappe.db.savepoint(savepoint)
	try:
		pe_name = _create_payment_entry_idempotent(transaction)
		transaction.payment_entry = pe_name
		transaction.status = "SUCCESS"
		transaction.flags.pinelabs_internal_transition = True
		transaction.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			frappe.get_traceback(),
			f"Pinelabs mark_success failed for {transaction.name}",
		)
		raise

	_publish_status_update(transaction)
	return transaction


def mark_failed(transaction, *, error_message=None, response_payload=None):
	"""Move a transaction to FAILED. Idempotent."""
	transaction = _lock_and_reload(transaction)
	if transaction.status == "FAILED":
		return transaction
	if transaction.status in ("SUCCESS", "CANCELLED"):
		frappe.throw(
			_("Cannot mark FAILED from terminal status {0}.").format(transaction.status),
		)

	transaction.status = "FAILED"
	transaction.completed_at = now_datetime()
	if error_message:
		transaction.error_message = _truncate_with_ellipsis(error_message, 1000)
	if response_payload is not None:
		transaction.response_payload = _to_json_str(response_payload)
	transaction.flags.pinelabs_internal_transition = True
	transaction.save(ignore_permissions=True)
	_publish_status_update(transaction)
	return transaction


def mark_cancelled(transaction, *, reason=None):
	"""Move a transaction to CANCELLED. Idempotent."""
	transaction = _lock_and_reload(transaction)
	if transaction.status == "CANCELLED":
		return transaction
	if transaction.status in ("SUCCESS", "FAILED"):
		frappe.throw(
			_("Cannot mark CANCELLED from terminal status {0}.").format(transaction.status),
		)

	transaction.status = "CANCELLED"
	transaction.completed_at = now_datetime()
	if reason:
		transaction.error_message = _truncate_with_ellipsis(reason, 1000)
	transaction.flags.pinelabs_internal_transition = True
	transaction.save(ignore_permissions=True)
	_publish_status_update(transaction)
	return transaction


def _publish_status_update(transaction):
	"""Push a status change to the browser via Frappe's realtime channel.

	The Pay-on-Machine modal subscribes to `pinelabs_txn_update` events on
	the user's socket and reacts when the transaction it's watching reaches
	a terminal state. Server-side callers (the cron reconcile task, webhook,
	and the foreground request) all flow through this function so any state
	transition lands on every open dialog watching that transaction.

	`after_commit=True` means the event fires only after the DB commit, so
	the browser never sees a status the database hasn't durably persisted.
	"""
	try:
		frappe.publish_realtime(
			event="pinelabs_txn_update",
			message={
				"transaction_name": transaction.name,
				"reference_doctype": transaction.reference_doctype,
				"reference_name": transaction.reference_name,
				"status": transaction.status,
				"payment_id": transaction.payment_id,
				"payment_entry": transaction.payment_entry,
				"error_message": transaction.error_message,
			},
			user=transaction.owner,
			after_commit=True,
		)
	except Exception:
		# Realtime is best-effort. Browser polling fallback still reconciles.
		frappe.log_error(
			frappe.get_traceback(),
			f"Pinelabs realtime publish failed for {transaction.name}",
		)


# ──────────────────────────────────────────────────────────────────────────
# Mode of Payment lookup
# ──────────────────────────────────────────────────────────────────────────


def _resolve_mode_of_payment(transaction):
	"""Find the Mode of Payment that matches this transaction's flow + method.

	Looks up the custom fields seeded by install_pinelabs_modes.py:

	- For Terminal: ``pinelabs_terminal_code`` matches the integer Pine Labs
	  code we sent (1 = Card, 11 = UPI). Two modes exist (Card, UPI).
	- For Plural: a single ``PineLabs - Payment Link`` mode covers the whole
	  flow regardless of what the customer ended up paying with on the
	  hosted page. The customer's real method is still captured on
	  ``Pinelabs Transaction.payment_method`` and on the resulting
	  Payment Entry's ``reference_no`` (= Plural ``payment_id``).
	"""
	flow = transaction.flow_type
	method = (transaction.payment_method or "").upper()

	# Map the canonical Terminal method strings to the integer code Pine Labs uses.
	_TERMINAL_METHOD_TO_CODE = {"CARD": 1, "UPI": 11}

	if flow == "Terminal":
		filters = {
			"pinelabs_enabled": 1,
			"pinelabs_flow_type": ["in", ["Terminal", "Both"]],
		}
		if method:
			code = _TERMINAL_METHOD_TO_CODE.get(method)
			if code is None:
				frappe.throw(_("Unknown terminal payment_method {0}.").format(method))
			filters["pinelabs_terminal_code"] = code
		rows = frappe.get_all("Mode of Payment", filters=filters, fields=["name"], limit=1)
		if not rows:
			frappe.throw(
				_("No PineLabs Mode of Payment found for Terminal method {0}.").format(method or "?"),
			)
		return rows[0].name

	# Plural — a single mode covers all hosted-link payments.
	return get_plural_mode_of_payment()


def get_plural_mode_of_payment():
	"""Return the name of the single Mode of Payment used for every Plural payment.

	Centralised so the initiate-time account-mapping pre-flight and the
	finalize-time PE creation can't drift out of sync.
	"""
	rows = frappe.get_all(
		"Mode of Payment",
		filters={
			"pinelabs_enabled": 1,
			"pinelabs_flow_type": ["in", ["Plural", "Both"]],
		},
		fields=["name"],
		order_by="creation asc",
		limit=1,
	)
	if not rows:
		frappe.throw(
			_(
				"No PineLabs Mode of Payment is enabled for the Plural (Payment Link) "
				"flow. Run ``bench --site &lt;site&gt; migrate`` to seed the standard "
				"'PineLabs - Payment Link' mode, or enable a Plural-flow Mode of Payment "
				"manually."
			),
		)
	return rows[0].name


def will_create_payment_entry(reference_doctype):
	"""Return True iff the configured finalization path will produce a Payment Entry.

	Three "no PE" paths exist (see ``validate_finalization_ready``):

	1. The reference_doctype isn't PE-native AND has no Payable Documents
	   row (permissive — custom-button flow, source doc is not auto-finalized).
	2. The Payable Documents row has ``finalize_via_payment_entry = 0``
	   (mapped-field finalization writes a status field instead).
	3. (Implicitly) misconfigured rows — ``validate_finalization_ready``
	   throws on those before we ever get here.

	The account-mapping pre-flight only matters when a PE is going to be
	posted, so it should be gated on this.
	"""
	config = _get_payable_config(reference_doctype)
	if config and not cint(config.get("finalize_via_payment_entry", 1)):
		return False  # mapped-field finalization
	if reference_doctype in PE_NATIVE_DOCTYPES:
		return True
	if not config:
		return False  # permissive — custom button + no PE
	# Non-PE-native doctype with finalize_via_pe=1 — validate_finalization_ready
	# refuses this combination, so we never actually finalize via PE here.
	return True


def assert_mode_has_account_mapped(mode_of_payment, company, *, reference_doctype=None):
	"""Throw if ``Mode of Payment → Accounts`` has no row for this company.

	ERPNext's ``get_payment_entry`` won't be able to post the credit leg of
	the PE without the account mapping, but the failure surfaces deep inside
	``mark_success`` — long after the customer has paid. Call this BEFORE
	the request hits Pine Labs / Plural so the operator gets a clean,
	actionable error and the customer is never charged for a payment we
	can't finalize.

	The check is skipped when the configured finalization path will NOT
	create a Payment Entry (mapped-field finalization, or the permissive
	custom-button path on a non-PE-native doctype). In those cases the
	Mode of Payment is informational only — no account is posted to.
	"""
	if not (mode_of_payment and company):
		# Caller didn't provide enough context to check — better to skip
		# than to throw a misleading error. The finalize-time path will
		# still surface any real misconfig.
		return

	if reference_doctype is not None and not will_create_payment_entry(reference_doctype):
		# Mapped-field or permissive path — no PE will be posted, so the
		# Mode of Payment's account doesn't need to be configured.
		return

	account = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mode_of_payment, "company": company},
		"default_account",
	)
	if not account:
		frappe.throw(
			_(
				"Mode of Payment <b>{0}</b> has no default account configured for "
				"<b>{1}</b>. Open Mode of Payment → {0} → Accounts and add a row "
				"with Company = {1} and a Default Account before taking this payment."
			).format(mode_of_payment, company),
		)


# ──────────────────────────────────────────────────────────────────────────
# Payment Entry creation
# ──────────────────────────────────────────────────────────────────────────


def _create_payment_entry_idempotent(transaction):
	"""Finalize the transaction. Returns the PE name when one is created, or
	None when the configured doctype uses mapped-field finalization instead.

	Idempotent against:
	- A pre-existing PE with the same reference_no (Plan §8 rule 2).
	- The transaction already having a linked PE (Plan §8 rule 3).
	"""
	# Rule 3: transaction already linked → return that linked PE.
	if transaction.payment_entry:
		return transaction.payment_entry

	# Per-doctype config decides PE vs mapped-field finalization.
	config = _get_payable_config(transaction.reference_doctype)
	finalize_via_pe = cint((config or {}).get("finalize_via_payment_entry", 1))

	if not finalize_via_pe:
		_apply_mapped_finalization(transaction, config)
		return None

	# Rule 2: PE with same reference_no exists → reuse it.
	# Restrict to non-cancelled PEs (docstatus 0 or 1) so a previously
	# cancelled PE pointing at the same Pine Labs payment doesn't block
	# a legitimate retry.
	existing = frappe.db.get_value(
		"Payment Entry",
		{
			"reference_no": transaction.payment_id,
			"docstatus": ["<", 2],
		},
		"name",
	)
	if existing:
		return existing

	# Rule 4: standard ERPNext PE builder.
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	if transaction.reference_doctype not in PE_NATIVE_DOCTYPES:
		# Two sub-cases here:
		# 1. No Payable Documents row → permissive path. The caller chose
		#    to skip configuration; mark the Pinelabs Transaction as
		#    SUCCESS without touching the source doc. They can react to
		#    the pinelabs_txn_update realtime event if they want their
		#    source doc updated.
		# 2. Row exists with finalize_via_payment_entry = 1 (misconfig).
		#    validate_finalization_ready should have caught this at click
		#    time. Reaching here means a code path bypassed it (e.g. a
		#    direct test fixture). Throw with the actionable message.
		if not config:
			return None
		frappe.throw(
			_("PE creation supports Sales Invoice / POS Invoice / Payment Request only. Got {0}. Either add support or set Create Payment Entry to 0 on this doctype's Payable Doctypes row.").format(
				transaction.reference_doctype
			),
		)

	# Conflict guard — source doc already fully paid by something else?
	#
	# If a cashier (or any other path) recorded a Payment Entry manually
	# while our Pine Labs payment was in flight, the source doc's
	# Outstanding will already be 0 by the time we get here. ERPNext's
	# get_payment_entry would throw in that case ("nothing to pay"),
	# the savepoint in mark_success would roll back, and the Pinelabs
	# Transaction would stay PENDING — with the money already debited
	# from the customer on the Pine Labs side.
	#
	# Instead, we detect the conflict, write a clear note onto the
	# transaction, and return None. mark_success will then complete the
	# transaction as SUCCESS (because the customer DID pay) but leave
	# payment_entry empty and error_message populated. The operator
	# sees the conflict on the Pinelabs Transaction record and can do
	# manual reconciliation.
	source = frappe.get_doc(transaction.reference_doctype, transaction.reference_name)
	outstanding = flt(getattr(source, "outstanding_amount", 0) or 0)
	if outstanding <= 0:
		conflict_msg = (
			f"Customer paid {flt(transaction.amount):.2f} via Pine Labs "
			f"(payment_id {transaction.payment_id}), but {transaction.reference_doctype} "
			f"{transaction.reference_name} was already fully paid by another "
			f"Payment Entry. Manual reconciliation required — either refund the "
			f"Pine Labs charge or cancel the conflicting Payment Entry and re-run "
			f"Refresh Status on this transaction."
		)
		transaction.error_message = _truncate_with_ellipsis(conflict_msg, 1000)
		frappe.log_error(conflict_msg, "Pinelabs PE conflict")
		return None

	pe = get_payment_entry(transaction.reference_doctype, transaction.reference_name)

	# Rule 5: explicit fields.
	pe.mode_of_payment = transaction.mode_of_payment
	pe.paid_amount = flt(transaction.amount)
	pe.received_amount = flt(transaction.amount)
	pe.reference_no = transaction.payment_id
	pe.reference_date = (transaction.completed_at or now_datetime())
	pe.remarks = (
		f"Pine Labs {transaction.flow_type} payment "
		f"({transaction.payment_method or 'unknown method'}) "
		f"for {transaction.reference_doctype} {transaction.reference_name}. "
		f"Pinelabs Transaction: {transaction.name}."
	)

	# Bypass flag for conflict_guard.refuse_if_pinelabs_active — without
	# this our own PE insertion would trip the validate hook because
	# THIS Pinelabs Transaction is still in PENDING at this exact moment.
	pe.flags.pinelabs_internal_pe = True
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	return pe.name


def _get_payable_config(reference_doctype):
	"""Return the Payable Doctype config dict for the given source doctype, or None."""
	if not reference_doctype:
		return None
	try:
		settings = frappe.get_single("Pinelabs Settings")
	except Exception:
		return None
	for row in (settings.payable_doctypes or []):
		if row.doctype_name != reference_doctype:
			continue
		return {
			"doctype_name": row.doctype_name,
			"is_enabled": cint(row.is_enabled),
			"show_on_docstatus": row.show_on_docstatus,
			"amount_field": row.amount_field,
			"status_field": row.status_field,
			"paid_status_value": row.paid_status_value,
			"link_payment_entry_field": row.link_payment_entry_field,
			"finalize_via_payment_entry": cint(getattr(row, "finalize_via_payment_entry", 1) or 0),
		}
	return None


def _apply_mapped_finalization(transaction, config):
	"""Update the source document's status field instead of creating a PE.

	- Set status_field = paid_status_value
	- If link_payment_entry_field is set, write the Pinelabs Transaction name there
	- If paid_status_value is "Submitted" and the doctype is submittable + draft,
	  also submit the document.
	"""
	if not config:
		frappe.throw(_("Payable Doctype config missing for {0}.").format(transaction.reference_doctype))

	status_field = (config.get("status_field") or "").strip()
	paid_value = (config.get("paid_status_value") or "").strip()
	link_field = (config.get("link_payment_entry_field") or "").strip()

	if not (status_field and paid_value):
		frappe.throw(
			_("Mapped finalization for {0} requires status_field and paid_status_value to be configured.").format(
				transaction.reference_doctype
			),
		)

	source = frappe.get_doc(transaction.reference_doctype, transaction.reference_name)
	updates = {status_field: paid_value}
	if link_field:
		updates[link_field] = transaction.name

	source.flags.ignore_permissions = True
	source.db_set(updates, update_modified=True, notify=True)

	# Optional: submit the source doc when paid_value == "Submitted" on a submittable doctype.
	if paid_value.lower() == "submitted" and cint(getattr(source, "docstatus", 0)) == 0:
		try:
			meta = frappe.get_meta(transaction.reference_doctype)
			if getattr(meta, "is_submittable", 0):
				source.reload()
				source.submit()
		except Exception:
			# Submission failure is logged but doesn't roll back the transaction —
			# the field update already landed.
			frappe.log_error(
				frappe.get_traceback(),
				f"Pinelabs mapped finalization: submit failed for {source.doctype} {source.name}",
			)


# ──────────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────────


def _reload(transaction):
	"""Accept either a doc or a name; return a fresh doc."""
	if isinstance(transaction, str):
		return frappe.get_doc("Pinelabs Transaction", transaction)
	return frappe.get_doc("Pinelabs Transaction", transaction.name)


def _lock_and_reload(transaction):
	"""Take a row-level lock on the transaction, then return a fresh doc.

	The foreground poll, the realtime-driven cron reconcile, and the webhook
	can all reach a transition function concurrently. Without locking, two
	workers can both pass the "is this terminal?" check before either commits,
	and the loser hits TimestampMismatchError on save. SELECT … FOR UPDATE
	serializes them: the second caller blocks until the first commits, then
	loads the doc with the new (terminal) status and the idempotency check
	at the top of each transition returns early.
	"""
	name = transaction if isinstance(transaction, str) else transaction.name
	# for_update=True issues SELECT … FOR UPDATE; the lock is released
	# automatically when this request's transaction commits or rolls back.
	frappe.db.get_value("Pinelabs Transaction", name, "name", for_update=True)
	return frappe.get_doc("Pinelabs Transaction", name)


def _to_json_str(value):
	if isinstance(value, str):
		return value
	try:
		return json.dumps(value, indent=2, default=str)
	except Exception:
		return str(value)


def _truncate_with_ellipsis(text, max_length):
	"""Truncate text to max_length, appending an ellipsis when something was cut.

	Without the marker, an operator reading a 1000-char error_message has no
	way to know the gateway actually returned more — `response_payload` has
	the full body, but only if they know to look there.
	"""
	if text is None:
		return ""
	text = str(text)
	if len(text) <= max_length:
		return text
	# Leave 1 char of headroom for the ellipsis.
	return text[: max_length - 1] + "…"
