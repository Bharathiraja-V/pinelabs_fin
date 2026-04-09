# -*- coding: utf-8 -*-
"""Shared PineLabs service functions.

This module keeps the current PineLabs Mode of Payment flow centralized:

1. Identify PineLabs payment modes from settings.
2. Validate whether a saved source document is eligible.
3. Start the terminal payment request.
4. Expose status, cancel, and finalization helpers for other callers.
"""

from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import cint, flt


MANUAL_TRIGGER_STYLE = "manual"


def get_settings():
	"""Return the Pinelabs Settings single doc."""
	return frappe.get_single("Pinelabs Settings")


def is_pinelabs_enabled():
	"""Return True when the integration is enabled in settings."""
	try:
		return bool(get_settings().enabled)
	except Exception:
		return False


def has_enabled_machine():
	"""Return True when at least one PineLabs machine is enabled."""
	try:
		return any(machine.enabled for machine in (get_settings().machines or []))
	except Exception:
		return False


def get_all_enabled_pinelabs_modes():
	"""Return enabled PineLabs mode rows from settings."""
	if not is_pinelabs_enabled():
		return []

	settings = get_settings()
	return [
		{
			"mode_of_payment": row.mode_of_payment,
			"label": row.label,
			"allowed_payment_mode": row.allowed_payment_mode,
			"icon": row.icon or "",
			"is_enabled": cint(row.is_enabled),
		}
		for row in (settings.payment_modes or [])
		if cint(row.is_enabled) == 1
	]


def is_pinelabs_mode(mode_of_payment):
	"""Return True when the given Mode of Payment is configured for PineLabs."""
	if not mode_of_payment:
		return False

	return any(
		mode["mode_of_payment"] == mode_of_payment
		for mode in get_all_enabled_pinelabs_modes()
	)


def get_pinelabs_mode_config(mode_of_payment):
	"""Return the enabled PineLabs mode config for the given Mode of Payment."""
	if not mode_of_payment:
		return None

	for mode in get_all_enabled_pinelabs_modes():
		if mode["mode_of_payment"] == mode_of_payment:
			return mode
	return None


def resolve_trigger_style(doctype=None, context=None):
	"""Return the shared trigger style for the current app-owned flow."""
	if not doctype:
		return "unsupported"
	return MANUAL_TRIGGER_STYLE


def get_payable_amount(doc):
	"""Return the payable amount for supported source documents."""
	if not doc:
		return 0.0

	if getattr(doc, "docstatus", 0) == 1:
		outstanding = flt(getattr(doc, "outstanding_amount", 0))
		return outstanding if outstanding > 0 else 0.0

	if getattr(doc, "doctype", None) in ("Sales Invoice", "POS Invoice"):
		return max(
			flt(getattr(doc, "grand_total", 0)) - flt(getattr(doc, "paid_amount", 0)),
			0.0,
		)

	if getattr(doc, "doctype", None) == "Payment Entry":
		return flt(getattr(doc, "paid_amount", 0))

	return 0.0


def is_doc_already_paid(doc):
	"""Return True when the submitted source document is fully paid."""
	if not doc:
		return False
	if getattr(doc, "docstatus", 0) != 1:
		return False
	return flt(getattr(doc, "outstanding_amount", 0)) <= 0


def get_selected_mode_of_payment(doc, mode_of_payment=None):
	"""Resolve the PineLabs Mode of Payment from the doc or explicit override."""
	if mode_of_payment and is_pinelabs_mode(mode_of_payment):
		return mode_of_payment

	document_mode = getattr(doc, "mode_of_payment", None)
	if is_pinelabs_mode(document_mode):
		return document_mode

	for row in (getattr(doc, "payments", None) or []):
		row_mode = getattr(row, "mode_of_payment", None)
		if is_pinelabs_mode(row_mode):
			return row_mode

	return None


def validate_trigger_context(doc, mode_of_payment, trigger_style=MANUAL_TRIGGER_STYLE):
	"""Validate whether the given document is ready for PineLabs payment."""
	if not is_pinelabs_enabled():
		return False, _("PineLabs integration is not enabled.")

	if not has_enabled_machine():
		return False, _("No PineLabs machine is configured or enabled.")

	if mode_of_payment and not is_pinelabs_mode(mode_of_payment):
		return False, _("Mode of Payment '{0}' is not a PineLabs mode.").format(mode_of_payment)

	if not doc:
		return False, _("Document not provided.")

	if not getattr(doc, "name", None) or str(doc.name).startswith("new-"):
		return False, _("Document must be saved before triggering payment.")

	if getattr(doc, "docstatus", 0) == 2:
		return False, _("Document is cancelled.")

	if get_payable_amount(doc) <= 0:
		return False, _("Document has no payable amount.")

	if is_doc_already_paid(doc):
		return False, _("Document is already fully paid.")

	return True, None


