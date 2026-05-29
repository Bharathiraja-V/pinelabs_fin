"""Idempotent patch to set up PineLabs Mode of Payment records and populate
the Pinelabs Settings payment_modes child table.

Marketplace-safe:
  - Skips creation if a Mode of Payment with the target name already exists.
  - Skips child table population if at least one row already exists.
  - Does NOT link Mode of Payment to a bank account (company-specific; admin task).
"""

import frappe


DEFAULT_MODES = [
	{
		"mode_of_payment": "PineLabs - Card",
		"type": "Bank",
		"label": "Card",
		"allowed_payment_mode": 1,
		"is_enabled": 1,
	},
	{
		"mode_of_payment": "PineLabs - UPI",
		"type": "Bank",
		"label": "UPI",
		"allowed_payment_mode": 11,
		"is_enabled": 1,
	},
]


def execute():
	for entry in DEFAULT_MODES:
		_ensure_mode_of_payment(entry["mode_of_payment"], entry["type"])

	_populate_settings_payment_modes()
	frappe.db.commit()


def _ensure_mode_of_payment(name, mop_type):
	if frappe.db.exists("Mode of Payment", name):
		return
	doc = frappe.new_doc("Mode of Payment")
	doc.mode_of_payment = name
	doc.type = mop_type
	doc.enabled = 1
	doc.insert(ignore_permissions=True)


def _populate_settings_payment_modes():
	if not frappe.db.exists("DocType", "Pinelabs Settings"):
		return

	settings = frappe.get_single("Pinelabs Settings")
	if settings.payment_modes and len(settings.payment_modes) > 0:
		return

	for entry in DEFAULT_MODES:
		if not frappe.db.exists("Mode of Payment", entry["mode_of_payment"]):
			continue
		settings.append("payment_modes", {
			"mode_of_payment": entry["mode_of_payment"],
			"label": entry["label"],
			"allowed_payment_mode": entry["allowed_payment_mode"],
			"is_enabled": entry.get("is_enabled", 1),
		})

	settings.save(ignore_permissions=True)
