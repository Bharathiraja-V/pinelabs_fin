# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Install patch (idempotent).

Adds the four PineLabs custom fields to the standard ERPNext `Mode of Payment`
doctype and seeds the four canonical PineLabs Mode of Payment records.

Mode of Payment is the new source of truth for "which Pine Labs payment
methods exist and how they route" — Settings no longer carries that list.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

PINELABS_MOP_FIELDS = {
	"Mode of Payment": [
		{
			"fieldname": "pinelabs_section",
			"fieldtype": "Section Break",
			"label": "Pine Labs Integration",
			"insert_after": "enabled",
			"collapsible": 1,
		},
		{
			"fieldname": "pinelabs_enabled",
			"fieldtype": "Check",
			"label": "Pine Labs Enabled",
			"description": "Tick if this Mode of Payment is offered by the Pine Labs integration.",
			"insert_after": "pinelabs_section",
		},
		{
			"fieldname": "pinelabs_flow_type",
			"fieldtype": "Select",
			"label": "Pine Labs Flow Type",
			"description": "Terminal = physical POS device only. Plural = payment link only. Both = available in either flow.",
			"options": "\nTerminal\nPlural\nBoth",
			"depends_on": "pinelabs_enabled",
			"insert_after": "pinelabs_enabled",
		},
		{
			"fieldname": "pinelabs_terminal_code",
			"fieldtype": "Int",
			"label": "Pine Labs Terminal Code",
			"description": "Pine Labs Cloud API allowed_payment_mode integer (1 = Card, 11 = UPI). Required for Terminal/Both flow.",
			"depends_on": "eval:doc.pinelabs_flow_type==\"Terminal\" || doc.pinelabs_flow_type==\"Both\"",
			"insert_after": "pinelabs_flow_type",
		},
		{
			"fieldname": "pinelabs_plural_method",
			"fieldtype": "Data",
			"label": "Pine Labs Plural Method",
			"description": "Plural API allowed_payment_methods string (CARD / UPI / NB / WALLET). Required for Plural/Both flow.",
			"depends_on": "eval:doc.pinelabs_flow_type==\"Plural\" || doc.pinelabs_flow_type==\"Both\"",
			"insert_after": "pinelabs_terminal_code",
		},
	]
}


SEED_MODES = [
	{
		"mode_of_payment": "PineLabs - Card",
		"type": "Bank",
		"flow_type": "Both",
		"terminal_code": 1,
		"plural_method": "CARD",
	},
	{
		"mode_of_payment": "PineLabs - UPI",
		"type": "Bank",
		"flow_type": "Both",
		"terminal_code": 11,
		"plural_method": "UPI",
	},
	{
		# Plural-only: terminal_code = 0 means "not applicable"; the
		# underlying column is NOT NULL so we cannot store None directly.
		"mode_of_payment": "PineLabs - NetBanking",
		"type": "Bank",
		"flow_type": "Plural",
		"terminal_code": 0,
		"plural_method": "NB",
	},
	{
		"mode_of_payment": "PineLabs - Wallet",
		"type": "Bank",
		"flow_type": "Plural",
		"terminal_code": 0,
		"plural_method": "WALLET",
	},
]


def execute():
	create_custom_fields(PINELABS_MOP_FIELDS, ignore_validate=True, update=True)

	for entry in SEED_MODES:
		_ensure_mode(entry)

	frappe.db.commit()


def _ensure_mode(entry):
	"""Create the Mode of Payment if missing, then sync the PineLabs fields."""
	name = entry["mode_of_payment"]
	if not frappe.db.exists("Mode of Payment", name):
		doc = frappe.new_doc("Mode of Payment")
		doc.mode_of_payment = name
		doc.type = entry["type"]
		doc.enabled = 1
		doc.insert(ignore_permissions=True)

	# Always (re-)write the PineLabs fields so reinstalls/upgrades self-heal.
	updates = {
		"pinelabs_enabled": 1,
		"pinelabs_flow_type": entry["flow_type"],
		"pinelabs_terminal_code": entry.get("terminal_code"),
		"pinelabs_plural_method": entry.get("plural_method"),
	}
	frappe.db.set_value("Mode of Payment", name, updates, update_modified=False)
