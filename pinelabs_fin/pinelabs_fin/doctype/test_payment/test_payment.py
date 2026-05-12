# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Test Payment — a tiny custom doctype used by the Pinelabs Test Payment page
to exercise the terminal payment API from a custom button (not from the
auto-injected buttons on Sales Invoice).

Submittable so the API's "must be submitted" eligibility check applies.
The app writes payment_status / payment_method / pinelabs_transaction /
paid_on after a successful payment via the mapped-finalization path in
``api/transaction.py`` (configured by the Payable Documents row for this
doctype, with ``finalize_via_payment_entry = 0``).
"""

from frappe.model.document import Document


class TestPayment(Document):
	pass
