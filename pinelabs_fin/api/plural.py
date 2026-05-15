# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Plural payment-link flow.

The "Send Payment Link" button drops into one of these flows:

  flow="all_methods"  - Plural hosted page, all 4 methods (default).
  flow="card_only"    - Plural hosted page restricted to CARD.
  flow="upi_only"     - Plural hosted page restricted to UPI.
  flow="nb_only"      - Plural hosted page restricted to NETBANKING.

Completion is driven by webhook.py — this module never creates Payment
Entries. It only creates the transaction, calls the right Plural API,
and stores the order_id / redirect_url for the customer flow.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime

_DEFAULT_EXPIRY_MINUTES = 1440
_DEFAULT_COUNTRY_CODE = "91"

_FLOW_TO_METHODS = {
	"all_methods": None,  # don't restrict — Plural shows everything enabled on dashboard
	"card_only":   ["CARD"],
	"upi_only":    ["UPI"],
	"nb_only":     ["NETBANKING"],
}


@frappe.whitelist()
def initiate_payment_link(
	reference_doctype: str,
	reference_name: str,
	flow: str = "all_methods",
	expiry_minutes: int | None = None,
	customer_email: str | None = None,
	customer_mobile: str | None = None,
	create_payment_entry: int | None = None,
) -> dict:
	"""Single entry point for the Pay-by-Link flow.

	``customer_email`` / ``customer_mobile`` are optional inline overrides
	for the customer contact resolution. When the caller already has the
	values (e.g. a custom button collected them from a form), passing them
	here bypasses the Customer Contact Field Mapping for that channel. If
	either is omitted, the resolver fills in the missing one from the
	source doc / Payable Documents row config. So:
	  - pass both       → no resolver lookup is consulted for either
	  - pass one        → resolver still fills the other (if it can)
	  - pass neither    → full legacy behaviour (resolver only)

	``create_payment_entry`` (optional, ``0`` / ``1``): per-call override of
	the Payable Documents row's Create Payment Entry setting. ``1`` forces
	PE creation on success (and runs the account-mapping pre-flight now);
	``0`` skips PE creation entirely (mapped finalization is used if
	configured). Omit to use the configured default.

	Returns:
	  {success, transaction_name, payment_link_url, payment_link_id, message}
	"""
	from pinelabs_fin.api import transaction as txn_service

	print(f"[PineLabs PayByLink] Initiate: reference_doctype={reference_doctype} reference_name={reference_name} flow={flow} expiry_minutes={expiry_minutes}")
	frappe.logger("pinelabs_paybylink", file_count=5).info(f"[PineLabs PayByLink] Initiate: reference_doctype={reference_doctype} reference_name={reference_name} flow={flow} expiry_minutes={expiry_minutes}")

	if flow not in _FLOW_TO_METHODS:
		frappe.throw(_("Unknown flow: {0}").format(flow))

	source_doc = _load_eligible_source(reference_doctype, reference_name)
	# Fail fast BEFORE we ask Plural to deliver a link — if finalization
	# won't work for this doctype, the customer should never see the link.
	txn_service.validate_finalization_ready(reference_doctype)
	# Every Plural payment finalizes through the single "PineLabs - Payment Link"
	# Mode of Payment. Confirm its default account is mapped for this company
	# now — surfacing the misconfig before the link is sent costs nothing,
	# discovering it after the customer pays leaves them charged and the
	# invoice unfinalized. Skipped automatically when the doctype's
	# finalization path doesn't create a Payment Entry.
	txn_service.assert_mode_has_account_mapped(
		txn_service.get_plural_mode_of_payment(),
		getattr(source_doc, "company", None),
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		create_payment_entry=create_payment_entry,
	)
	amount = _amount_for(source_doc)
	if amount <= 0:
		frappe.throw(_("No outstanding amount on {0} {1}.").format(reference_doctype, reference_name))

	# Start with whatever the resolver can find on the source doc; the
	# caller's inline values (if any) take precedence below.
	customer = _get_customer_details(source_doc)
	if customer_email:
		customer["email_id"] = (customer_email or "").strip()
	if customer_mobile:
		customer["mobile_number"] = (customer_mobile or "").strip()

	if not (customer.get("email_id") or customer.get("mobile_number")):
		from pinelabs_fin.api.customer_contact import describe_resolution

		trace = describe_resolution(source_doc)
		frappe.throw(
			_("Customer email or mobile is required to send a payment link.")
			+ "<br><br>"
			+ _(
				"Either pass `customer_email` / `customer_mobile` directly when "
				"calling the API, or configure Customer Contact Field Mapping in "
				"Pinelabs Settings → Payable Documents."
			)
			+ "<br><br>"
			+ _("Resolver trace:")
			+ "<pre style='white-space:pre-wrap;'>"
			+ frappe.utils.escape_html(frappe.as_json(trace, indent=2))
			+ "</pre>",
		)

	# Prevent concurrent attempts on the same doc.
	existing = txn_service.get_active_for_doc(reference_doctype, reference_name)
	if existing:
		frappe.throw(
			_("A {0} payment ({1}) is already in progress for this document.").format(
				existing.flow_type, existing.name
			),
		)

	expiry = cint(expiry_minutes) or _settings_default_expiry()

	result = _initiate_payment_link(
		source_doc=source_doc,
		amount=amount,
		customer=customer,
		flow=flow,
		expiry_minutes=expiry,
		create_payment_entry=create_payment_entry,
	)

	print(f"[PineLabs PayByLink] Initiate Result: {result}")
	frappe.logger("pinelabs_paybylink", file_count=5).info(f"[PineLabs PayByLink] Initiate Result: {result}")
	return result


