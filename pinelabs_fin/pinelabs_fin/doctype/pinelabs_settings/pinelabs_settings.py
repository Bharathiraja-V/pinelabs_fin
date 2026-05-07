# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_url


_MIN_WEBHOOK_SECRET_LEN = 16


class PinelabsSettings(Document):
	def validate(self):
		self._auto_default_only_machine()
		self._validate_one_default_machine()
		self._validate_plural_credentials()
		self._validate_payable_doctypes()
		self._fill_webhook_url()

	def _auto_default_only_machine(self):
		"""When a single machine row exists, force is_default = 1 on it."""
		machines = self.machines or []
		if len(machines) == 1 and not cint(machines[0].is_default):
			machines[0].is_default = 1

	def _validate_one_default_machine(self):
		"""At most one machine may be default. Empty machines table is allowed."""
		defaults = [m for m in (self.machines or []) if cint(m.is_default)]
		if len(defaults) > 1:
			frappe.throw(
				_("Exactly one machine may be marked Is Default. Found {0}: {1}.").format(
					len(defaults),
					", ".join(m.machine_name or "(unnamed)" for m in defaults),
				)
			)

	def _validate_plural_credentials(self):
		"""Plural credentials and webhook secret are mandatory when payment links are enabled."""
		if not cint(self.enable_payment_links):
			return

		# Frappe's mandatory_depends_on takes care of the empty-field case for
		# plural_client_id / plural_client_secret / payment_link_webhook_secret;
		# we only enforce the secret-length rule here.
		secret = None
		try:
			secret = self.get_password("payment_link_webhook_secret", raise_exception=False)
		except Exception:
			secret = None
		if secret and len(secret) < _MIN_WEBHOOK_SECRET_LEN:
			frappe.throw(
				_("Payment Link Webhook Secret must be at least {0} characters for security.").format(
					_MIN_WEBHOOK_SECRET_LEN
				)
			)

	def _validate_payable_doctypes(self):
		"""Require amount_field, no duplicates, and mapped-status fields when
		Create Payment Entry is unchecked.
		"""
		seen = set()
		for row in (self.payable_doctypes or []):
			doctype = (row.doctype_name or "").strip()
			if not doctype:
				continue
			if doctype in seen:
				frappe.throw(_("Duplicate Payable Doctype row for {0}.").format(doctype))
			seen.add(doctype)
			if not (row.amount_field or "").strip():
				frappe.throw(_("Payable Doctype {0} requires an Amount Field.").format(doctype))

			# When PE creation is off, mapped-field updates must be configured.
			if not cint(getattr(row, "finalize_via_payment_entry", 1)):
				if not (row.status_field or "").strip() or not (row.paid_status_value or "").strip():
					frappe.throw(
						_("Payable Doctype {0}: with Create Payment Entry unchecked, both Status Field and Paid Status Value are required.").format(doctype)
					)

	def _fill_webhook_url(self):
		"""Populate the read-only webhook URL field from the site URL."""
		if not cint(self.enable_payment_links):
			return
		try:
			self.webhooks_url = get_url(
				"/api/method/pinelabs_fin.api.webhook.handle_webhook"
			)
		except Exception:
			# Outside a request context (e.g. patches) we leave whatever was there.
			pass


def get_default_machine_config():
	"""Return the default Pinelabs Machine Config row, or None if not configured."""
	settings = frappe.get_single("Pinelabs Settings")
	if not cint(settings.enabled):
		return None
	for machine in (settings.machines or []):
		if cint(machine.is_default) and cint(machine.enabled):
			return machine
	# Fallback: if exactly one enabled machine exists, treat it as default.
	enabled = [m for m in (settings.machines or []) if cint(m.enabled)]
	if len(enabled) == 1:
		return enabled[0]
	return None


def resolve_machine_for_context(reference_doctype, reference_name, source_doc=None):
	"""Pick the Pinelabs Machine Config row for a transaction.

	Resolution depends on Pinelabs Settings.terminal_routing_mode:

	- ``Single Machine`` (default) → returns ``get_default_machine_config()``.
	- ``Mapping`` → walks ``settings.machine_mappings`` (an inline child table)
	  for rows whose ``(mapping_type, reference_name)`` matches a context
	  extracted from the source document and current session, ordered by
	  ``priority asc, machine asc``; falls back to ``get_default_machine_config()``
	  on no match.

	Returns ``None`` if neither path resolves a machine — caller should error
	with the standard "no machine configured" message.
	"""
	settings = frappe.get_single("Pinelabs Settings")
	if not cint(settings.enabled):
		return None

	mode = (settings.terminal_routing_mode or "Single Machine").strip()
	if mode != "Mapping":
		return get_default_machine_config()

	if source_doc is None and reference_doctype and reference_name:
		try:
			source_doc = frappe.get_doc(reference_doctype, reference_name)
		except Exception:
			source_doc = None

	contexts = _extract_routing_contexts(source_doc)
	if contexts:
		context_set = set(contexts)
		# Filter the inline child rows in Python — the table is small enough
		# that a DB query buys nothing; staying in-process also means the
		# rows reflect any unsaved edits in the current request.
		candidates = [
			row
			for row in (settings.machine_mappings or [])
			if cint(row.is_enabled)
			and (row.mapping_type, row.reference_name) in context_set
		]
		# Stable sort on (priority asc, machine asc) — matches the previous
		# `order_by="priority asc, machine asc"` SQL ordering.
		candidates.sort(key=lambda r: (cint(r.priority), (r.machine or "")))
		if candidates:
			machine_name = candidates[0].machine
			# Resolve the child-table row on Settings.machines — terminal.py
			# expects a Machine Config child with all credential fields populated.
			for machine in (settings.machines or []):
				if machine.machine_name == machine_name and cint(machine.enabled):
					return machine

	return get_default_machine_config()


def _extract_routing_contexts(source_doc):
	"""Return [(mapping_type, reference_name), ...] for the current request.

	Always emits a User context for the logged-in user (skip Guest). Pulls
	Warehouse / Company / POS Profile from the source doc's standard fields,
	plus any unique ``items[*].warehouse`` rows.
	"""
	contexts = []

	user = (frappe.session.user if hasattr(frappe, "session") else None) or ""
	if user and user != "Guest":
		contexts.append(("User", user))

	if source_doc is None:
		return contexts

	def _get(field):
		# Works for both Document and dict-like inputs.
		try:
			return source_doc.get(field)
		except Exception:
			return getattr(source_doc, field, None)

	warehouse = _get("set_warehouse") or _get("warehouse")
	if warehouse:
		contexts.append(("Warehouse", warehouse))

	company = _get("company")
	if company:
		contexts.append(("Company", company))

	pos_profile = _get("pos_profile")
	if pos_profile:
		contexts.append(("POS Profile", pos_profile))

	# Per-line warehouses on the items child table.
	seen_warehouses = {warehouse} if warehouse else set()
	items = _get("items") or []
	for item in items:
		try:
			wh = item.get("warehouse") if hasattr(item, "get") else getattr(item, "warehouse", None)
		except Exception:
			wh = None
		if wh and wh not in seen_warehouses:
			contexts.append(("Warehouse", wh))
			seen_warehouses.add(wh)

	return contexts
