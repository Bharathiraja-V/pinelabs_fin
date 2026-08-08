# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Mask gateway credentials before they reach a log file.

Both HTTP clients dump full request and response bodies at INFO — useful for
diagnosing a payment, except the bodies carry live credentials:

- ``pinelabs_client`` logs the UploadBilledTransaction payload, which
  includes ``SecurityToken`` (a Password field on Pinelabs Machine Config).
- ``plural_client`` logs the OAuth token request (``client_secret``), the
  token response (``access_token``), and the ``Authorization: Bearer`` header
  on every subsequent call.

Those land in the ``bench start`` terminal and in
``logs/pinelabs_paybylink.log``, so anyone with log access had the merchant's
gateway credentials in cleartext. Everything here is best-effort formatting
for humans — never parse a redacted dump back into a request.
"""

MASK = "***REDACTED***"

# Matched case-insensitively against dict keys, after stripping - and _ so
# "client_secret", "clientSecret" and "Client-Secret" all hit.
_SECRET_KEYS = frozenset(
	{
		"securitytoken",
		"clientsecret",
		"accesstoken",
		"refreshtoken",
		"authorization",
		"xverify",
		"password",
		"secret",
		"apikey",
	}
)


def _is_secret(key) -> bool:
	return str(key).replace("-", "").replace("_", "").lower() in _SECRET_KEYS


def redact(value):
	"""Return a copy of ``value`` with secret-bearing keys masked.

	Recurses through dicts and lists. Non-container values pass through
	unchanged — a bare string is not inspected, because only the *key* tells
	us whether the content is sensitive. The input is never mutated.
	"""
	if isinstance(value, dict):
		return {k: (MASK if _is_secret(k) else redact(v)) for k, v in value.items()}
	if isinstance(value, (list, tuple)):
		return [redact(v) for v in value]
	return value
