# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Resolve customer mobile/email from the per-doctype config in Pinelabs Settings.

Each row in `Pinelabs Settings.payable_doctypes` (Pinelabs Payable Doctype)
optionally carries a Customer Contact Field Mapping that says where mobile
and email live for that source doctype. Two modes per field:

  - Direct Field   → read <doc>.<field>
  - Linked DocType → read <linked_doc>.<field>, where linked_doc is found
                     via <doc>.<link_field> (a Link / Dynamic Link)

The resolver never raises on missing values — callers decide whether to
fall back to a generic auto-detect or surface a "missing contact" error.
"""

import frappe
from frappe.utils import cint


def resolve_from_config(doc) -> dict[str, str]:
	"""Return a {"mobile": str, "email": str} dict from the configured mapping.

	Both values default to "" when:
	  - No matching `Pinelabs Payable Doctype` row exists for `doc.doctype`.
	  - The matching row has no fetch_from configured for that field.
	  - The configured field/linked-doc/linked-field is empty on the source.

	The empty-string return lets callers chain a fallback (e.g. auto-detect
	on common field names) without needing to handle exceptions.
	"""
	mapping = _find_mapping_row(getattr(doc, "doctype", None))
	if mapping is None:
		return {"mobile": "", "email": ""}
	return {
		"mobile": _resolve_value(doc, mapping, "mobile"),
		"email": _resolve_value(doc, mapping, "email"),
	}


def describe_resolution(doc) -> dict:
	"""Return a verbose trace of what the resolver tried, for error messages.

	Each field gets a one-line dict explaining where the value came from
	(or why it was blank). Used to surface debuggable detail when callers
	throw a "missing contact" error — the user immediately sees whether the
	mapping fired, which doc was looked at, and what was returned.
	"""
	doctype = getattr(doc, "doctype", None)
	mapping = _find_mapping_row(doctype)
	if mapping is None:
		return {
			"config_status": f"no enabled Pinelabs Payable Doctype row for {doctype!r}",
			"mobile": {"source": "auto-detect (no row)", "value": ""},
			"email": {"source": "auto-detect (no row)", "value": ""},
		}
	return {
		"config_status": f"row matched for {doctype!r}",
		"mobile": _trace_value(doc, mapping, "mobile"),
		"email": _trace_value(doc, mapping, "email"),
	}


def _trace_value(doc, row, field_type):
	fetch_from = (row.get(f"{field_type}_fetch_from") or "").strip()
	if not fetch_from:
		return {"source": "auto-detect (fetch_from blank)", "value": ""}

	if fetch_from == "Direct Field":
		direct_field = (row.get(f"{field_type}_direct_field") or "").strip()
		if not direct_field:
			return {
				"source": "Direct Field",
				"error": f"{field_type}_direct_field is empty",
				"value": "",
			}
		raw = doc.get(direct_field)
		return {
			"source": "Direct Field",
			"field": direct_field,
			"raw": raw,
			"value": _stringify(raw),
		}

	if fetch_from == "Linked DocType":
		link_field = (row.get(f"{field_type}_link_field") or "").strip()
		linked_dt = (row.get(f"{field_type}_linked_doctype") or "").strip()
		linked_field = (row.get(f"{field_type}_linked_field") or "").strip()
		base = {
			"source": "Linked DocType",
			"link_field": link_field,
			"linked_doctype": linked_dt,
			"linked_field": linked_field,
		}
		if not (link_field and linked_dt and linked_field):
			return {**base, "error": "one or more mapping fields are empty", "value": ""}

		linked_name = doc.get(link_field)
		if not linked_name:
			return {
				**base,
				"error": f"{link_field!r} is empty on this {doc.doctype}",
				"value": "",
			}

		try:
			raw = frappe.db.get_value(linked_dt, linked_name, linked_field)
		except Exception as e:
			return {**base, "linked_name": linked_name, "error": f"lookup failed: {e}", "value": ""}
		return {
			**base,
			"linked_name": linked_name,
			"raw": raw,
			"value": _stringify(raw),
		}

	return {"source": fetch_from, "error": "unknown fetch_from value", "value": ""}


def _find_mapping_row(doctype):
	if not doctype:
		return None
	try:
		settings = frappe.get_single("Pinelabs Settings")
	except Exception:
		return None
	for row in (settings.payable_doctypes or []):
		if row.doctype_name != doctype:
			continue
		if not cint(getattr(row, "is_enabled", 0)):
			continue
		return row
	return None


def _resolve_value(doc, row, field_type):
	"""field_type is 'mobile' or 'email'. Returns "" on any missing piece."""
	fetch_from = (row.get(f"{field_type}_fetch_from") or "").strip()
	if not fetch_from:
		return ""

	if fetch_from == "Direct Field":
		direct_field = (row.get(f"{field_type}_direct_field") or "").strip()
		if not direct_field:
			return ""
		return _stringify(doc.get(direct_field))

	if fetch_from == "Linked DocType":
		link_field = (row.get(f"{field_type}_link_field") or "").strip()
		linked_dt = (row.get(f"{field_type}_linked_doctype") or "").strip()
		linked_field = (row.get(f"{field_type}_linked_field") or "").strip()
		if not (link_field and linked_dt and linked_field):
			return ""

		linked_name = doc.get(link_field)
		if not linked_name:
			return ""

		# Use db_get_value rather than get_doc — cheaper, avoids loading the
		# whole linked record just to read one field.
		try:
			value = frappe.db.get_value(linked_dt, linked_name, linked_field)
		except Exception:
			return ""
		return _stringify(value)

	return ""


def _stringify(value):
	if value is None:
		return ""
	return str(value).strip()


# ──────────────────────────────────────────────────────────────────────────
# Metadata endpoints (driving the cascading dropdowns in the Settings grid)
# ──────────────────────────────────────────────────────────────────────────


_MOBILE_TEXT_FIELDTYPES = ("Data", "Phone", "Small Text", "Read Only")
_EMAIL_TEXT_FIELDTYPES = ("Data", "Email", "Read Only")
_LINK_FIELDTYPES = ("Link", "Dynamic Link")
_AMOUNT_FIELDTYPES = ("Currency", "Float", "Int", "Percent")
_STATUS_FIELDTYPES = ("Data", "Select", "Link", "Read Only", "Small Text")
_REFERENCE_FIELDTYPES = ("Data", "Link", "Small Text")


@frappe.whitelist()
def list_doctype_fields(doctype: str) -> dict:
	"""Return the cascading-mapping field lists for a doctype.

	Output shape:
		{
		  "link_fields":      [{fieldname, label, fieldtype, options}, ...],
		  "mobile_fields":    [...],
		  "email_fields":     [...],
		  "amount_fields":    [...],   # Currency / Float / Int / Percent
		  "status_fields":    [...],   # Data / Select / Link / Read Only
		  "reference_fields": [...],   # Data / Link / Small Text — for ID storage
		}

	Each list is sorted with keyword-matching names first where applicable
	(amount/total/grand for amount; status/state for status; ref/id for
	reference; mobile/phone for mobile; email for email) so the most likely
	choice surfaces at the top of the dropdown.
	"""
	empty = {
		"link_fields": [],
		"mobile_fields": [],
		"email_fields": [],
		"amount_fields": [],
		"status_fields": [],
		"reference_fields": [],
	}
	if not doctype:
		return empty
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return empty

	out = {k: [] for k in empty}

	for df in meta.fields:
		described = _describe_field(df)
		if df.fieldtype in _LINK_FIELDTYPES:
			out["link_fields"].append(described)
		if df.fieldtype in _MOBILE_TEXT_FIELDTYPES:
			out["mobile_fields"].append(described)
		if df.fieldtype in _EMAIL_TEXT_FIELDTYPES:
			out["email_fields"].append(described)
		if df.fieldtype in _AMOUNT_FIELDTYPES:
			out["amount_fields"].append(described)
		if df.fieldtype in _STATUS_FIELDTYPES:
			out["status_fields"].append(described)
		if df.fieldtype in _REFERENCE_FIELDTYPES:
			out["reference_fields"].append(described)

	out["link_fields"].sort(key=lambda f: f["fieldname"])
	out["mobile_fields"].sort(key=_keyword_sort_key(
		priority_names=("contact_mobile", "mobile_no", "mobile", "phone"),
		keywords=("mobile", "phone"),
	))
	out["email_fields"].sort(key=_keyword_sort_key(
		priority_names=("contact_email", "email_id", "email"),
		keywords=("email",),
	))
	out["amount_fields"].sort(key=_keyword_sort_key(
		priority_names=(
			"outstanding_amount",
			"grand_total",
			"base_grand_total",
			"base_outstanding_amount",
			"total",
			"paid_amount",
			"amount",
		),
		keywords=("amount", "total"),
	))
	out["status_fields"].sort(key=_keyword_sort_key(
		priority_names=("status", "workflow_state"),
		keywords=("status", "state", "workflow"),
	))
	out["reference_fields"].sort(key=_keyword_sort_key(
		priority_names=(
			"pinelabs_order_id",
			"pinelabs_transaction_id",
			"pinelabs_payment_id",
		),
		keywords=("pinelabs", "transaction", "reference", "ref"),
	))

	return out


@frappe.whitelist()
def get_link_field_target(doctype: str, fieldname: str) -> str:
	"""Return the target doctype for a Link field on `doctype`.

	Returns "" for Dynamic Link (target lives in another field; the user
	has to pick the linked doctype manually) or anything that isn't a
	Link-type field.
	"""
	if not (doctype and fieldname):
		return ""
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return ""
	df = meta.get_field(fieldname)
	if not df:
		return ""
	if df.fieldtype == "Link":
		return df.options or ""
	return ""


def _describe_field(df):
	return {
		"fieldname": df.fieldname,
		"label": df.label or df.fieldname,
		"fieldtype": df.fieldtype,
		"options": df.options or "",
	}


def _keyword_sort_key(priority_names=(), keywords=()):
	"""Tiered sort:
	  0. fieldname is in `priority_names` — kept in the order they were given,
	     so the canonical pick (e.g. `outstanding_amount`) goes to the very top.
	  1. starts with any of `keywords`
	  2. contains any of `keywords`
	  3. everything else
	Within tiers 1-3, alphabetical.
	"""
	priority_index = {name: i for i, name in enumerate(priority_names)}

	def _key(field):
		name = (field["fieldname"] or "").lower()
		if name in priority_index:
			return (0, priority_index[name], name)
		if any(name.startswith(k) for k in keywords):
			return (1, 0, name)
		if any(k in name for k in keywords):
			return (2, 0, name)
		return (3, 0, name)
	return _key
