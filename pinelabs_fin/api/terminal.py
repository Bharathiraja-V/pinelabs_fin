# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Terminal payment flow.

Whitelisted entry points for the "Pay on Machine" button. Each method
takes the source document + selected Mode of Payment, drives the Pine
Labs Cloud API, and updates the Pinelabs Transaction via the helpers
in transaction.py.

State flow:
    initiate → INITIATED              (transaction created, API call sent)
    initiate response is sync:
        ResponseCode 0    → mark_pending (terminal is processing)
        ResponseCode 1/2  → mark_failed (rejected immediately)
    poll → mark_success | mark_pending | mark_failed
    cancel → mark_cancelled
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, time_diff_in_seconds

# When Pine Labs returns a non-standard ResponseCode for a terminal poll,
# the txn would otherwise sit PENDING forever and the cron would loop on it.
# After this many minutes with no movement, mark the txn FAILED so the
# operator sees a terminal state. Overrideable per-machine via
# Pinelabs Machine Config.auto_cancel_duration_in_minutes.
_DEFAULT_AUTO_CANCEL_MINUTES = 5


@frappe.whitelist()
def initiate_terminal_payment(
	reference_doctype: str,
	reference_name: str,
	mode_of_payment: str,
	amount: float | None = None,
	create_payment_entry: int | None = None,
) -> dict:
	"""Start a terminal payment.

	Returns {success, transaction_name, response_code, message}.
	The frontend should then poll check_terminal_status until terminal.

	``create_payment_entry`` (optional): per-call override of the Payable
	Documents row's Create Payment Entry setting.

	- ``1`` → always create a PE on success (and run the account-mapping
	  pre-flight now)
	- ``0`` → never create a PE; fall back to mapped finalization if the
	  Payable Documents row configures it, otherwise leave the source doc
	  untouched
	- omitted / ``None`` → use the row's setting (current behaviour)
	"""
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client
	from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
		resolve_machine_for_context,
	)

	source_doc = _load_eligible_source(reference_doctype, reference_name)
	# Fail fast BEFORE we contact Pine Labs — if finalization won't work for
	# this doctype, the customer should never be asked to tap their card.
	txn_service.validate_finalization_ready(reference_doctype)
	mode_doc = _load_pinelabs_mode(mode_of_payment, expected_flows=("Terminal", "Both"))
	# The Mode of Payment must have a default account for the source doc's
	# company — but only if the configured finalization path actually creates
	# a Payment Entry. Mapped-field finalization (custom doctypes) and the
	# permissive no-config path don't post to a ledger account.
	txn_service.assert_mode_has_account_mapped(
		mode_of_payment,
		getattr(source_doc, "company", None),
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		create_payment_entry=create_payment_entry,
	)
	resolved_amount = flt(amount) if amount else _amount_for(source_doc)
	if resolved_amount <= 0:
		frappe.throw(_("No outstanding amount on {0} {1}.").format(reference_doctype, reference_name))

	# Don't start a second attempt while one is in flight.
	existing = txn_service.get_active_for_doc(reference_doctype, reference_name)
	if existing:
		frappe.throw(
			_("A {0} payment ({1}) is already in progress for this document.").format(
				existing.flow_type, existing.name
			),
		)

	machine = resolve_machine_for_context(
		reference_doctype, reference_name, source_doc=source_doc,
	)
	if not machine:
		frappe.throw(
			_("No Pinelabs machine resolves for this transaction. Configure machine mappings in Pinelabs Machine Mapping, or mark one machine as Is Default in Pinelabs Settings."),
		)

	terminal_method = _terminal_method_label(mode_doc)
	terminal_code = cint(mode_doc.get("pinelabs_terminal_code"))
	if terminal_code not in (1, 11):
		frappe.throw(
			_("Mode of Payment {0} has invalid terminal code {1}; expected 1 (Card) or 11 (UPI).").format(
				mode_of_payment, terminal_code
			),
		)

	txn = txn_service.create_transaction(
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		flow_type="Terminal",
		amount=resolved_amount,
		payment_method=terminal_method,
		machine=machine.machine_name,
		create_payment_entry=create_payment_entry,
	)

	client = get_pinelabs_client(machine_name=machine.machine_name)
	response = client.upload_billed_raw(
		transaction_ref=txn.name,
		amount=resolved_amount,
		allowed_payment_mode=terminal_code,
	)
	response_data = response.get("data") or {}
	response_code = response_data.get("ResponseCode")
	plutus_ref = response_data.get("PlutusTransactionReferenceID")
	plutus_ref_str = str(plutus_ref) if plutus_ref else None
	# Surfaced by the client so we can persist exactly what the app sent
	# alongside what Pine Labs returned. Same shape Plural already stores
	# at create_transaction time — this brings the Terminal flow to parity.
	request_payload = response.get("request_payload")

	# ResponseCode mapping (UploadBilledTransaction):
	#   0    = accepted, terminal is now processing → PENDING
	#   1, 2 = rejected synchronously → FAILED
	if not response.get("success"):
		txn_service.mark_failed(
			txn,
			error_message=response.get("error") or _("Pine Labs API call failed"),
			response_payload=response_data or response,
			request_payload=request_payload,
		)
		return _shape(success=False, txn=txn, response_code=response_code, message=response.get("error"))

	if response_code in (0, "0"):
		# Pass payment_id through mark_pending — the doc gets reloaded inside,
		# so an in-memory `txn.payment_id = ...` here would be lost.
		txn_service.mark_pending(
			txn,
			payment_id=plutus_ref_str,
			response_payload=response_data,
			request_payload=request_payload,
		)
		return _shape(success=True, txn=txn, response_code=response_code, message=response_data.get("ResponseMessage"))

	# Synchronous decline.
	txn_service.mark_failed(
		txn,
		error_message=response_data.get("ResponseMessage") or _("Terminal declined the request"),
		response_payload=response_data,
		request_payload=request_payload,
	)
	return _shape(success=False, txn=txn, response_code=response_code, message=response_data.get("ResponseMessage"))


