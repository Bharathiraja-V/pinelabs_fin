# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Pinelabs Machine Mapping — child of Pinelabs Settings.

A routing rule: when context X (User / Warehouse / Company / POS Profile)
matches reference_name Y, route the terminal payment to machine Z. When
multiple rows match the current context, the alphabetically-first machine
name wins. Consumed by ``pinelabs_settings.resolve_machine_for_context``
when Terminal Routing Mode is "Mapping".

The ``before_validate`` hook keeps reference_doctype in sync with
mapping_type so Frappe's Dynamic Link validation passes — that runs
before any controller hook on child rows.
"""

import frappe
from frappe import _
from frappe.model.document import Document

# Server-side mirror of the form-JS auto-set rule. Both layers enforce it so
# scripted/API row creation can't sneak in a mismatched (mapping_type,
# reference_doctype) pair.
_MAPPING_TYPE_TO_DOCTYPE = {
	"User": "User",
	"Warehouse": "Warehouse",
	"Company": "Company",
	"POS Profile": "POS Profile",
}


class PinelabsMachineMapping(Document):
	def before_validate(self):
		# Run before Frappe's Dynamic Link validation so `reference_name`
		# resolves against the correct doctype.
		self._sync_reference_doctype()

	def validate(self):
		self._validate_reference_exists()
		self._validate_machine_exists()
		self._warn_on_duplicate()

	def _validate_machine_exists(self):
		"""``machine`` is a Data field holding a Pinelabs Machine Config row's
		``machine_name`` (Machine Config is a child table, so we can't use a
		Link). Verify the name resolves to an actual configured machine.
		"""
		if not self.machine:
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

	def _sync_reference_doctype(self):
		expected = _MAPPING_TYPE_TO_DOCTYPE.get(self.mapping_type)
		if not expected:
			frappe.throw(
				_("Unknown mapping_type {0}.").format(self.mapping_type),
			)
		if self.reference_doctype and self.reference_doctype != expected:
			frappe.throw(
				_("reference_doctype must be {0} for mapping_type {1} (got {2}).").format(
					expected, self.mapping_type, self.reference_doctype
				),
			)
		# Auto-set so API callers don't have to remember.
		self.reference_doctype = expected

	def _validate_reference_exists(self):
		if not (self.reference_doctype and self.reference_name):
			return
		if not frappe.db.exists(self.reference_doctype, self.reference_name):
			frappe.throw(
				_("{0} {1} does not exist.").format(self.reference_doctype, self.reference_name),
			)

	def _warn_on_duplicate(self):
		"""Soft check: another row in the same parent with identical
		(machine, mapping_type, reference_name).

		Doesn't reject — having two enabled rows for the same key is silly
		but not destructive (the alphabetical tie-break on machine name
		still gives a deterministic result). msgprint as a nudge.
		"""
		existing = frappe.db.get_value(
			"Pinelabs Machine Mapping",
			{
				"parent": self.parent or "",
				"parenttype": self.parenttype or "",
				"parentfield": self.parentfield or "",
				"machine": self.machine,
				"mapping_type": self.mapping_type,
				"reference_name": self.reference_name,
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if existing:
			frappe.msgprint(
				_("Another mapping already exists for the same machine + {0} + {1}.").format(
					self.mapping_type, self.reference_name
				),
				indicator="orange",
				alert=True,
			)
