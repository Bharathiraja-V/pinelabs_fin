# Copyright (c) 2026, pine and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestPinelabsTransactionLog(FrappeTestCase):
	def test_log_creation(self):
		"""Test that a transaction log can be created with basic fields"""
		log = frappe.get_doc({
			"doctype": "Pinelabs Transaction Log",
			"transaction_reference": "TEST-REF-123",
			"status": "Pending",
			"amount": 100.0,
			"payment_mode": "Card"
		})
		log.insert()
		
		self.assertTrue(frappe.db.exists("Pinelabs Transaction Log", log.name))
		stored_log = frappe.get_doc("Pinelabs Transaction Log", log.name)
		self.assertEqual(stored_log.transaction_reference, "TEST-REF-123")
		self.assertEqual(stored_log.status, "Pending")
		
		# Cleanup
		log.delete()