@frappe.whitelist()
def check_terminal_status(transaction_name: str, create_payment_entry: int | None = None) -> dict:
	"""Poll Pine Labs for the latest terminal status of a transaction.

	Frontend calls this every ~3s while its modal is open.
	Returns {success, status, response_code, message}.

	``create_payment_entry`` (optional): late override of the same flag set
	at initiate time. ``1`` / ``0`` updates the stored decision on the
	Pinelabs Transaction so the finalize path uses it. Omit to leave the
	stored value (or the Payable Documents config) untouched.
	"""
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

	txn = frappe.get_doc("Pinelabs Transaction", transaction_name)
	# Late override — caller can flip the create-PE decision before the
	# poll triggers mark_success. Stored on the row so the cron/webhook
	# paths also honor the latest value.
	override = txn_service.normalize_create_payment_entry(create_payment_entry)
	if override and txn.create_payment_entry_override != override and txn.status in ("INITIATED", "PENDING"):
		txn.create_payment_entry_override = override
		txn.flags.pinelabs_internal_transition = True
		txn.save(ignore_permissions=True)
	if txn.flow_type != "Terminal":
		frappe.throw(_("Transaction {0} is not a Terminal transaction.").format(transaction_name))
	if txn.status in ("SUCCESS", "FAILED", "CANCELLED"):
		return {"success": True, "status": txn.status, "response_code": None, "message": "terminal state"}
	if not txn.payment_id:
		return {"success": False, "status": txn.status, "response_code": None, "message": "PlutusTransactionReferenceID missing"}

	client = get_pinelabs_client(machine_name=txn.machine)
	response = client.get_status_raw(txn.payment_id)
	response_data = response.get("data") or {}
	response_code = response_data.get("ResponseCode")

	# ResponseCode mapping (GetCloudBasedTxnStatus):
	#   0    = APPROVED
	#   1001 = TXN UPLOADED (still pending)
	#   1, 2 = DECLINED
	if response_code in (0, "0"):
		txn_service.mark_success(
			txn,
			payment_id=txn.payment_id,
			payment_method=txn.payment_method,
			response_payload=response_data,
		)
		return {"success": True, "status": "SUCCESS", "response_code": response_code, "message": "Approved"}

	if response_code in (1001, "1001"):
		# Still waiting on the terminal — refresh response_payload for visibility.
		# Lock the row so the foreground 3s poll and the 1-min cron can't stomp
		# each other's response_payload writes. If another worker already finalized
		# us in the meantime, bail out and report the new state.
		txn = txn_service._lock_and_reload(txn)
		if txn.status in ("SUCCESS", "FAILED", "CANCELLED"):
			return {"success": True, "status": txn.status, "response_code": response_code, "message": "already finalized"}
		txn.response_payload = frappe.as_json(response_data, indent=2)
		txn.flags.pinelabs_internal_transition = True
		txn.save(ignore_permissions=True)
		return {"success": True, "status": "PENDING", "response_code": response_code, "message": "Waiting on terminal"}

	if response_code in (1, 2, "1", "2"):
		message = response_data.get("ResponseMessage") or _("Terminal declined")
		txn_service.mark_failed(txn, error_message=message, response_payload=response_data)
		return {"success": True, "status": "FAILED", "response_code": response_code, "message": message}

	# Unknown response_code — Pine Labs returned something outside the documented
	# set. Capture it on the txn for visibility AND age-out to FAILED so the row
	# doesn't sit PENDING forever (and the cron doesn't loop on it forever).
	unknown_msg = response_data.get("ResponseMessage") or "Unknown response code"
	txn = txn_service._lock_and_reload(txn)
	if txn.status in ("SUCCESS", "FAILED", "CANCELLED"):
		return {"success": True, "status": txn.status, "response_code": response_code, "message": "already finalized"}
	txn.response_payload = frappe.as_json(response_data, indent=2)
	txn.error_message = txn_service._truncate_with_ellipsis(
		f"Unknown ResponseCode {response_code}: {unknown_msg}", 1000
	)
	txn.flags.pinelabs_internal_transition = True
	txn.save(ignore_permissions=True)

	age_minutes = _txn_age_minutes(txn)
	cutoff = _auto_cancel_minutes(txn)
	if age_minutes >= cutoff:
		txn_service.mark_failed(
			txn,
			error_message=(
				f"No terminal state after {cutoff} min. Last ResponseCode={response_code}: {unknown_msg}"
			),
			response_payload=response_data,
		)
		return {"success": True, "status": "FAILED", "response_code": response_code, "message": "Aged out"}

	return {
		"success": True,
		"status": txn.status,
		"response_code": response_code,
		"message": unknown_msg,
	}


