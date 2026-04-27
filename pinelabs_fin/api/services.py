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
DEFAULT_BUTTON_LABEL = "Pay via Pinelabs"


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


def serialize_payable_doctype_config(row):
	"""Return the public payable-doctype config payload for a settings row."""
	if not row:
		return None

	finalize_via_payment_entry = getattr(row, "finalize_via_payment_entry", None)
	if finalize_via_payment_entry is None:
		finalize_via_payment_entry = 1

	return {
		"doctype_name": row.doctype_name,
		"button_label": row.button_label or DEFAULT_BUTTON_LABEL,
		"show_on_docstatus": row.show_on_docstatus or "1",
		"is_enabled": cint(row.is_enabled),
		"amount_field": row.amount_field or None,
		"status_field": row.status_field or None,
		"paid_status_value": row.paid_status_value or None,
		"link_payment_entry_field": row.link_payment_entry_field or None,
		"finalize_via_payment_entry": cint(finalize_via_payment_entry),
	}


def get_payable_doctype_config(doctype, enabled_only=True):
	"""Return the configured payable-doctype row for a source doctype, if enabled."""
	if not doctype:
		return None

	try:
		settings = get_settings()
	except Exception:
		return None

	for row in settings.get("payable_doctypes", []) or []:
		if row.doctype_name != doctype:
			continue
		if enabled_only and cint(row.is_enabled) != 1:
			continue
		return serialize_payable_doctype_config(row)

	return None


def docstatus_matches_payable_config(doc, config):
	"""Return True when the document docstatus is allowed by the payable config."""
	if not config:
		return True

	docstatus = cint(getattr(doc, "docstatus", 0))
	if docstatus == 2:
		return False

	allowed = [
		cint(value.strip())
		for value in (config.get("show_on_docstatus") or "1").split(",")
		if value.strip()
	]
	return docstatus in allowed


def get_payable_amount(doc, config=None):
	"""Return the payable amount for supported source documents."""
	if not doc:
		return 0.0

	doctype = getattr(doc, "doctype", None)
	docstatus = cint(getattr(doc, "docstatus", 0))

	if docstatus == 2:
		return 0.0

	if config and config.get("amount_field"):
		return max(flt(getattr(doc, config.get("amount_field"), 0)), 0.0)

	if doctype in ("Sales Invoice", "POS Invoice"):
		if docstatus == 1:
			outstanding = flt(getattr(doc, "outstanding_amount", 0))
			return outstanding if outstanding > 0 else 0.0

		return max(
			flt(getattr(doc, "grand_total", 0)) - flt(getattr(doc, "paid_amount", 0)),
			0.0,
		)

	if doctype == "Payment Entry":
		if docstatus != 0:
			return 0.0
		return max(flt(getattr(doc, "paid_amount", 0)), 0.0)

	if docstatus == 1:
		outstanding = flt(getattr(doc, "outstanding_amount", 0))
		return outstanding if outstanding > 0 else 0.0

	return 0.0


def is_doc_already_paid(doc, config=None):
	"""Return True when the submitted source document is fully paid."""
	if not doc:
		return False

	if config and config.get("status_field") and config.get("paid_status_value"):
		status_value = getattr(doc, config.get("status_field"), None)
		if str(status_value or "").strip() == str(config.get("paid_status_value") or "").strip():
			return True
		if cint(config.get("finalize_via_payment_entry")) != 1:
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


def validate_trigger_context(
	doc,
	mode_of_payment,
	trigger_style=MANUAL_TRIGGER_STYLE,
	config=None,
):
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

	if config and not docstatus_matches_payable_config(doc, config):
		return False, _("Document docstatus is not allowed for PineLabs payment.")

	if get_payable_amount(doc, config=config) <= 0:
		return False, _("Document has no payable amount.")

	if is_doc_already_paid(doc, config=config):
		return False, _("Document is already fully paid.")

	return True, None


