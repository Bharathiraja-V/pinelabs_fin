# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import flt, nowdate


def _get_bank_account(mode_of_payment, company):
	"""Resolve GL account from Mode of Payment Account child table."""
	bank_account = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mode_of_payment, "company": company},
		"default_account",
	)
	if not bank_account:
		frappe.throw(
			_(
				"No account configured for Mode of Payment '{0}' in company '{1}'. "
				"Please add a Mode of Payment Account row under the Mode of Payment document."
			).format(mode_of_payment, company)
		)
	return bank_account


def _write_payment_entry_link(source_doctype, source_docname, payment_entry, config=None):
	"""Write the created Payment Entry name to the mapped source field, if configured."""
	link_field = (config or {}).get("link_payment_entry_field")
	if link_field and payment_entry:
		frappe.db.set_value(
			source_doctype,
			source_docname,
			link_field,
			payment_entry,
			update_modified=True,
		)


def create_payment_entry(transaction_log_name):
	"""
	Create an ERPNext Payment Entry from an approved PineLabs transaction.

	Args:
		transaction_log_name: Name of the Pinelabs Transaction Log document

	Returns:
		dict with success status and payment_entry name
	"""
	try:
		log = frappe.get_doc("Pinelabs Transaction Log", transaction_log_name)
		from pinelabs_fin.api import services

		# Validate status
		if log.status != "Approved":
			return {
				"success": False,
				"error": _("Cannot create Payment Entry for transaction with status '{0}'.").format(
					log.status
				),
			}

		# Idempotency: check if PE already created for this transaction
		if log.payment_entry and frappe.db.exists("Payment Entry", log.payment_entry):
			source_doctype = log.source_doctype
			source_docname = log.source_docname or log.external_order_id or log.invoice
			config = services.get_payable_doctype_config(source_doctype)
			if source_doctype and source_docname:
				_write_payment_entry_link(source_doctype, source_docname, log.payment_entry, config=config)
			return {
				"success": True,
				"payment_entry": log.payment_entry,
				"already_exists": True,
				"message": _("Payment Entry {0} already exists for this transaction.").format(
					log.payment_entry
				),
			}

		# Also check by reference_no in case payment_entry field wasn't updated
		existing_pe = frappe.db.exists(
			"Payment Entry",
			{"reference_no": log.transaction_reference, "docstatus": 1},
		)
		if existing_pe:
			log.db_set("payment_entry", existing_pe)
			source_doctype = log.source_doctype
			source_docname = log.source_docname or log.external_order_id or log.invoice
			config = services.get_payable_doctype_config(source_doctype)
			if source_doctype and source_docname:
				_write_payment_entry_link(source_doctype, source_docname, existing_pe, config=config)
			return {
				"success": True,
				"payment_entry": existing_pe,
				"already_exists": True,
				"message": _("Payment Entry {0} already exists for this transaction.").format(
					existing_pe
				),
			}

		# Resolve source document
		source_doctype = log.source_doctype
		source_docname = log.source_docname or log.external_order_id or log.invoice
		if not source_doctype and source_docname:
			if frappe.db.exists("POS Invoice", source_docname):
				source_doctype = "POS Invoice"
			elif frappe.db.exists("Sales Invoice", source_docname):
				source_doctype = "Sales Invoice"

		if not source_doctype or not source_docname:
			return {
				"success": False,
				"error": _("No source document reference found on transaction log."),
			}

		if not frappe.db.exists(source_doctype, source_docname):
			return {
				"success": False,
				"error": _("{0} '{1}' not found.").format(source_doctype, source_docname),
			}

		invoice_doc = frappe.get_doc(source_doctype, source_docname)
		payable_config = services.get_payable_doctype_config(source_doctype)

		# Validate invoice is submitted
		if invoice_doc.docstatus != 1:
			return {
				"success": False,
				"error": _("Invoice must be submitted before payment can be recorded."),
			}

		# Validate outstanding amount
		outstanding = services.get_payable_amount(invoice_doc, config=payable_config)
		if outstanding <= 0:
			return {
				"success": False,
				"error": _("Invoice {0} has no outstanding amount.").format(source_docname),
			}

		# Get mode_of_payment:
		# 1. From the transaction log (set during payment initiation)
		# 2. Fallback: derive from payment_modes child table using payment_mode label
		# 3. Fallback: legacy mode_of_payment field in settings
		mode_of_payment = getattr(log, "mode_of_payment", None) or None
		if not mode_of_payment:
			settings = frappe.get_single("Pinelabs Settings")
			# Try to match from payment_modes child table using payment_mode label
			payment_mode_label = getattr(log, "payment_mode", None) or getattr(log, "payment_mode_label", None)
			if payment_mode_label and settings.payment_modes:
				for pm in settings.payment_modes:
					if pm.label == payment_mode_label:
						mode_of_payment = pm.mode_of_payment
						break
			# Fallback: first configured payment mode
			if not mode_of_payment and settings.payment_modes:
				mode_of_payment = settings.payment_modes[0].mode_of_payment
			# Fallback: legacy field
			if not mode_of_payment:
				mode_of_payment = settings.mode_of_payment
		if not mode_of_payment:
			frappe.throw(
				_(
					"No Mode of Payment configured. "
					"Please set up Payment Modes in Pinelabs Settings."
				)
			)

		company = invoice_doc.company
		bank_account = _get_bank_account(mode_of_payment, company)

		# Create Payment Entry using ERPNext's standard function
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		pe = get_payment_entry(
			dt=source_doctype,
			dn=source_docname,
			bank_account=bank_account,
			party_type="Customer",
			payment_type="Receive",
		)

		# Override fields
		pe.mode_of_payment = mode_of_payment
		pe.reference_no = log.transaction_reference
		pe.reference_date = nowdate()

		payment_amount = min(flt(log.amount), outstanding)
		pe.paid_amount = payment_amount
		pe.received_amount = payment_amount

		# Adjust allocated amount in references
		if pe.references:
			pe.references[0].allocated_amount = payment_amount

		remarks_parts = ["PineLabs payment {0}".format(log.transaction_reference)]
		if log.rrn:
			remarks_parts.append("RRN: {0}".format(log.rrn))
		if log.approval_code:
			remarks_parts.append("Approval Code: {0}".format(log.approval_code))
		pe.remarks = " | ".join(remarks_parts)
		pe.custom_remarks = 1

		pe.insert(ignore_permissions=True)
		pe.submit()

		# Update transaction log with PE reference
		log.db_set("payment_entry", pe.name)
		_write_payment_entry_link(source_doctype, source_docname, pe.name, config=payable_config)

		return {
			"success": True,
			"payment_entry": pe.name,
			"message": _("Payment Entry {0} created successfully.").format(pe.name),
		}

	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "PineLabs Payment Entry Creation Error")
		return {
			"success": False,
			"error": str(e),
		}
