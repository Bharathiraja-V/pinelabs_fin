# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Scrub gateway credentials already persisted on Pinelabs Transaction rows.

Before this release the terminal ``SecurityToken`` was saved in cleartext in
``request_payload`` / ``response_payload`` and copied into ``tabVersion`` by
``track_changes``. New writes are now redacted at the source
(``transaction._to_json_str``); this patch masks what is already stored.

Rotate the affected tokens as well — a scrubbed row does not un-leak a
backup taken earlier.
"""

import re

import frappe

MASK = "***REDACTED***"

# Matches  "SecurityToken": "value"  and its backslash-escaped form inside
# the JSON blob that tabVersion.data stores.
_SECRET_VALUE = re.compile(
	r'((?:SecurityToken|client_secret|access_token|refresh_token)\\*"\s*:\s*\\*")[^"\\]*',
	re.IGNORECASE,
)


def _scrub(text):
	return _SECRET_VALUE.sub(lambda m: m.group(1) + MASK, text)


def execute():
	if frappe.db.has_table("Pinelabs Transaction"):
		for field in ("request_payload", "response_payload"):
			rows = frappe.db.sql(
				f"select name, `{field}` from `tabPinelabs Transaction` where `{field}` like %s",  # nosec
				("%SecurityToken%",),
			)
			for name, value in rows:
				cleaned = _scrub(value or "")
				if cleaned != value:
					frappe.db.set_value("Pinelabs Transaction", name, field, cleaned, update_modified=False)

	rows = frappe.db.sql(
		"select name, data from `tabVersion` where ref_doctype = %s and data like %s",
		("Pinelabs Transaction", "%SecurityToken%"),
	)
	for name, data in rows:
		cleaned = _scrub(data or "")
		if cleaned != data:
			frappe.db.set_value("Version", name, "data", cleaned, update_modified=False)
