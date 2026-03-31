from __future__ import unicode_literals

import frappe


class BaseHostAdapter:
	"""Minimal adapter contract for host application integration."""

	def get_order_doc(self, order_id):
		raise NotImplementedError

	def is_order_paid(self, order_doc):
		raise NotImplementedError

	def payment_exists(self, transaction_reference):
		raise NotImplementedError


class ERPNextAdapter(BaseHostAdapter):
	"""ERPNext/Frappe adapter implementation."""

	def get_order_doc(self, order_id):
		if frappe.db.exists("POS Invoice", order_id):
			return frappe.get_doc("POS Invoice", order_id)
		if frappe.db.exists("Sales Invoice", order_id):
			return frappe.get_doc("Sales Invoice", order_id)
		return None

	def is_order_paid(self, order_doc):
		return order_doc.docstatus == 1 and (order_doc.outstanding_amount or 0) <= 0

	def payment_exists(self, transaction_reference):
		return frappe.db.exists("Payment Entry", {"reference_no": transaction_reference})


def get_host_adapter():
	"""
	Return host adapter.
	Defaults to ERPNext adapter to keep backward compatibility.
	"""
	return ERPNextAdapter()
