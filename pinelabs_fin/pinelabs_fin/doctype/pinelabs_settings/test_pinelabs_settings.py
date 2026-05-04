# Copyright (c) 2026, pine and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestPinelabsSettings(FrappeTestCase):
	def test_settings_singleton(self):
		"""Test that Pinelabs Settings is a singleton and fields exist"""
		settings = frappe.get_single("Pinelabs Settings")
		self.assertEqual(settings.doctype, "Pinelabs Settings")
		
		# Check core fields exist (will raise AttributeError if they don't)
		_ = settings.enabled
		_ = settings.verify_ssl
		_ = settings.auto_cancel_duration_minutes

	def test_enabled_check(self):
		"""Test the enabled check logic (if used in controllers)"""
		settings = frappe.get_single("Pinelabs Settings")
		old_enabled = settings.enabled
		
		try:
			settings.enabled = 1
			settings.save()
			self.assertEqual(frappe.get_single("Pinelabs Settings").enabled, 1)
			
			settings.enabled = 0
			settings.save()
			self.assertEqual(frappe.get_single("Pinelabs Settings").enabled, 0)
		finally:
			settings.enabled = old_enabled
			settings.save()
