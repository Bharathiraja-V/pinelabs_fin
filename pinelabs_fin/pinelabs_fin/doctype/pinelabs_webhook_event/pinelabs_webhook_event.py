# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Pinelabs Webhook Event — DB-level dedup log for inbound Plural webhooks.

One row per webhook delivery received. `event_id` carries a UNIQUE constraint
so a redelivery of the same event by Plural is rejected by the database
before any business logic runs. Read-only from the desk; webhook.py is the
only writer.
"""

from frappe.model.document import Document


class PinelabsWebhookEvent(Document):
	pass