@frappe.whitelist()
def refresh_payment_link_status(transaction_name: str) -> dict:
	"""Manual status refresh for admin troubleshooting (webhooks are primary)."""
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.plural_client import get_plural_client

	txn = frappe.get_doc("Pinelabs Transaction", transaction_name)
	if txn.flow_type != "Plural":
		frappe.throw(_("Transaction {0} is not a Plural transaction.").format(transaction_name))
	if txn.status in ("SUCCESS", "FAILED", "CANCELLED"):
		return {"success": True, "status": txn.status, "message": "Already terminal"}

	# If the source doc has been deleted since the link was sent, neither the
	# webhook path nor PE creation can finalize this txn. Mark it FAILED so the
	# cron stops looping and the operator sees a clear explanation, instead of
	# the row sitting PENDING forever.
	if not frappe.db.exists(txn.reference_doctype, txn.reference_name):
		txn_service.mark_failed(
			txn,
			error_message=(
				f"Source {txn.reference_doctype} {txn.reference_name} no longer "
				f"exists; cannot finalize this payment link."
			),
		)
		return {"success": True, "status": "FAILED", "message": "Source doc deleted"}

	client = get_plural_client()
	result = client.get_payment_link_status(txn.reference_name)
	if not result.get("success"):
		return {"success": False, "status": txn.status, "error": result.get("error")}

	data = result.get("data") or {}
	status = (data.get("status") or "").upper()
	if status in ("PROCESSED", "PAID", "SUCCESS"):
		# Synthesize a webhook-shaped payload and route through transaction.py.
		txn_service.mark_success(
			txn,
			payment_id=data.get("payment_id") or data.get("transaction_id") or data.get("order_id"),
			payment_method=(data.get("payment_method") or "").upper() or None,
			response_payload=data,
		)
		return {"success": True, "status": "SUCCESS"}
	if status in ("FAILED", "CANCELLED", "EXPIRED", "DECLINED"):
		txn_service.mark_failed(txn, error_message=data.get("message") or status, response_payload=data)
		return {"success": True, "status": "FAILED"}
	return {"success": True, "status": "PENDING", "message": status or "still pending"}


