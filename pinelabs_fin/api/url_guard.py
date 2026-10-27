# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Validate the gateway base URLs before credentials are sent to them.

The Pine Labs terminal API and the Plural API are configured as free-text
URLs. Every request carries live credentials (the terminal ``SecurityToken``,
the Plural client secret and Bearer token), so a URL that points anywhere
else — or that downgrades to ``http`` / disables certificate checks — hands
those credentials to whoever controls that host or sits on the network path.
It also lets a spoofed "approved" response book a Payment Entry.

Rules:
  - the scheme must be ``https``;
  - there must be a host, and no ``user:pass@`` in the URL;
  - ``verify_ssl`` may not be switched off;
  - the exceptions are loopback hosts (``localhost`` / ``127.0.0.0/8`` /
    ``::1``, used by the local simulator) and sites running with
    ``developer_mode``, which may use plain http and skip verification.
"""

import ipaddress
from urllib.parse import urlparse

import frappe
from frappe import _


def _is_loopback(host):
	if not host:
		return False
	if host.lower() == "localhost":
		return True
	try:
		return ipaddress.ip_address(host).is_loopback
	except ValueError:
		return False


def _relaxed(host):
	return _is_loopback(host) or bool(frappe.conf.get("developer_mode"))


def assert_gateway_url(url, label, verify_ssl=True):
	"""Throw unless ``url`` is safe to send gateway credentials to."""
	url = (url or "").strip()
	parsed = urlparse(url)
	host = parsed.hostname

	if parsed.scheme not in ("http", "https") or not host:
		frappe.throw(_("{0} must be a full URL such as https://host (got '{1}').").format(label, url))
	if parsed.username or parsed.password:
		frappe.throw(_("{0} must not contain credentials in the URL.").format(label))

	relaxed = _relaxed(host)
	if parsed.scheme != "https" and not relaxed:
		frappe.throw(
			_("{0} must use https. Plain http is only allowed for localhost or in developer mode.").format(label)
		)
	if not verify_ssl and not relaxed:
		frappe.throw(
			_("{0}: SSL verification cannot be disabled outside localhost / developer mode.").format(label)
		)
