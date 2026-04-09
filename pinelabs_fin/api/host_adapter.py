from __future__ import unicode_literals

import frappe
from frappe import _


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

		if not self.is_order_finalized(order_doc):
			return {
				"success": False,
				"invoice_name": order_doc.name,
				"invoice_doctype": order_doc.doctype,
				"invoice_finalized": False,
				"finalization_status": "invoice_not_submitted",
				"error": _("Invoice must be submitted before payment can be recorded."),
			}

		if self.is_order_paid(order_doc):
			return {
				"success": True,
				"invoice_name": order_doc.name,
				"invoice_doctype": order_doc.doctype,
				"invoice_finalized": True,
				"finalization_status": "already_finalized",
				"message": _("Invoice is already fully paid."),
			}

		# Create Payment Entry via payment_bridge
		from pinelabs_fin.api.payment_bridge import create_payment_entry

		try:
			result = create_payment_entry(transaction_log.name)
			if result.get("success"):
				return {
					"success": True,
					"invoice_name": order_doc.name,
					"invoice_doctype": order_doc.doctype,
					"invoice_finalized": True,
					"finalization_status": "finalized",
					"payment_entry": result.get("payment_entry"),
					"message": result.get("message"),
				}
			return {
				"success": False,
				"invoice_name": order_doc.name,
				"invoice_doctype": order_doc.doctype,
				"invoice_finalized": True,
				"finalization_status": "failed",
				"error": result.get("error"),
			}
		except Exception as exc:
			frappe.log_error(frappe.get_traceback(), "PineLabs Finalization Error")
			return {
				"success": False,
				"invoice_name": order_doc.name,
				"invoice_doctype": order_doc.doctype,
				"invoice_finalized": True,
				"finalization_status": "failed",
				"error": str(exc),
			}


def get_host_adapter():
	"""
	Return host adapter.
	Defaults to ERPNext adapter to keep backward compatibility.
	"""
	return ERPNextAdapter()