def reconcile_pending_plural_payments() -> None:
	"""Cron task — reconcile every PENDING Plural transaction.

	Why this exists: Plural webhooks are the primary signal, but they need a
	publicly-reachable URL and a configured signing secret. On dev sites, on
	misconfigured webhooks, or when a webhook delivery is dropped, the
	transaction would sit at PENDING forever and the user would have to hit
	the form's manual "Refresh Status" button. This job pulls the status
	server-side every minute so docs finalize regardless of webhook health.

	Idempotent: piggy-backs on `refresh_payment_link_status`, which routes
	through `mark_success` / `mark_failed` (both no-op on terminal states).
	`mark_success` also publishes a realtime event so any open form-view
	auto-refreshes once we land here. Errors per row are logged but never
	abort the whole batch.
	"""
	rows = frappe.get_all(
		"Pinelabs Transaction",
		filters={
			"flow_type": "Plural",
			"status": "PENDING",
		},
		fields=["name"],
	)
	for row in rows:
		try:
			refresh_payment_link_status(row.name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				frappe.get_traceback(),
				f"Pinelabs reconcile_pending_plural_payments failed for {row.name}",
			)


# ──────────────────────────────────────────────────────────────────────────
# Flow implementations
# ──────────────────────────────────────────────────────────────────────────


def _initiate_payment_link(*, source_doc, amount, customer, flow, expiry_minutes, create_payment_entry=None):
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.plural_client import get_plural_client

	allowed_methods = _FLOW_TO_METHODS.get(flow)  # None for all_methods

	# Resolve a payment_method label *only* when the link is restricted to one.
	# For all_methods we leave it blank — the webhook fills it in based on what the customer used.
	preset_method = (allowed_methods[0] if allowed_methods and len(allowed_methods) == 1 else None)
	method_label = {"CARD": "CARD", "UPI": "UPI", "NETBANKING": "NB"}.get(preset_method)

	payload = _build_payment_link_payload(
		merchant_ref=source_doc.name,
		amount=amount,
		expiry_minutes=expiry_minutes,
		customer=customer,
		allowed_methods=allowed_methods,
	)

	txn = txn_service.create_transaction(
		reference_doctype=source_doc.doctype,
		reference_name=source_doc.name,
		flow_type="Plural",
		amount=amount,
		payment_method=method_label,
		request_payload=payload,
		create_payment_entry=create_payment_entry,
	)

	client = get_plural_client()
	response = client.create_payment_link(payload)

	if not response.get("success"):
		txn_service.mark_failed(
			txn,
			error_message=response.get("error") or _("Failed to create payment link"),
			response_payload=response.get("raw"),
		)
		return {"success": False, "transaction_name": txn.name, "error": response.get("error")}

	txn_service.mark_pending(
		txn,
		order_id=response.get("payment_link_id"),
		response_payload=response.get("raw"),
	)
	return {
		"success": True,
		"transaction_name": txn.name,
		"payment_link_url": response.get("payment_link_url"),
		"payment_link_id": response.get("payment_link_id"),
		"message": _("Payment link generated. Customer will receive SMS/Email from Pine Labs."),
	}


# ──────────────────────────────────────────────────────────────────────────
# Payload builders
# ──────────────────────────────────────────────────────────────────────────


def _build_payment_link_payload(*, merchant_ref, amount, expiry_minutes, customer, allowed_methods):
	expiry_iso = add_to_date(get_datetime(), minutes=expiry_minutes).strftime("%Y-%m-%dT%H:%M:%SZ")
	body = {
		"merchant_payment_link_reference": merchant_ref,
		"amount": {"value": round(flt(amount) * 100), "currency": "INR"},
		"description": f"Payment for {merchant_ref}",
		"expiry_date": expiry_iso,
		"send_sms": True,
		"send_email": True,
		"customer": {
			"email_id": customer.get("email_id"),
			"first_name": customer.get("first_name"),
			"mobile_number": customer.get("mobile_number"),
			"country_code": customer.get("country_code"),
		},
	}
	if allowed_methods:
		body["allowed_payment_methods"] = list(allowed_methods)
	return body