@frappe.whitelist()
def cancel_terminal_payment(transaction_name: str, reason: str | None = None) -> dict:
	"""User-driven cancel of an in-flight terminal transaction."""
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

	txn = frappe.get_doc("Pinelabs Transaction", transaction_name)
	if txn.flow_type != "Terminal":
		frappe.throw(_("Transaction {0} is not a Terminal transaction.").format(transaction_name))
	if txn.status in ("SUCCESS", "FAILED", "CANCELLED"):
		return {"success": True, "status": txn.status, "message": "Already terminal"}

	server_message = None
	if txn.payment_id:
		client = get_pinelabs_client(machine_name=txn.machine)
		response = client.cancel_raw(txn.payment_id)
		response_data = response.get("data") or {}
		server_message = response_data.get("ResponseMessage")

	txn_service.mark_cancelled(txn, reason=reason or server_message or "user cancelled")
	return {"success": True, "status": "CANCELLED", "message": server_message or "Cancelled"}


def reconcile_pending_terminal_payments() -> None:
	"""Cron task — reconcile every PENDING Terminal transaction with Pine Labs.

	Why this exists: the browser dialog drives `check_terminal_status` while
	the user watches it, but if the user closes the tab, refreshes, or any
	dialog dismisses unexpectedly, the in-flight transaction would be stuck
	at PENDING forever (Pine Labs Cloud doesn't push terminal webhooks). This
	job pulls the latest status server-side every minute so the doc gets
	finalized regardless of what the browser is doing.

	Idempotent: piggy-backs on `check_terminal_status`, which is already
	idempotent (mark_success / mark_failed both no-op on terminal states).
	Errors per row are logged but never abort the whole batch.
	"""
	rows = frappe.get_all(
		"Pinelabs Transaction",
		filters={
			"flow_type": "Terminal",
			"status": "PENDING",
		},
		fields=["name", "payment_id"],
	)
	for row in rows:
		# Skip rows that don't yet have a Plutus reference — the foreground
		# request that creates them sets payment_id during initiate; if it's
		# missing here we can't query Pine Labs anyway.
		if not row.payment_id:
			continue
		try:
			check_terminal_status(row.name)
			# Per-row commit in a batch cron: a successfully reconciled
			# transaction must be durable before moving to the next row, so
			# one bad row's rollback (in the except below) can't undo the
			# rows already finalized in this run.
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				frappe.get_traceback(),
				f"Pinelabs reconcile_pending_terminal_payments failed for {row.name}",
			)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _load_eligible_source(reference_doctype, reference_name):
	if not (reference_doctype and reference_name):
		frappe.throw(_("reference_doctype and reference_name are required."))
	if not frappe.db.exists(reference_doctype, reference_name):
		frappe.throw(_("{0} {1} does not exist.").format(reference_doctype, reference_name))
	doc = frappe.get_doc(reference_doctype, reference_name)
	if cint(getattr(doc, "docstatus", 0)) != 1:
		frappe.throw(_("{0} {1} must be submitted to take a terminal payment.").format(reference_doctype, reference_name))
	return doc


