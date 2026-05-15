# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Cross-flow conflict prevention.

Blocks a manual ERPNext Payment Entry from being inserted while a Pine
Labs payment (Pinelabs Transaction in INITIATED / PENDING state) is in
flight for the same source document. Pairs with the finalize-time guard
in ``api/transaction._create_payment_entry_idempotent`` so the two
flows can never silently double-record the same invoice.

Wired into the framework via ``hooks.doc_events`` on Payment Entry's
``validate`` event.
"""

import frappe
from frappe import _


def refuse_if_pinelabs_active(doc, method=None):
	"""Throw when an in-flight Pinelabs Transaction conflicts with this PE.

	Our own Payment Entries set ``flags.pinelabs_internal_pe = True``
	before ``insert()`` so this check returns immediately for them. A PE
	created by ERPNext UI / a Server Script / any other path has no
	such flag and is checked against the active-transaction set.

	The check is doctype-agnostic — any reference with an active Pinelabs
	Transaction blocks the PE, including custom doctypes that finalize via
	status-field mapping (where the dual-recording window is otherwise
	exactly the same as on PE-native doctypes).
	"""
	# Our own PE — created by api/transaction.py's mark_success. Allow.
	if doc.flags.get("pinelabs_internal_pe"):
		return

	# Walk references; block if ANY one has an active Pinelabs Transaction.
	for ref in (doc.references or []):
		if not (ref.reference_doctype and ref.reference_name):
			continue
		active = frappe.db.get_value(
			"Pinelabs Transaction",
			{
				"reference_doctype": ref.reference_doctype,
				"reference_name": ref.reference_name,
				"status": ("in", ("INITIATED", "PENDING")),
			},
			["name", "flow_type"],
			as_dict=True,
		)
		if active:
			frappe.throw(
				_(
					"A Pine Labs {0} payment ({1}) is currently in progress for "
					"{2} {3}. Wait for it to finish, or cancel that Pinelabs "
					"Transaction first, before recording a manual Payment Entry."
				).format(
					active.flow_type,
					active.name,
					ref.reference_doctype,
					ref.reference_name,
				),
			)
