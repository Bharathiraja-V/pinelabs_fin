from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import cint


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

	def _get_configured_field(self, doctype, fieldname):
		if not doctype or not fieldname:
			return None

		meta = frappe.get_meta(doctype)
		return meta.get_field(fieldname)

	def _should_submit_mapped_document(self, order_doc, config):
		if not order_doc or not config:
			return False

		if cint(getattr(order_doc, "docstatus", 0)) != 0:
			return False

		paid_status_value = str((config or {}).get("paid_status_value") or "").strip().lower()
		if paid_status_value != "submitted":
			return False

		meta = frappe.get_meta(order_doc.doctype)
		return bool(getattr(meta, "is_submittable", 0))

	def _get_mapped_link_value(self, order_doc, config, transaction_log):
		link_field = (config or {}).get("link_payment_entry_field")
		if not link_field or not transaction_log:
			return None

		df = self._get_configured_field(order_doc.doctype, link_field)
		if df and df.fieldtype == "Link" and df.options == "Payment Entry":
			return getattr(transaction_log, "payment_entry", None) or None

		return (
			getattr(transaction_log, "payment_entry", None)
			or getattr(transaction_log, "transaction_reference", None)
			or None
		)

	def _is_mapped_finalization_complete(self, order_doc, config, transaction_log):
		status_field = (config or {}).get("status_field")
		paid_status_value = (config or {}).get("paid_status_value")
		if not status_field or not paid_status_value:
			return False

		current_status = getattr(order_doc, status_field, None)
		if str(current_status or "").strip() != str(paid_status_value or "").strip():
			return False

		if self._should_submit_mapped_document(order_doc, config) and cint(getattr(order_doc, "docstatus", 0)) != 1:
			return False

		link_field = (config or {}).get("link_payment_entry_field")
		expected_link_value = self._get_mapped_link_value(order_doc, config, transaction_log)
		if link_field and expected_link_value is not None:
			current_link_value = getattr(order_doc, link_field, None)
			if str(current_link_value or "").strip() != str(expected_link_value or "").strip():
				return False

		return True

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
		from pinelabs_fin.api import services

		config = services.get_payable_doctype_config(order_doc.doctype)
		return services.is_doc_already_paid(order_doc, config=config)

	def is_order_finalized(self, order_doc):
		if not order_doc:
			return False

		from pinelabs_fin.api import services

		config = services.get_payable_doctype_config(order_doc.doctype)
		if config and cint(config.get("finalize_via_payment_entry")) != 1:
			return services.docstatus_matches_payable_config(order_doc, config)

		return bool(order_doc and order_doc.docstatus == 1)

	def payment_exists(self, transaction_reference):
		return frappe.db.exists("Payment Entry", {"reference_no": transaction_reference})

	def finalize_transaction(self, transaction_log):
		from pinelabs_fin.api import services

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

		config = services.get_payable_doctype_config(order_doc.doctype)

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

		if config and cint(config.get("finalize_via_payment_entry")) != 1:
			if self._is_mapped_finalization_complete(order_doc, config, transaction_log):
				return {
					"success": True,
					"invoice_name": order_doc.name,
					"invoice_doctype": order_doc.doctype,
					"invoice_finalized": True,
					"finalization_status": "already_finalized",
					"message": _("Invoice is already fully paid."),
				}

			result = self.finalize_mapped_fields(order_doc, config, transaction_log=transaction_log)
			if result.get("success"):
				return {
					"success": True,
					"invoice_name": order_doc.name,
					"invoice_doctype": order_doc.doctype,
					"invoice_finalized": True,
					"finalization_status": "fields_updated",
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

	def finalize_mapped_fields(self, order_doc, config, transaction_log=None):
		status_field = (config or {}).get("status_field")
		paid_status_value = (config or {}).get("paid_status_value")

		if not status_field or not paid_status_value:
			return {
				"success": False,
				"error": _("Status field and paid status value are required for mapped finalization."),
			}

		try:
			if self._should_submit_mapped_document(order_doc, config):
				order_doc.flags.ignore_permissions = True
				order_doc.submit()
				order_doc.reload()

			updates = {status_field: paid_status_value}
			link_field = (config or {}).get("link_payment_entry_field")
			link_value = self._get_mapped_link_value(order_doc, config, transaction_log)
			if link_field and link_value is not None:
				updates[link_field] = link_value

			frappe.db.set_value(
				order_doc.doctype,
				order_doc.name,
				updates,
				update_modified=True,
			)

			updated_fields = ", ".join(sorted(updates))
			return {
				"success": True,
				"message": _("Updated {0} on {1} {2}.").format(
					updated_fields,
					order_doc.doctype,
					order_doc.name,
				),
			}
		except Exception as exc:
			frappe.log_error(frappe.get_traceback(), "PineLabs Mapped Finalization Error")
			return {
				"success": False,
				"error": str(exc),
			}


def get_host_adapter():
	"""
	Return host adapter.
	Defaults to ERPNext adapter to keep backward compatibility.
	"""
	return ERPNextAdapter()
