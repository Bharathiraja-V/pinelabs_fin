# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Configuration helpers for Pinelabs Settings.

`test_plural_connection` powers the "Test Connection" button on the
Settings form. It forces a fresh OAuth token fetch (bypassing the cache)
and returns success/failure to the caller; the button surfaces the
result via a dialog.
"""

import frappe
from frappe import _


@frappe.whitelist()
def test_plural_connection() -> dict:
	"""Try to obtain a Plural OAuth token using the saved credentials.

	Returns:
	    {
	      "success": bool,
	      "message": str,            # human-readable status
	      "expires_in_seconds": int  # only set on success
	    }
	"""
	from pinelabs_fin.api.plural_client import get_plural_client

	# Force a fresh fetch — a cached token from before the user updated
	# credentials would mask a misconfig. Token is re-cached on success.
	frappe.cache().delete_value("pine_plural_access_token")

	settings = frappe.get_single("Pinelabs Settings")
	if not settings.enable_payment_links:
		return {"success": False, "message": _("Payment Links are not enabled in Pinelabs Settings.")}

	if not (getattr(settings, "plural_client_id", None) or "").strip():
		return {"success": False, "message": _("Plural Client ID is empty. Save the Settings form first.")}

	try:
		client = get_plural_client()
	except Exception as e:
		return {"success": False, "message": _("Could not initialise Plural client: {0}").format(str(e))}

	try:
		token, error = client.get_access_token()
	except Exception as e:
		return {"success": False, "message": _("Token request raised: {0}").format(str(e))}

	if not token:
		return {"success": False, "message": _("Failed — {0}").format(error or _("unknown error"))}

	return {
		"success": True,
		"message": _("Connected — token generated successfully (expires within ~60 min)."),
		"expires_in_seconds": 3500,
	}
