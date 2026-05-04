# Copyright (c) 2026, pine and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model import default_fields
from frappe.model.document import Document
from frappe.utils import cint


PAYMENT_ENTRY_SOURCE_DOCTYPES = (
	"Sales Invoice",
	"POS Invoice",
)
SHOW_ON_DOCSTATUS_OPTIONS = ("0", "1", "0,1")
NUMERIC_FIELD_TYPES = ("Currency", "Float", "Int", "Percent")


class PinelabsSettings(Document):
	def validate(self):
		self.validate_payable_doctypes()

	def validate_payable_doctypes(self):
		for row in self.get("payable_doctypes", []) or []:
			if not cint(row.is_enabled):
				continue

			self._validate_payable_doctype_row(row)

	def _validate_payable_doctype_row(self, row):
		if not row.doctype_name:
			frappe.throw(_("Doctype is required in Payable Doctypes row {0}.").format(row.idx))

		self._validate_payable_source_doctype(row)

		if row.show_on_docstatus not in SHOW_ON_DOCSTATUS_OPTIONS:
			frappe.throw(
				_("Show on docstatus must be one of {0} in Payable Doctypes row {1}.").format(
					", ".join(SHOW_ON_DOCSTATUS_OPTIONS),
					row.idx,
				)
			)

		if not row.amount_field:
			frappe.throw(_("Amount field is required in Payable Doctypes row {0}.").format(row.idx))

		amount_df = self._get_source_field(row.doctype_name, row.amount_field)
		if amount_df.fieldtype not in NUMERIC_FIELD_TYPES:
			frappe.throw(
				_("Amount field '{0}' in Payable Doctypes row {1} must be a numeric field.").format(
					row.amount_field,
					row.idx,
				)
			)

		if row.status_field:
			self._get_source_field(row.doctype_name, row.status_field)

		if row.link_payment_entry_field:
			self._get_source_field(row.doctype_name, row.link_payment_entry_field)

		if cint(row.finalize_via_payment_entry) == 1:
			if row.doctype_name not in PAYMENT_ENTRY_SOURCE_DOCTYPES:
				frappe.throw(
					_(
						"Create Payment Entry is only supported for {0} in Payable Doctypes row {1}."
					).format(
						", ".join(PAYMENT_ENTRY_SOURCE_DOCTYPES),
						row.idx,
					)
				)
			return

		if not row.status_field:
			frappe.throw(
				_(
					"Status field is required when Create Payment Entry is disabled in Payable Doctypes row {0}."
				).format(row.idx)
			)

		if not row.paid_status_value:
			frappe.throw(
				_(
					"Paid status value is required when Create Payment Entry is disabled in Payable Doctypes row {0}."
				).format(row.idx)
			)

	def _validate_payable_source_doctype(self, row):
		if not frappe.db.exists("DocType", row.doctype_name):
			frappe.throw(_("Doctype '{0}' does not exist.").format(row.doctype_name))

		doctype_doc = frappe.get_doc("DocType", row.doctype_name)
		for fieldname, expected_value in (("istable", 0), ("issingle", 0)):
			if getattr(doctype_doc, fieldname, 0) != expected_value:
				frappe.throw(
					_("Doctype '{0}' cannot be used in Payable Doctypes because {1} is enabled.").format(
						row.doctype_name,
						fieldname,
					)
				)

	def _get_source_field(self, doctype, fieldname):
		meta = frappe.get_meta(doctype)
		df = meta.get_field(fieldname)

		if df:
			return df

		if fieldname in default_fields:
			return frappe._dict({"fieldname": fieldname, "fieldtype": "Data"})

		frappe.throw(_("Field '{0}' does not exist on doctype '{1}'.").format(fieldname, doctype))
