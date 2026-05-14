# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Install patch (idempotent).

Seeds the three canonical PineLabs Mode of Payment rows:

- ``PineLabs - Card``         — terminal flow (physical device, card)
- ``PineLabs - UPI``          — terminal flow (physical device, UPI)
- ``PineLabs - Payment Link`` — Plural hosted link (one row covers every
                                  method the customer can pick on Plural's
                                  page: card / UPI / NB / wallet)

The app identifies Pine Labs modes by **name** (see
``PINELABS_TERMINAL_MODES`` and ``PINELABS_PLURAL_MODE`` in
``api/transaction.py``). No custom fields are added to the standard
ERPNext ``Mode of Payment`` doctype — the integration runs against a
zero-customisation Mode of Payment.

Each row needs a default account configured per company under
``Mode of Payment → Accounts`` before the first payment goes through;
the initiate-time pre-flight in ``api/transaction.py`` enforces this
and throws a clear error before the customer is asked to pay.
"""

import frappe

SEED_MODES = [
	{"mode_of_payment": "PineLabs - Card", "type": "Bank"},
	{"mode_of_payment": "PineLabs - UPI", "type": "Bank"},
	{"mode_of_payment": "PineLabs - Payment Link", "type": "Bank"},
]


def execute():
	for entry in SEED_MODES:
		_ensure_mode(entry)
	frappe.db.commit()


def _ensure_mode(entry):
	"""Create the Mode of Payment row if missing. Idempotent."""
	name = entry["mode_of_payment"]
	if frappe.db.exists("Mode of Payment", name):
		return
	doc = frappe.new_doc("Mode of Payment")
	doc.mode_of_payment = name
	doc.type = entry["type"]
	doc.enabled = 1
	doc.insert(ignore_permissions=True)
