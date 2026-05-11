# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Pinelabs Payable Doctype — child of Pinelabs Settings.

One row per source doctype that should show the Pine Labs payment
buttons. Carries: button label, eligibility (docstatus + amount field),
how to finalise on success (Payment Entry vs mapped status field), and
the Customer Contact Field Mapping used by ``api/customer_contact.py``.

Validation lives on ``PinelabsSettings.validate`` because Frappe doesn't
call child controllers' ``validate()`` during the parent's save.
"""

from frappe.model.document import Document


class PinelabsPayableDoctype(Document):
	pass
