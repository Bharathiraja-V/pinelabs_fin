# -*- coding: utf-8 -*-
# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Plural payment-link webhook handler.

Receives POST callbacks from Pine Labs Plural after a customer completes
or fails a payment link. Validates the X-Verify HMAC against the raw
request body BEFORE JSON parsing, finds the matching Pinelabs Transaction
by order_id, and dispatches to transaction.mark_success / mark_failed.

HTTP contract:
  200  - Processed normally, or already-processed (idempotent retry).
  400  - Missing reference / unparseable body.
  401  - Signature missing / invalid / secret unconfigured.
  404  - Unknown order_id (Plural will not retry, which is intended).
  500  - System error (Plural will retry).
"""

from __future__ import unicode_literals

import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.utils import now_datetime


_PAID_STATUSES = {"PROCESSED", "SUCCESS", "PAID", "CAPTURED"}
_FAILED_STATUSES = {"FAILED", "FAILURE", "CANCELLED", "EXPIRED", "DECLINED"}
_PAID_EVENTS = {"ORDER_PROCESSED"}
_FAILED_EVENTS = {"PAYMENT_FAILED", "ORDER_CANCELLED", "ORDER_FAILED", "ORDER_EXPIRED"}


@frappe.whitelist(allow_guest=True)
def handle_webhook():
	"""Public entry point. See module docstring for HTTP contract."""
	try:
		if frappe.request.method != "POST":
			return _respond(405, "Only POST allowed")

		_validate_signature_or_throw()

		try:
			data = frappe.request.get_json() or {}
		except Exception:
			return _respond(400, "Unparseable JSON body")
		if not data:
			return _respond(400, "Empty body")

		_log_inbound(data)

		payload = data.get("data") or {}
		event_type = data.get("event_type")
		order_id = (
			payload.get("order_id")
			or payload.get("payment_link_id")
			or payload.get("merchant_order_reference")
			or payload.get("merchant_payment_link_reference")
		)
		if not order_id:
			return _respond(400, "Missing order_id / merchant reference")

		txn_name = _find_transaction(order_id)
		if not txn_name:
			# 404 keeps Plural from retrying an unrecoverable case.
			return _respond(404, f"No Pinelabs Transaction matches {order_id}")

		from pinelabs_fin.api import transaction as txn_service

		txn = frappe.get_doc("Pinelabs Transaction", txn_name)
		if txn.status == "SUCCESS":
			# Idempotent — already finalized on a prior delivery.
			_touch_last_sync()
			return _respond(200, "Already processed", success=True)

		decision = _classify(payload, event_type)
		if decision == "paid":
			payment_id = payload.get("payment_id") or payload.get("transaction_id") or order_id
			payment_method = _extract_method(payload)
			txn_service.mark_success(
				txn,
				payment_id=payment_id,
				payment_method=payment_method,
				response_payload=data,
			)
			_touch_last_sync()
			return _respond(200, "Processed", success=True)

		if decision == "failed":
			reason = payload.get("error_message") or payload.get("message") or event_type
			txn_service.mark_failed(txn, error_message=reason, response_payload=data)
			_touch_last_sync()
			return _respond(200, "Recorded failure", success=True)

		# Unknown event — store payload for debugging, leave status alone.
		txn.response_payload = json.dumps(data, indent=2, default=str)
		txn.flags.pinelabs_internal_transition = True
		txn.save(ignore_permissions=True)
		_touch_last_sync()
		return _respond(200, "Recorded", success=True)

	except frappe.AuthenticationError as exc:
		return _respond(401, str(exc))
	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), "Pinelabs Plural Webhook")
		return _respond(500, str(exc))


# ──────────────────────────────────────────────────────────────────────────
# Signature
# ──────────────────────────────────────────────────────────────────────────


def _validate_signature_or_throw():
	"""Refuse the request unless the X-Verify HMAC matches the raw body.

	Refuses when the webhook secret is unconfigured (fail-closed) — never
	silently accept unsigned webhooks on a guest-allowed endpoint.
	"""
	settings = frappe.get_single("Pinelabs Settings")
	secret = None
	try:
		secret = settings.get_password("payment_link_webhook_secret", raise_exception=False)
	except Exception:
		secret = None
	if not secret:
		secret = frappe.conf.get("pine_webhook_secret")
	if not secret:
		frappe.throw(
			_("Webhook secret not configured; refusing webhook."),
			frappe.AuthenticationError,
		)

	signature = frappe.request.headers.get("X-Verify")
	if not signature:
		frappe.throw(_("Missing X-Verify header"), frappe.AuthenticationError)

	raw_body = frappe.request.get_data(as_text=True)
	if not raw_body:
		frappe.throw(_("Empty body — cannot verify signature"), frappe.AuthenticationError)

	expected = hmac.new(
		secret.encode("utf-8"),
		raw_body.encode("utf-8"),
		hashlib.sha256,
	).hexdigest()

	if not hmac.compare_digest(signature.lower(), expected.lower()):
		frappe.throw(_("Invalid signature"), frappe.AuthenticationError)


# ──────────────────────────────────────────────────────────────────────────
# Routing helpers
# ──────────────────────────────────────────────────────────────────────────


def _find_transaction(order_id):
	"""Match a webhook to a Pinelabs Transaction by order_id, then payment_id, then name."""
	for filters in (
		{"order_id": order_id},
		{"payment_id": order_id},
		{"name": order_id},
	):
		name = frappe.db.get_value("Pinelabs Transaction", filters, "name")
		if name:
			return name
	return None


def _classify(payload, event_type):
	status = (payload.get("status") or "").upper()
	if event_type in _PAID_EVENTS or status in _PAID_STATUSES:
		return "paid"
	if event_type in _FAILED_EVENTS or status in _FAILED_STATUSES:
		return "failed"
	return "unknown"


def _extract_method(payload):
	"""Pull payment_method out of the webhook payload (varies by event shape)."""
	method = payload.get("payment_method") or payload.get("payment_mode")
	if not method:
		payments = payload.get("payments")
		if isinstance(payments, list) and payments:
			first = payments[0] or {}
			method = first.get("payment_method") or first.get("payment_mode")
	return (method or "").upper() or None


# ──────────────────────────────────────────────────────────────────────────
# Response shaping
# ──────────────────────────────────────────────────────────────────────────


def _respond(http_status, message, success=False):
	try:
		frappe.local.response["http_status_code"] = http_status
	except Exception:
		pass
	return {"success": bool(success), "status": "ok" if success else "error", "message": message}


def _log_inbound(data):
	try:
		frappe.logger().info(f"Plural webhook received: {json.dumps(data, default=str)[:1500]}")
	except Exception:
		pass


def _touch_last_sync():
	try:
		frappe.db.set_value(
			"Pinelabs Settings",
			"Pinelabs Settings",
			"last_sync",
			now_datetime(),
			update_modified=False,
		)
	except Exception:
		pass
