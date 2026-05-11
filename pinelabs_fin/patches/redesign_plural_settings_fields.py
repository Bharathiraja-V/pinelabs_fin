# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Migrate Pinelabs Settings to the redesigned Plural section.

Old → New mapping:
  plural_use_sandbox (Check 0/1) → plural_environment (Select Sandbox/Production)
  plural_base_url (was the override) → plural_base_url_override
                                       (the slot keeps its name but its meaning
                                        flipped to "auto-resolved", so we move
                                        the user's old value to the new field
                                        and clear the slot.)

Pinelabs Settings is a Single, so values live in `tabSingles` rows
(doctype, field, value) — not as columns of a per-doctype table. The
migration is just row INSERTs and DELETEs.

Idempotent: skips rows already migrated, no-op on a fresh install.
"""

import frappe

DOCTYPE = "Pinelabs Settings"


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	values = _read_singles()

	# 1. plural_use_sandbox → plural_environment
	if "plural_environment" not in values and "plural_use_sandbox" in values:
		new_val = "Sandbox" if str(values["plural_use_sandbox"]).strip() in ("1", "true", "True") else "Production"
		_set_single_value("plural_environment", new_val)

	# 2. old plural_base_url (override) → plural_base_url_override
	if (
		"plural_base_url_override" not in values
		and values.get("plural_base_url")
	):
		_set_single_value("plural_base_url_override", values["plural_base_url"])

	# 3. Clear the plural_base_url slot — controller's auto-fill repopulates
	#    it on next save based on environment + override.
	_delete_single_value("plural_base_url")

	# 4. Drop the obsolete sandbox toggle row.
	_delete_single_value("plural_use_sandbox")

	frappe.db.commit()
	frappe.logger().info("Pinelabs migrate: redesigned Plural Settings fields.")


def _read_singles():
	rows = frappe.db.sql(
		"SELECT field, value FROM tabSingles WHERE doctype = %s",
		(DOCTYPE,),
		as_dict=True,
	)
	return {r["field"]: r["value"] for r in rows}


def _set_single_value(field, value):
	frappe.db.sql(
		"INSERT INTO tabSingles (doctype, field, value) VALUES (%s, %s, %s) "
		"ON DUPLICATE KEY UPDATE value = VALUES(value)",
		(DOCTYPE, field, value),
	)


def _delete_single_value(field):
	frappe.db.sql(
		"DELETE FROM tabSingles WHERE doctype = %s AND field = %s",
		(DOCTYPE, field),
	)