def evaluate_pinelabs_context(doctype, docname=None, mode_of_payment=None, context=None):
	"""Return the current PineLabs context for a source document."""
	response = {
		"trigger_style": resolve_trigger_style(doctype=doctype, context=context),
		"can_trigger": False,
		"reason": None,
		"payable_amount": 0.0,
		"selected_mode": None,
		"selected_mode_config": None,
		"payment_modes": get_all_enabled_pinelabs_modes(),
		"doc_state": None,
	}

	if not is_pinelabs_enabled():
		response["reason"] = _("PineLabs integration is not enabled.")
		return response

	if response["trigger_style"] == "unsupported":
		response["reason"] = _("Doctype is required.")
		return response

	if not docname:
		response["can_trigger"] = bool(response["payment_modes"]) and has_enabled_machine()
		if not response["can_trigger"]:
			response["reason"] = _("No payment modes or machines configured.")
		return response

	if not frappe.db.exists(doctype, docname):
		response["reason"] = _("{0} {1} not found.").format(doctype, docname)
		return response

	doc = frappe.get_doc(doctype, docname)
	response["payable_amount"] = get_payable_amount(doc)

	docstatus = getattr(doc, "docstatus", 0)
	if docstatus == 0:
		response["doc_state"] = "draft"
	elif docstatus == 1:
		response["doc_state"] = "submitted"
	elif docstatus == 2:
		response["doc_state"] = "cancelled"

	selected_mode = get_selected_mode_of_payment(doc, mode_of_payment=mode_of_payment)
	if selected_mode:
		response["selected_mode"] = selected_mode
		response["selected_mode_config"] = get_pinelabs_mode_config(selected_mode)

	eligible, reason = validate_trigger_context(
		doc,
		selected_mode,
		trigger_style=response["trigger_style"],
	)
	response["can_trigger"] = eligible
	response["reason"] = reason

	return response


def handle_mode_of_payment_trigger(
	doctype,
	docname,
	mode_of_payment,
	amount,
	machine_name=None,
	context=None,
):
	"""Validate the request and start PineLabs terminal payment."""
	context_data = evaluate_pinelabs_context(
		doctype=doctype,
		docname=docname,
		mode_of_payment=mode_of_payment,
		context=context,
	)
	if not context_data["can_trigger"]:
		return {"success": False, "error": context_data["reason"]}

	mode_config = get_pinelabs_mode_config(mode_of_payment)
	if not mode_config:
		return {
			"success": False,
			"error": _("Mode of Payment '{0}' is not a PineLabs mode.").format(mode_of_payment),
		}

	return start_terminal_payment(
		source_doctype=doctype,
		source_docname=docname,
		amount=flt(amount),
		mode_of_payment=mode_of_payment,
		allowed_payment_mode=cint(mode_config["allowed_payment_mode"]),
		payment_mode_label=mode_config["label"],
		machine_name=machine_name,
	)


def start_terminal_payment(
	source_doctype,
	source_docname,
	amount,
	mode_of_payment,
	allowed_payment_mode,
	payment_mode_label,
	machine_name=None,
):
	"""Start the PineLabs terminal payment request."""
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client
	from pinelabs_fin.api.pos_endpoints import _get_warehouse_from_invoice

	warehouse = None
	try:
		doc = frappe.get_doc(source_doctype, source_docname)
		if hasattr(doc, "items"):
			warehouse = _get_warehouse_from_invoice(doc)
	except Exception:
		warehouse = None

	client = get_pinelabs_client(machine_name=machine_name, warehouse=warehouse)
	result = client.upload_billed_transaction(
		invoice_name=source_docname,
		amount=amount,
		payment_mode=payment_mode_label,
		allowed_payment_mode=allowed_payment_mode,
		mode_of_payment=mode_of_payment,
		source_system="erpnext",
		source_doctype=source_doctype,
		source_docname=source_docname,
		external_order_id=source_docname,
	)

	if result.get("success"):
		return {
			"success": True,
			"transaction_reference": result.get("transaction_reference"),
			"transaction_log": result.get("transaction_log"),
			"message": _("Payment initiated. Please complete payment on terminal."),
			"amount": amount,
			"payment_mode": payment_mode_label,
			"mode_of_payment": mode_of_payment,
			"allowed_payment_mode": allowed_payment_mode,
			"invoice_name": source_docname,
			"invoice_doctype": source_doctype,
			"response_code": result.get("response_code"),
			"response_message": result.get("response_message"),
		}

	return {
		"success": False,
		"error": result.get("error", _("Failed to initiate payment")),
		"transaction_reference": result.get("transaction_reference"),
		"transaction_log": result.get("transaction_log"),
		"response_code": result.get("response_code"),
	}


def get_payment_status(transaction_reference):
	"""Return current status for a transaction."""
	from pinelabs_fin.api.pos_endpoints import check_payment_status

	return check_payment_status(transaction_reference)


def cancel_payment(transaction_reference):
	"""Cancel a pending transaction."""
	from pinelabs_fin.api.pos_endpoints import cancel_payment as cancel_payment_request

	return cancel_payment_request(transaction_reference)


def handle_success(transaction_log_name):
	"""Handle a successful payment by creating the ERPNext payment entry."""
	from pinelabs_fin.api.payment_bridge import create_payment_entry

	return create_payment_entry(transaction_log_name)


def handle_failure(transaction_log_name, error_message=None):
	"""Mark a transaction log as failed when the flow stops with an error."""
	if not transaction_log_name:
		return

	try:
		log = frappe.get_doc("Pinelabs Transaction Log", transaction_log_name)
		if log.status not in ("Failed", "Declined", "Cancelled", "Timeout"):
			log.db_set("status", "Failed")
		if error_message and not log.error_message:
			log.db_set("error_message", str(error_message)[:140])
	except Exception:
		frappe.log_error(frappe.get_traceback(), "PineLabs handle_failure")


def get_trigger_style(doctype):
	"""Backward-compatible alias for resolve_trigger_style."""
	return resolve_trigger_style(doctype=doctype)


def can_trigger_pinelabs(doc, mode_of_payment=None):
	"""Backward-compatible alias for validate_trigger_context."""
	return validate_trigger_context(doc, mode_of_payment)