def evaluate_pinelabs_context(doctype, docname=None, mode_of_payment=None, context=None, amount=None):
	"""Return the current PineLabs context for a source document."""
	response = {
		"trigger_style": resolve_trigger_style(doctype=doctype, context=context),
		"can_trigger": False,
		"reason": None,
		"payable_amount": flt(amount or 0.0),
		"selected_mode": None,
		"selected_mode_config": None,
		"payment_modes": get_all_enabled_pinelabs_modes(),
		"doc_state": None,
		"payable_config": None,
	}

	if not is_pinelabs_enabled():
		response["reason"] = _("PineLabs integration is not enabled.")
		return response

	if response["trigger_style"] == "unsupported":
		response["reason"] = _("Doctype is required.")
		return response

	# docname can be None or "" for new/unnamed documents in some flows
	if docname is None:
		response["can_trigger"] = bool(response["payment_modes"]) and has_enabled_machine()
		if not response["can_trigger"]:
			response["reason"] = _("No payment modes or machines configured.")
		return response

	if not docname or not frappe.db.exists(doctype, docname):
		# If document doesn't exist in DB, it might be a new/offline POS invoice.
		# We create a mock document for validation purposes.
		doc = frappe._dict({
			"doctype": doctype,
			"name": docname,
			"docstatus": 0,
			"is_new": True
		})
		
		payable_config = get_payable_doctype_config(doctype)
		response["payable_config"] = payable_config
		
		if context and isinstance(context, dict) and context.get("amount"):
			response["payable_amount"] = flt(context.get("amount"))
		
		response["doc_state"] = "draft"
		response["can_trigger"] = True # Allow triggering for new docs if machine is configured
		
		selected_mode = get_selected_mode_of_payment(doc, mode_of_payment=mode_of_payment)
		if selected_mode:
			response["selected_mode"] = selected_mode
			response["selected_mode_config"] = get_pinelabs_mode_config(selected_mode)
		
		if not has_enabled_machine():
			response["can_trigger"] = False
			response["reason"] = _("No PineLabs machine is configured or enabled.")
		
		return response

	doc = frappe.get_doc(doctype, docname)
	payable_config = get_payable_doctype_config(doctype)
	response["payable_config"] = payable_config
	response["payable_amount"] = get_payable_amount(doc, config=payable_config)

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
		config=payable_config,
	)
	response["can_trigger"] = eligible
	response["reason"] = reason

	return response


def handle_mode_of_payment_trigger(
	doctype,
	docname,
	mode_of_payment,
	amount,
	allowed_payment_mode=None,
	machine_name=None,
	context=None,
):
	"""Validate the request and start PineLabs terminal payment."""
	context_data = evaluate_pinelabs_context(
		doctype=doctype,
		docname=docname,
		mode_of_payment=mode_of_payment,
		context=context,
		amount=amount,
	)
	if not context_data["can_trigger"]:
		return {"success": False, "error": context_data["reason"]}

	mode_config = get_pinelabs_mode_config(mode_of_payment)
	if not mode_config and not allowed_payment_mode:
		return {
			"success": False,
			"error": _("Mode of Payment '{0}' is not a PineLabs mode.").format(mode_of_payment),
		}

	payable_amount = flt(context_data.get("payable_amount") or amount)
	
	if allowed_payment_mode:
		allowed_payment_mode = cint(allowed_payment_mode)
	elif mode_config:
		allowed_payment_mode = cint(mode_config.get("allowed_payment_mode"))
	
	if not allowed_payment_mode:
		allowed_payment_mode = 1 # Default to Card if not specified
	
	payment_mode_label = (mode_config.get("label") if mode_config else None) or "Card"
	if allowed_payment_mode == 11 and payment_mode_label == "Card":
		payment_mode_label = "UPI"

	return start_terminal_payment(
		source_doctype=doctype,
		source_docname=docname,
		amount=payable_amount,
		mode_of_payment=mode_of_payment,
		allowed_payment_mode=allowed_payment_mode,
		payment_mode_label=payment_mode_label,
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
	from pinelabs_fin.api.host_adapter import get_host_adapter

	log = frappe.get_doc("Pinelabs Transaction Log", transaction_log_name)
	return get_host_adapter().finalize_transaction(log)


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
