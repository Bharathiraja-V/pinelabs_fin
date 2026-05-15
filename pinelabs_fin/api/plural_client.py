# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""HTTP client for the Plural Pay-by-Link API.

Split out of ``pinelabs_client.py`` so the Pay-by-Link surface can be
imported, mocked, and reasoned about independently of the Pine Labs Cloud
terminal client. Plural is per-merchant (one set of credentials) — there's
no "machine" parameter, just the singleton Pinelabs Settings.

Public surface:
  * ``PluralClient`` — class wrapping the OAuth + payment-link calls.
  * ``get_plural_client()`` — factory returning a fresh client instance.
  * ``PLURAL_SANDBOX_URL`` / ``PLURAL_PRODUCTION_URL`` — canonical hosts,
    referenced by the Settings controller's auto-fill.

Token caching: a successful ``_get_plural_access_token`` puts the token in
``frappe.cache()`` under ``pine_plural_access_token`` with a 3500 s ceiling.
Settings.on_update busts that key whenever an identity-bearing field
changes, so the next call refreshes against the new identity.
"""

import json
import uuid
from datetime import datetime

import frappe
import requests
from frappe import _

# Canonical Plural API hosts. Single source of truth — also referenced by
# pinelabs_settings.PinelabsSettings._resolve_plural_base_url to surface the
# auto-resolved URL on the Settings form.
PLURAL_SANDBOX_URL = "https://pluraluat.v2.pinepg.in"
PLURAL_PRODUCTION_URL = "https://api.pluralpay.in"


class PluralClient:
	"""Wraps the OAuth + Pay-by-Link endpoints."""

	def __init__(self):
		self.settings = frappe.get_single("Pinelabs Settings")
		# Cached on the instance so repeated header builds in one request
		# don't re-decrypt the password field.
		self._creds_cache: tuple[str | None, str | None] | None = None

	# ─────────────────────────────────────────────────────────────────────
	# Public API — the methods callers in plural.py / config.py reach for
	# ─────────────────────────────────────────────────────────────────────

	def get_access_token(self) -> tuple[str | None, str | None]:
		"""Public alias for ``_get_plural_access_token``.

		Returns ``(token, error)`` — exactly one of the two is non-None.
		"""
		return self._get_plural_access_token()

	def create_payment_link(self, payload: dict) -> dict:
		"""POST /api/pay/v1/paymentlink — Plural hosted Pay-by-Link.

		URL pattern matches Pine Labs' published Pay-by-Link API docs:
		https://developer.pinelabsonline.com/reference/payment-link-create

		Returns {success, payment_link_url, payment_link_id, status, raw} or
		{success: False, error}.
		"""
		token, error = self._get_plural_access_token()
		if not token:
			return {"success": False, "error": error}

		url = f"{self._get_base_url()}/api/pay/v1/paymentlink"
		headers = self._headers(token)
		req_dump = (
			f"[PineLabs PayByLink] Link Request:\n"
			f"  POST {url}\n"
			f"  Headers: {json.dumps(headers, indent=2)}\n"
			f"  Body: {json.dumps(payload, indent=2)}"
		)
		print(req_dump)
		frappe.logger("pinelabs_paybylink", file_count=5).info(req_dump)
		try:
			response = requests.post(
				url,
				headers=headers,
				json=payload,
				timeout=30,
				verify=True,
			)
			data = response.json() if response.text else {}
		except Exception as exc:
			print(f"[PineLabs PayByLink] Link ERROR: {exc}")
			frappe.logger("pinelabs_paybylink", file_count=5).error(f"[PineLabs PayByLink] Link ERROR: {exc}")
			return {"success": False, "error": str(exc)}

		resp_dump = (
			f"[PineLabs PayByLink] Link Response:\n"
			f"  Status: {response.status_code}\n"
			f"  Body: {json.dumps(data, indent=2, default=str)}"
		)
		print(resp_dump)
		frappe.logger("pinelabs_paybylink", file_count=5).info(resp_dump)
		if response.status_code in (200, 201):
			return {
				"success": True,
				"payment_link_url": data.get("payment_link_url"),
				"payment_link_id": data.get("payment_link_id"),
				"status": data.get("status"),
				"raw": data,
			}
		return {
			"success": False,
			"error": _extract_error(data) or _("Failed to create payment link"),
			"raw": data,
		}

	def get_payment_link_status(self, merchant_ref: str) -> dict:
		"""GET /api/pay/v1/paymentlink/ref/{merchant_payment_link_reference}.

		URL pattern matches Pine Labs' published Pay-by-Link API docs:
		https://developer.pinelabsonline.com/reference/payment-link-get-by-merchant-payment-link-reference
		"""
		token, error = self._get_plural_access_token()
		if not token:
			return {"success": False, "error": error}

		url = f"{self._get_base_url()}/api/pay/v1/paymentlink/ref/{merchant_ref}"
		headers = self._headers(token)
		req_dump = (
			f"[PineLabs PayByLink] Status Request:\n"
			f"  GET {url}\n"
			f"  Headers: {json.dumps(headers, indent=2)}"
		)
		print(req_dump)
		frappe.logger("pinelabs_paybylink", file_count=5).info(req_dump)
		try:
			response = requests.get(
				url,
				headers=headers,
				timeout=30,
				verify=True,
			)
			data = response.json() if response.text else {}
		except Exception as exc:
			print(f"[PineLabs PayByLink] Status ERROR: {exc}")
			frappe.logger("pinelabs_paybylink", file_count=5).error(f"[PineLabs PayByLink] Status ERROR: {exc}")
			return {"success": False, "error": str(exc)}

		resp_dump = (
			f"[PineLabs PayByLink] Status Response:\n"
			f"  Status: {response.status_code}\n"
			f"  Body: {json.dumps(data, indent=2, default=str)}"
		)
		print(resp_dump)
		frappe.logger("pinelabs_paybylink", file_count=5).info(resp_dump)
		if response.status_code == 200:
			return {"success": True, "status": data.get("status"), "data": data}
		return {
			"success": False,
			"error": _extract_error(data) or f"HTTP {response.status_code}",
			"raw": data,
		}

	# ─────────────────────────────────────────────────────────────────────
	# Internals
	# ─────────────────────────────────────────────────────────────────────
	# The single-underscore prefix here marks "intended for use within the
	# api package" — config.test_plural_connection still calls
	# _get_plural_access_token directly via the public alias above.

	def _get_base_url(self) -> str:
		"""Resolve the Plural base URL.

		Priority: Settings.plural_base_url (typed by the user) >
				  site_config `pine_plural_base_url` >
				  sandbox default.
		The Settings field is editable, so a value typed there is the
		single source of truth. site_config remains as a deploy-time
		fallback for environments where the DB row isn't seeded.
		"""
		typed = (getattr(self.settings, "plural_base_url", None) or "").strip()
		if typed:
			return typed.rstrip("/")
		site_override = (frappe.conf.get("pine_plural_base_url") or "").strip()
		if site_override:
			return site_override.rstrip("/")
		return PLURAL_SANDBOX_URL.rstrip("/")

	def _get_credentials(self) -> tuple[str | None, str | None]:
		"""Resolve Plural credentials with priority: Settings > site_config."""
		if self._creds_cache is not None:
			return self._creds_cache

		client_id = (getattr(self.settings, "plural_client_id", None) or "").strip()
		client_secret = None
		try:
			client_secret = self.settings.get_password("plural_client_secret", raise_exception=False)
		except Exception:
			client_secret = None
		if not (client_id and client_secret):
			client_id = frappe.conf.get("pine_plural_client_id") or client_id
			client_secret = frappe.conf.get("pine_plural_client_secret") or client_secret

		self._creds_cache = (client_id or None, client_secret or None)
		return self._creds_cache

	def _headers(self, token: str) -> dict:
		"""Build the standard Plural header set for any authenticated call."""
		client_id, client_secret = self._get_credentials()
		return {
			"Content-Type": "application/json",
			"Accept": "application/json",
			"Authorization": f"Bearer {token}",
			"Request-ID": str(uuid.uuid4()),
			"Request-Timestamp": _request_timestamp(),
			"x-plural-client-id": client_id or "",
			"x-plural-client-secret": client_secret or "",
		}

	def _get_plural_access_token(self) -> tuple[str | None, str | None]:
		"""Fetch (or reuse cached) OAuth2 bearer token from Plural.

		Returns ``(token, error)``. Cap the cache TTL at 3500 s so the token
		never expires mid-request even if Plural says ``expires_in: 3600+``.
		"""
		cached = frappe.cache().get_value("pine_plural_access_token")
		if cached:
			return cached, None

		client_id, client_secret = self._get_credentials()
		if not (client_id and client_secret):
			return None, _("Plural credentials missing in Pinelabs Settings.")

		url = f"{self._get_base_url()}/api/auth/v1/token"
		payload = {
			"client_id": client_id,
			"client_secret": client_secret,
			"grant_type": "client_credentials",
		}
		req_dump = (
			f"[PineLabs PayByLink] Token Request:\n"
			f"  POST {url}\n"
			f"  Body: {json.dumps(payload, indent=2)}"
		)
		print(req_dump)
		frappe.logger("pinelabs_paybylink", file_count=5).info(req_dump)
		try:
			response = requests.post(url, json=payload, timeout=15, verify=True)
			data = response.json() if response.text else {}
		except Exception as exc:
			print(f"[PineLabs PayByLink] Token ERROR: {exc}")
			frappe.logger("pinelabs_paybylink", file_count=5).error(f"[PineLabs PayByLink] Token ERROR: {exc}")
			return None, str(exc)

		resp_dump = (
			f"[PineLabs PayByLink] Token Response:\n"
			f"  Status: {response.status_code}\n"
			f"  Body: {json.dumps(data, indent=2, default=str)}"
		)
		print(resp_dump)
		frappe.logger("pinelabs_paybylink", file_count=5).info(resp_dump)
		token = data.get("access_token")
		if not token:
			return None, data.get("message") or _("Failed to obtain Plural access token")

		expires_in = max(int(data.get("expires_in") or 3600) - 60, 60)
		expires_in = min(expires_in, 3500)
		frappe.cache().set_value("pine_plural_access_token", token, expires_in_sec=expires_in)
		return token, None


# ─────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ─────────────────────────────────────────────────────────────────────────


def get_plural_client() -> PluralClient:
	"""Return a fresh PluralClient bound to the current Pinelabs Settings."""
	return PluralClient()


def _request_timestamp() -> str:
	"""ISO 8601 UTC timestamp with microsecond precision (Plural API Basics)."""
	return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _extract_error(data) -> str | None:
	"""Pull a human-readable error string out of a Plural error response."""
	if not isinstance(data, dict):
		return None
	err = data.get("error")
	if isinstance(err, dict):
		return err.get("message") or err.get("code")
	if isinstance(err, str):
		return err
	return data.get("message")
