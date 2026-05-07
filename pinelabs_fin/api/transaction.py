# -*- coding: utf-8 -*-
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

from __future__ import unicode_literals

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime


VALID_FLOW_TYPES = ("Terminal", "Plural", "Direct NB")
ACTIVE_STATUSES = ("INITIATED", "PENDING")


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
	bank_code=None,
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
	doc.bank_code = bank_code
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
		transaction.error_message = error_message[:1000]
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
		transaction.error_message = (reason or "")[:1000]
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
	- pinelabs_enabled = 1
	- pinelabs_flow_type matches (or "Both")
	- For Terminal: pinelabs_terminal_code matches the integer code we sent
	- For Plural / Direct NB: pinelabs_plural_method matches CARD/UPI/NB/WALLET
	"""
	flow = transaction.flow_type
	method = (transaction.payment_method or "").upper()

	filters = {"pinelabs_enabled": 1}

	if flow == "Terminal":
		# Terminal already knows its method up-front; payment_method is "CARD" or "UPI".
		filters["pinelabs_flow_type"] = ["in", ["Terminal", "Both"]]
		if method:
			filters["pinelabs_plural_method"] = method
	else:
		# Plural / Direct NB — payment_method comes back from the webhook.
		filters["pinelabs_flow_type"] = ["in", ["Plural", "Both"]]
		if method:
			filters["pinelabs_plural_method"] = method

	rows = frappe.get_all(
		"Mode of Payment",
		filters=filters,
		fields=["name"],
		limit=1,
	)
	if not rows:
		frappe.throw(
			_("No PineLabs Mode of Payment found for flow={0}, method={1}.").format(flow, method or "?"),
		)
	return rows[0].name


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
	existing = frappe.db.get_value(
		"Payment Entry",
		{"reference_no": transaction.payment_id},
		"name",
	)
	if existing:
		return existing

	# Rule 4: standard ERPNext PE builder.
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	if transaction.reference_doctype not in ("Sales Invoice", "POS Invoice", "Payment Request"):
		frappe.throw(
			_("PE creation supports Sales Invoice / POS Invoice / Payment Request only. Got {0}. Either add support or set Create Payment Entry to 0 on this doctype's Payable Doctypes row.").format(
				transaction.reference_doctype
			),
		)

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
			"button_label": row.button_label,
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

	Behavior mirrors the prior host_adapter mapped-finalization path:
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