def _amount_for(source_doc):
	# Outstanding amount for SI/POSI/Payment Request, fall back to grand_total.
	for field in ("outstanding_amount", "grand_total", "amount"):
		val = getattr(source_doc, field, None)
		if val is not None:
			return flt(val)
	return 0.0


def _load_pinelabs_mode(mode_of_payment, expected_flows):
	"""Validate that ``mode_of_payment`` is a Pine Labs Terminal mode.

	Pine Labs modes are identified by name (see ``PINELABS_TERMINAL_MODES``
	in transaction.py) — no custom-field lookup is needed. Returns a small
	dict the rest of the file can read like the legacy ``mode_doc``:
	``{"pinelabs_terminal_code": <int>}``.

	``expected_flows`` is kept for signature compatibility with the legacy
	flow-type check; this function only ever runs against the Terminal
	dialog, so the only valid input is ``("Terminal", "Both")``.
	"""
	from pinelabs_fin.api.transaction import PINELABS_TERMINAL_MODES

	if not mode_of_payment:
		frappe.throw(_("Mode of Payment is required."))
	if not frappe.db.exists("Mode of Payment", mode_of_payment):
		frappe.throw(_("Mode of Payment {0} does not exist.").format(mode_of_payment))

	terminal_code = PINELABS_TERMINAL_MODES.get(mode_of_payment)
	if terminal_code is None:
		frappe.throw(
			_(
				"Mode of Payment {0} is not a Pine Labs Terminal mode. "
				"Pick one of: {1}."
			).format(mode_of_payment, ", ".join(PINELABS_TERMINAL_MODES.keys())),
		)
	return {"pinelabs_terminal_code": terminal_code}


def _terminal_method_label(mode_doc):
	"""Map terminal_code → CARD / UPI string used for payment_method."""
	code = cint(mode_doc.get("pinelabs_terminal_code"))
	return {1: "CARD", 11: "UPI"}.get(code, "")


def _shape(*, success, txn, response_code, message):
	return {
		"success": bool(success),
		"transaction_name": txn.name,
		"status": txn.status,
		"response_code": response_code,
		"message": message,
	}


def _txn_age_minutes(txn) -> float:
	"""Minutes since the Pinelabs Transaction was created."""
	created = txn.created_at or txn.creation
	if not created:
		return 0.0
	return time_diff_in_seconds(get_datetime(), get_datetime(created)) / 60.0


def _auto_cancel_minutes(txn) -> int:
	"""Per-machine override for auto-cancel age (falls back to the default)."""
	if not txn.machine:
		return _DEFAULT_AUTO_CANCEL_MINUTES
	minutes = frappe.db.get_value(
		"Pinelabs Machine Config",
		txn.machine,
		"auto_cancel_duration_in_minutes",
	)
	return cint(minutes) or _DEFAULT_AUTO_CANCEL_MINUTES