# ──────────────────────────────────────────────────────────────────────────
# Customer extraction (works for any payable doctype)
# ──────────────────────────────────────────────────────────────────────────


def _get_customer_details(doc):
	# Priority 1: explicit per-doctype mapping configured in Pinelabs Settings →
	# Payable Doctypes → Customer Contact Field Mapping. Empty-string returns
	# from the resolver mean "not configured / not found", so we still fall
	# through to the auto-detect chain below.
	from pinelabs_fin.api.customer_contact import resolve_from_config

	configured = resolve_from_config(doc)
	mobile = configured.get("mobile") or None
	email = configured.get("email") or None

	# Priority 2: well-known direct fields on the source doc.
	mobile = mobile or doc.get("contact_mobile") or doc.get("mobile_no") or doc.get("phone")
	email = email or doc.get("contact_email") or doc.get("email_id") or doc.get("email")
	name = doc.get("customer_name") or doc.get("party_name") or doc.get("contact_display") or doc.get("full_name")
	country_code = doc.get("phone_country_code") or doc.get("country_code") or _DEFAULT_COUNTRY_CODE

	# Priority 3: linked Contact doc (default contact for the customer/party).
	if not (mobile and email and name):
		contact = _find_contact(doc)
		if contact:
			mobile = mobile or contact.mobile_no or contact.phone
			email = email or contact.email_id
			if not name:
				first = (contact.first_name or "").strip()
				last = (contact.last_name or "").strip()
				name = (f"{first} {last}").strip() or None

	first_name = name.split(" ")[0] if name else "Customer"

	if mobile:
		digits = "".join(filter(str.isdigit, mobile))
		if len(digits) > 10 and digits.startswith("91"):
			digits = digits[-10:]
		elif len(digits) > 10:
			digits = digits[-10:]
		mobile = digits

	country_code = "".join(filter(str.isdigit, str(country_code))) or _DEFAULT_COUNTRY_CODE

	return {
		"email_id": email,
		"first_name": first_name,
		"mobile_number": mobile,
		"country_code": country_code,
	}


def _find_contact(doc):
	if doc.get("contact_person"):
		try:
			return frappe.get_doc("Contact", doc.contact_person)
		except Exception:
			return None
	party = doc.get("customer") or doc.get("party")
	party_type = "Customer" if doc.get("customer") else doc.get("party_type")
	if not (party and party_type):
		return None
	from frappe.contacts.doctype.contact.contact import get_default_contact
	contact_name = get_default_contact(party_type, party)
	if contact_name:
		try:
			return frappe.get_doc("Contact", contact_name)
		except Exception:
			return None
	return None


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _load_eligible_source(reference_doctype, reference_name):
	if not (reference_doctype and reference_name):
		frappe.throw(_("reference_doctype and reference_name are required."))
	if not frappe.db.exists(reference_doctype, reference_name):
		frappe.throw(_("{0} {1} does not exist.").format(reference_doctype, reference_name))
	doc = frappe.get_doc(reference_doctype, reference_name)
	if cint(getattr(doc, "docstatus", 0)) != 1:
		frappe.throw(_("{0} {1} must be submitted to send a payment link.").format(reference_doctype, reference_name))
	return doc


def _amount_for(source_doc):
	for field in ("outstanding_amount", "grand_total", "amount"):
		val = getattr(source_doc, field, None)
		if val is not None:
			return flt(val)
	return 0.0


def _settings_default_expiry():
	try:
		settings = frappe.get_single("Pinelabs Settings")
		val = cint(getattr(settings, "payment_link_expiry_minutes", None))
		return val or _DEFAULT_EXPIRY_MINUTES
	except Exception:
		return _DEFAULT_EXPIRY_MINUTES
