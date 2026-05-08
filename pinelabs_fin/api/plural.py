# -*- coding: utf-8 -*-
# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Plural payment-link flow + Quick Net Banking option.

The "Send Payment Link" button drops into one of these flows:

  flow="all_methods"  - Plural hosted page, all 4 methods (default).
  flow="card_only"    - Plural hosted page restricted to CARD.
  flow="upi_only"     - Plural hosted page restricted to UPI.
  flow="nb_only"      - Plural hosted page restricted to NETBANKING.
  flow="quick_nb"     - Direct NB (server-to-server seamless API + bank_code).
                        Requires bank_code; redirects customer to bank.

Completion is driven by webhook.py — this module never creates Payment
Entries. It only creates the transaction, calls the right Plural API,
and stores the order_id / redirect_url for the customer flow.
"""

from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, get_url


_DEFAULT_EXPIRY_MINUTES = 1440
_DEFAULT_COUNTRY_CODE = "91"

_FLOW_TO_METHODS = {
	"all_methods": None,  # don't restrict — Plural shows everything enabled on dashboard
	"card_only":   ["CARD"],
	"upi_only":    ["UPI"],
	"nb_only":     ["NETBANKING"],
}


@frappe.whitelist()
def initiate_payment_link(reference_doctype, reference_name, flow="all_methods", bank_code=None, expiry_minutes=None):
	"""Single entry point for both Pay-by-Link and Quick Net Banking flows.

	Returns:
	  - all_methods/card_only/upi_only/nb_only:
	      {success, transaction_name, payment_link_url, payment_link_id, message}
	  - quick_nb:
	      {success, transaction_name, redirect_url, transaction_id, message}
	"""
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

	if flow not in _FLOW_TO_METHODS and flow != "quick_nb":
		frappe.throw(_("Unknown flow: {0}").format(flow))

	source_doc = _load_eligible_source(reference_doctype, reference_name)
	amount = _amount_for(source_doc)
	if amount <= 0:
		frappe.throw(_("No outstanding amount on {0} {1}.").format(reference_doctype, reference_name))

	customer = _get_customer_details(source_doc)
	if not (customer.get("email_id") or customer.get("mobile_number")):
		from pinelabs_fin.api.customer_contact import describe_resolution

		trace = describe_resolution(source_doc)
		frappe.throw(
			_("Customer email or mobile is required to send a payment link.")
			+ "<br><br>"
			+ _("Resolver trace (see Pinelabs Settings → Payable Documents → Customer Contact Field Mapping):")
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

	if flow == "quick_nb":
		return _initiate_quick_nb(
			source_doc=source_doc,
			amount=amount,
			customer=customer,
			bank_code=bank_code,
		)

	return _initiate_payment_link(
		source_doc=source_doc,
		amount=amount,
		customer=customer,
		flow=flow,
		expiry_minutes=expiry,
	)


@frappe.whitelist()
def refresh_payment_link_status(transaction_name):
	"""Manual status refresh for admin troubleshooting (webhooks are primary)."""
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

	txn = frappe.get_doc("Pinelabs Transaction", transaction_name)
	if txn.flow_type not in ("Plural", "Direct NB"):
		frappe.throw(_("Transaction {0} is not a Plural or Direct NB transaction.").format(transaction_name))
	if txn.status in ("SUCCESS", "FAILED", "CANCELLED"):
		return {"success": True, "status": txn.status, "message": "Already terminal"}

	client = get_pinelabs_client()
	result = client.get_payment_link_status(txn.reference_name)
	if not result.get("success"):
		return {"success": False, "status": txn.status, "error": result.get("error")}

	data = result.get("data") or {}
	status = (data.get("status") or "").upper()
	if status in ("PROCESSED", "PAID", "SUCCESS"):
		from pinelabs_fin.api import webhook as webhook_module  # late import; reuse the same classifier
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


# ──────────────────────────────────────────────────────────────────────────
# Flow implementations
# ──────────────────────────────────────────────────────────────────────────


def _initiate_payment_link(*, source_doc, amount, customer, flow, expiry_minutes):
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

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
	)

	client = get_pinelabs_client()
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


def _initiate_quick_nb(*, source_doc, amount, customer, bank_code):
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

	if not bank_code:
		frappe.throw(_("Quick Net Banking requires a bank_code (NB####)."))

	payload = _build_seamless_nb_payload(
		merchant_ref=source_doc.name,
		amount=amount,
		customer=customer,
		bank_code=bank_code,
	)

	txn = txn_service.create_transaction(
		reference_doctype=source_doc.doctype,
		reference_name=source_doc.name,
		flow_type="Direct NB",
		amount=amount,
		payment_method="NB",
		bank_code=bank_code,
		request_payload=payload,
	)

	client = get_pinelabs_client()
	response = client.create_netbanking_payment(payload)

	if not response.get("success"):
		txn_service.mark_failed(
			txn,
			error_message=response.get("error") or _("Failed to initiate netbanking"),
			response_payload=response.get("raw"),
		)
		return {"success": False, "transaction_name": txn.name, "error": response.get("error")}

	txn_service.mark_pending(
		txn,
		order_id=response.get("transaction_id"),
		response_payload=response.get("raw"),
	)
	return {
		"success": True,
		"transaction_name": txn.name,
		"redirect_url": response.get("redirect_url"),
		"transaction_id": response.get("transaction_id"),
		"message": _("Redirecting customer to bank for authentication."),
	}


# ──────────────────────────────────────────────────────────────────────────
# Payload builders
# ──────────────────────────────────────────────────────────────────────────


def _build_payment_link_payload(*, merchant_ref, amount, expiry_minutes, customer, allowed_methods):
	expiry_iso = add_to_date(get_datetime(), minutes=expiry_minutes).strftime("%Y-%m-%dT%H:%M:%SZ")
	body = {
		"merchant_payment_link_reference": merchant_ref,
		"amount": {"value": int(round(flt(amount) * 100)), "currency": "INR"},
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


def _build_seamless_nb_payload(*, merchant_ref, amount, customer, bank_code):
	callback = get_url("/api/method/pinelabs_fin.api.plural.handle_quick_nb_callback")
	return {
		"merchant_order_reference": merchant_ref,
		"amount": {"value": int(round(flt(amount) * 100)), "currency": "INR"},
		"description": f"Net Banking payment for {merchant_ref}",
		"customer": {
			"email_id": customer.get("email_id"),
			"first_name": customer.get("first_name"),
			"mobile_number": customer.get("mobile_number"),
			"country_code": customer.get("country_code"),
		},
		"payment_option": {
			"payment_method": "NETBANKING",
			"netbanking_details": {"bank_code": bank_code},
		},
		"callback_url": callback,
	}


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
# Quick NB callback (customer returns from bank)
# ──────────────────────────────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True)
def handle_quick_nb_callback(transaction_id=None, merchant_order_reference=None, **_kwargs):
	"""Bank/Plural redirects the customer here after auth.

	Webhook is the source of truth for status; this handler just fetches
	the latest state and bounces the customer back to the source document
	with a status hint.
	"""
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

	merchant_ref = merchant_order_reference
	txn_name = None
	if merchant_ref:
		txn_name = frappe.db.get_value("Pinelabs Transaction", {"reference_name": merchant_ref, "flow_type": "Direct NB"}, "name")
	if not txn_name and transaction_id:
		txn_name = frappe.db.get_value("Pinelabs Transaction", {"order_id": transaction_id, "flow_type": "Direct NB"}, "name")

	target = "/app"
	if txn_name:
		txn = frappe.get_doc("Pinelabs Transaction", txn_name)
		if txn.reference_doctype and txn.reference_name:
			target = f"/app/{txn.reference_doctype.replace(' ', '-').lower()}/{txn.reference_name}?pinelabs_status={txn.status or 'Pending'}"

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = target


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
