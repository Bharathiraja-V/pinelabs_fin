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

		raw_body = frappe.request.get_data(as_text=True) or ""
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

		# DB-level event dedup. If the same event_id arrives twice (Plural retry,
		# replay attack, network glitch), the second insert fails on the UNIQUE
		# constraint and we return 200 OK without re-running the handler.
		event_id = _resolve_event_id(data, payload, raw_body)
		event_row = _claim_webhook_event(event_id, event_type, data)
		if event_row is None:
			_log_decision("duplicate event_id (idempotent)", txn_name=None, payment_id=event_id)
			_touch_last_sync()
			return _respond(200, "Duplicate event ignored", success=True)

		txn_name = _find_transaction(order_id)
		if not txn_name:
			_finalize_event(event_row, status="ignored")
			# 404 keeps Plural from retrying an unrecoverable case.
			return _respond(404, f"No Pinelabs Transaction matches {order_id}")

		from pinelabs_fin.api import transaction as txn_service

		txn = frappe.get_doc("Pinelabs Transaction", txn_name)
		if txn.status == "SUCCESS":
			# Idempotent — already finalized on a prior delivery.
			_log_decision("already SUCCESS (idempotent)", txn_name)
			_finalize_event(event_row, status="ignored", pinelabs_transaction=txn_name)
			_touch_last_sync()
			return _respond(200, "Already processed", success=True)

		decision = _classify(payload, event_type)
		if decision == "paid":
			payment_id = payload.get("payment_id") or payload.get("transaction_id") or order_id
			payment_method = _extract_method(payload)
			_log_decision("paid → mark_success", txn_name, payment_id=payment_id, payment_method=payment_method)
			txn_service.mark_success(
				txn,
				payment_id=payment_id,
				payment_method=payment_method,
				response_payload=data,
			)
			_finalize_event(event_row, status="processed", pinelabs_transaction=txn_name)
			_touch_last_sync()
			return _respond(200, "Processed", success=True)

		if decision == "failed":
			reason = payload.get("error_message") or payload.get("message") or event_type
			_log_decision("failed → mark_failed", txn_name, reason=reason)
			txn_service.mark_failed(txn, error_message=reason, response_payload=data)
			_finalize_event(event_row, status="processed", pinelabs_transaction=txn_name)
			_touch_last_sync()
			return _respond(200, "Recorded failure", success=True)

		# Unknown event — store payload for debugging, leave status alone.
		# Lock the row first so concurrent unknown-event deliveries can't
		# stomp each other's response_payload (cron + webhook + multiple
		# webhook retries all race on the same Pinelabs Transaction row).
		_log_decision(f"unknown event_type={event_type} status={payload.get('status')}", txn_name)
		txn = txn_service._lock_and_reload(txn)
		txn.response_payload = json.dumps(data, indent=2, default=str)
		txn.flags.pinelabs_internal_transition = True
		txn.save(ignore_permissions=True)
		_finalize_event(event_row, status="ignored", pinelabs_transaction=txn_name)
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
# Event dedup
# ──────────────────────────────────────────────────────────────────────────


def _resolve_event_id(envelope, payload, raw_body):
	"""Best stable id for this webhook delivery.

	Prefers explicit event identifiers from the provider when present;
	falls back to a sha256 of the raw body so the dedup property holds even
	when Plural omits a stable id.
	"""
	for key in ("event_id", "id", "transaction_id", "request_id"):
		v = envelope.get(key) or payload.get(key)
		if v:
			return str(v)[:140]
	return "sha256:" + hashlib.sha256((raw_body or "").encode("utf-8")).hexdigest()


def _claim_webhook_event(event_id, event_type, envelope):
	"""Insert a Pinelabs Webhook Event row. Returns the doc, or None on duplicate.

	The UNIQUE constraint on event_id is the DB-level dedup guarantee — a
	redelivery races and one insert wins, the other surfaces here as None.
	"""
	doc = frappe.new_doc("Pinelabs Webhook Event")
	doc.event_id = event_id
	doc.event_type = event_type
	doc.status = "received"
	doc.received_at = now_datetime()
	try:
		doc.payload = json.dumps(envelope, indent=2, default=str)
	except Exception:
		doc.payload = str(envelope)
	try:
		doc.insert(ignore_permissions=True)
		return doc
	except frappe.DuplicateEntryError:
		return None
	except Exception:
		# DB-level dedup may also surface as a raw IntegrityError from the
		# underlying driver depending on Frappe version — treat as duplicate.
		if "duplicate" in (frappe.get_traceback() or "").lower():
			return None
		raise


def _finalize_event(event_doc, *, status, pinelabs_transaction=None):
	"""Update the Webhook Event row with the resolved status + linked txn.

	Best-effort. Webhook handler will still return 200 even if this fails.
	"""
	if event_doc is None:
		return
	try:
		event_doc.status = status
		if pinelabs_transaction:
			event_doc.pinelabs_transaction = pinelabs_transaction
		event_doc.save(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			"Pinelabs webhook event finalize failed",
		)


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
		dump = (
			f"[PineLabs PayByLink] Webhook Received:\n"
			f"  Headers: X-Verify={frappe.request.headers.get('X-Verify')}\n"
			f"  Body: {json.dumps(data, indent=2, default=str)}"
		)
		print(dump)
		frappe.logger("pinelabs_paybylink", file_count=5).info(dump)
	except Exception:
		pass


def _log_decision(decision, txn_name, payment_id=None, payment_method=None, reason=None):
	try:
		line = f"[PineLabs PayByLink] Webhook Decision: txn={txn_name} → {decision}"
		if payment_id:
			line += f" payment_id={payment_id}"
		if payment_method:
			line += f" payment_method={payment_method}"
		if reason:
			line += f" reason={reason}"
		print(line)
		frappe.logger("pinelabs_paybylink", file_count=5).info(line)
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
