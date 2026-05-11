# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Pinelabs Transaction — single source of truth for one payment attempt.

Polymorphic via ``reference_doctype`` / ``reference_name`` Dynamic Link.
Carries the flow type (Terminal / Plural), payment method
(CARD / UPI / NB / WALLET), the linked Pine Labs identifiers, and the
created Payment Entry on success.

This controller only enforces the **state machine** (allowed transitions
between INITIATED / PENDING / SUCCESS / FAILED / CANCELLED) — the actual
transitions and Payment Entry creation are driven by ``api/transaction.py``,
which sets ``flags.pinelabs_internal_transition = True`` on legitimate
saves. User-driven status edits without that flag are blocked here.
"""

import frappe
from frappe import _
from frappe.model.document import Document

# Allowed forward transitions. Anything not listed is rejected.
_ALLOWED_TRANSITIONS = {
	"INITIATED": {"PENDING", "SUCCESS", "FAILED", "CANCELLED"},
	"PENDING": {"SUCCESS", "FAILED", "CANCELLED"},
	"SUCCESS": set(),  # terminal
	"FAILED": set(),    # terminal
	"CANCELLED": set(), # terminal
}


class PinelabsTransaction(Document):
	"""Single source of truth for every Pine Labs payment attempt.

	Status is a state machine; flow_type is locked after creation so that a
	Terminal transaction can never be silently re-tagged as Plural and vice
	versa. All inserts/updates go through this controller, which enforces the
	rules. Direct DB writes from outside the controller bypass the rules and
	should be avoided.
	"""

	def validate(self):
		self._lock_flow_type()
		self._enforce_status_transition()
		self._validate_machine()

	def _validate_machine(self):
		"""``machine`` is a Data field holding a Pinelabs Machine Config row's
		``machine_name`` (Machine Config is a child table, so a Link field
		can't validate by name). Verify the value resolves to an actual
		configured machine when present.
		"""
		if not self.machine:
			return
		if self.flow_type != "Terminal":
			return
		exists = frappe.db.exists(
			"Pinelabs Machine Config",
			{"machine_name": self.machine, "parenttype": "Pinelabs Settings"},
		)
		if not exists:
			frappe.throw(
				_("Machine {0} is not configured in Pinelabs Settings. Add it under Pinelabs Settings → Machines first.").format(
					self.machine
				),
			)

	def before_insert(self):
		# A brand-new transaction must start in INITIATED.
		if not self.status:
			self.status = "INITIATED"
		if self.status != "INITIATED":
			frappe.throw(
				_("New Pinelabs Transaction must start with status INITIATED, got {0}.").format(self.status),
			)

	def on_trash(self):
		if self.payment_entry:
			frappe.throw(
				_("Cannot delete Pinelabs Transaction {0}: linked to Payment Entry {1}.").format(
					self.name, self.payment_entry
				)
			)

	def _lock_flow_type(self):
		"""Disallow editing flow_type after the row is created."""
		if self.is_new():
			return
		previous = self.get_doc_before_save()
		if previous and previous.flow_type and previous.flow_type != self.flow_type:
			frappe.throw(
				_("flow_type is locked after creation (was {0}, attempted {1}).").format(
					previous.flow_type, self.flow_type
				)
			)

	def _enforce_status_transition(self):
		"""Reject illegal status transitions and manual jumps to SUCCESS."""
		if self.is_new():
			return
		previous = self.get_doc_before_save()
		if not previous:
			return

		old_status = previous.status or "INITIATED"
		new_status = self.status or "INITIATED"
		if old_status == new_status:
			return

		# Block manual UI edits to SUCCESS — only the transaction service may set it.
		if new_status == "SUCCESS" and not self.flags.get("pinelabs_internal_transition"):
			frappe.throw(
				_("Status SUCCESS may only be set by the Pinelabs transaction service, not manually."),
			)

		if new_status not in _ALLOWED_TRANSITIONS.get(old_status, set()):
			frappe.throw(
				_("Illegal status transition {0} -> {1} for transaction {2}.").format(
					old_status, new_status, self.name
				)
			)
