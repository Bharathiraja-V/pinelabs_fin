# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Install hooks.

``after_install`` seeds the three canonical PineLabs Mode of Payment rows.

Why this lives here and not only in a patch: on a *fresh* ``bench
install-app``, Frappe records every ``patches.txt`` entry in the Patch
Log as already-run **without executing it** (patches exist to migrate
pre-existing data; a fresh install has none). So seed data that must
exist after a clean install belongs in ``after_install``, which runs on
every fresh install. The patch ``pinelabs_fin.patches.install_pinelabs_modes``
calls the same function so sites that *upgrade* from an older version
(where this seed never ran) also get the rows.
"""

import frappe

# The three canonical Pine Labs Mode of Payment rows. Kept in sync with
# PINELABS_TERMINAL_MODES / PINELABS_PLURAL_MODE in api/transaction.py —
# the app identifies its modes by these exact names.
SEED_MODES = [
	{"mode_of_payment": "PineLabs - Card", "type": "Bank"},
	{"mode_of_payment": "PineLabs - UPI", "type": "Bank"},
	{"mode_of_payment": "PineLabs - Payment Link", "type": "Bank"},
]


def after_install():
	"""Frappe ``after_install`` hook — runs once on a fresh install-app."""
	seed_pinelabs_modes_of_payment()


def seed_pinelabs_modes_of_payment():
	"""Create the three canonical Mode of Payment rows. Idempotent.

	Shared by ``after_install`` (fresh installs) and the
	``install_pinelabs_modes`` patch (upgrades).
	"""
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
