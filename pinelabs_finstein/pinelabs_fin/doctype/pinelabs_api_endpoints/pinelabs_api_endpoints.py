# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Pinelabs API Endpoints — child of Pinelabs Settings.

Holds the Pine Labs Cloud terminal endpoint paths (upload / get-status /
cancel) plus the base API URL. The active row is consumed by
``api/pinelabs_client.PineLabsClient`` to build request URLs.
"""

from frappe.model.document import Document


class PinelabsAPIEndpoints(Document):
	pass
