# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Remove the legacy Pine Labs custom fields from Mode of Payment.

The integration used to identify Pine Labs Modes of Payment via four
custom fields (``pinelabs_enabled`` / ``pinelabs_flow_type`` /
``pinelabs_terminal_code`` / ``pinelabs_plural_method``) plus a
``pinelabs_section`` divider. We now identify the three canonical
modes by **name** only — no customisation on ``Mode of Payment``.

This patch drops the five custom fields from sites that were installed
under the older scheme. Idempotent — missing rows are skipped.
"""

import frappe

CUSTOM_FIELDS_TO_REMOVE = [
	"pinelabs_section",
	"pinelabs_enabled",
	"pinelabs_flow_type",
	"pinelabs_terminal_code",
	"pinelabs_plural_method",
]


def execute():
	for fieldname in CUSTOM_FIELDS_TO_REMOVE:
		existing = frappe.db.get_value(
			"Custom Field",
			{"dt": "Mode of Payment", "fieldname": fieldname},
			"name",
		)
		if existing:
			frappe.delete_doc(
				"Custom Field",
				existing,
				force=True,
				ignore_permissions=True,
			)
	frappe.db.commit()
