# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Pinelabs Machine Config — child of Pinelabs Settings.

One row per physical Pine Labs terminal. Carries the credentials
(merchant_id, security_token, client_id, store_id) that
``PineLabsClient`` uses for the Pay-on-Machine flow. The ``is_default``
flag picks the row used in Single Machine routing mode; Mapping mode
selects rows via ``Pinelabs Machine Mapping``.
"""

from frappe.model.document import Document


class PinelabsMachineConfig(Document):
	pass
