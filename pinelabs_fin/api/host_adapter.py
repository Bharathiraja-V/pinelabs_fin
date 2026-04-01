from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import nowdate, nowtime


class BaseHostAdapter:
	"""Minimal adapter contract for host application integration."""

	def get_order_reference(self, transaction_log):
		raise NotImplementedError

	def get_order_doc(self, order_id, doctype=None):
		raise NotImplementedError

	def is_order_paid(self, order_doc):
		raise NotImplementedError

	def is_order_finalized(self, order_doc):
		raise NotImplementedError

	def payment_exists(self, transaction_reference):
		raise NotImplementedError

	def finalize_transaction(self, transaction_log):
		raise NotImplementedError


class ERPNextAdapter(BaseHostAdapter):
	"""ERPNext/Frappe adapter implementation."""

	def get_order_reference(self, transaction_log):
		doctype = transaction_log.source_doctype or "POS Invoice"
		name = (
			transaction_log.source_docname
			or transaction_log.external_order_id
			or transaction_log.invoice
		)
		return doctype, name

	def get_order_doc(self, order_id, doctype=None):
		if doctype and frappe.db.exists(doctype, order_id):
			return frappe.get_doc(doctype, order_id)
		if frappe.db.exists("POS Invoice", order_id):
			return frappe.get_doc("POS Invoice", order_id)
		if frappe.db.exists("Sales Invoice", order_id):
			return frappe.get_doc("Sales Invoice", order_id)
		return None

	def is_order_paid(self, order_doc):
		return order_doc.docstatus == 1 and (order_doc.outstanding_amount or 0) <= 0

	def is_order_finalized(self, order_doc):
		return bool(order_doc and order_doc.docstatus == 1)

	def payment_exists(self, transaction_reference):
		return frappe.db.exists("Payment Entry", {"reference_no": transaction_reference})

	def finalize_transaction(self, transaction_log):
		doctype, order_id = self.get_order_reference(transaction_log)
		if not order_id:
			return {
				"success": False,
				"invoice_name": None,
				"invoice_doctype": doctype,
				"invoice_finalized": False,
				"finalization_status": "failed",
				"error": _("Missing source document reference on transaction log."),
			}

		order_doc = self.get_order_doc(order_id, doctype=doctype)
		if not order_doc:
			return {
				"success": False,
				"invoice_name": order_id,
				"invoice_doctype": doctype,
				"invoice_finalized": False,
				"finalization_status": "failed",
				"error": _("Source document {0} was not found.").format(order_id),
			}

		if (transaction_log.status or "") != "Approved":
			return {
				"success": False,
				"invoice_name": order_doc.name,
				"invoice_doctype": order_doc.doctype,
				"invoice_finalized": self.is_order_finalized(order_doc),
				"finalization_status": "skipped",
				"error": _("Cannot finalize invoice while transaction status is {0}.").format(
					transaction_log.status or _("Unknown")
				),
			}

		if self.is_order_finalized(order_doc):
			return {
				"success": True,
				"invoice_name": order_doc.name,
				"invoice_doctype": order_doc.doctype,
				"invoice_finalized": True,
				"finalization_status": "already_finalized",
				"message": _("Invoice is already finalized."),
			}

		source_system = (transaction_log.source_system or "").strip().lower()
		if source_system == "fin_pos":
			return self._finalize_fin_pos_invoice(order_doc)

		return {
			"success": False,
			"invoice_name": order_doc.name,
			"invoice_doctype": order_doc.doctype,
			"invoice_finalized": False,
			"finalization_status": "failed",
			"error": _("Unsupported source system: {0}.").format(
				transaction_log.source_system or _("Unknown")
			),
		}

	def _finalize_fin_pos_invoice(self, order_doc):
		try:
			if order_doc.docstatus == 0:
				if order_doc.meta.has_field("posting_date"):
					order_doc.posting_date = nowdate()
				if order_doc.meta.has_field("posting_time"):
					order_doc.posting_time = nowtime()
				if order_doc.meta.has_field("set_posting_time"):
					order_doc.set_posting_time = 1
			order_doc.flags.ignore_permissions = True
			order_doc.submit()
			refreshed_doc = self.get_order_doc(order_doc.name, doctype=order_doc.doctype)
			is_finalized = self.is_order_finalized(refreshed_doc)
			return {
				"success": bool(is_finalized),
				"invoice_name": refreshed_doc.name if refreshed_doc else order_doc.name,
				"invoice_doctype": refreshed_doc.doctype if refreshed_doc else order_doc.doctype,
				"invoice_finalized": bool(is_finalized),
				"finalization_status": "finalized" if is_finalized else "failed",
				"message": _("Invoice finalized successfully."),
				"error": None if is_finalized else _("Invoice was not finalized by FIN POS."),
			}
		except Exception as exc:
			refreshed_doc = self.get_order_doc(order_doc.name, doctype=order_doc.doctype)
			if refreshed_doc and self.is_order_finalized(refreshed_doc):
				return {
					"success": True,
					"invoice_name": refreshed_doc.name,
					"invoice_doctype": refreshed_doc.doctype,
					"invoice_finalized": True,
					"finalization_status": "already_finalized",
					"message": _("Invoice is already finalized."),
				}
			frappe.log_error(frappe.get_traceback(), "Pine Labs FIN POS Finalization Error")
			return {
				"success": False,
				"invoice_name": order_doc.name,
				"invoice_doctype": order_doc.doctype,
				"invoice_finalized": False,
				"finalization_status": "failed",
				"error": str(exc),
			}


def get_host_adapter():
	"""
	Return host adapter.
	Defaults to ERPNext adapter to keep backward compatibility.
	"""
	return ERPNextAdapter()
