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

# Matched case-insensitively as SUBSTRINGS of the key, after stripping - and _,
# so "client_secret", "clientSecret", "Client-Secret" and the Plural header
# "x-plural-client-secret" all hit. Exact-name matching missed that header
# (it normalises to "xpluralclientsecret"), leaking the merchant's client
# secret on every status poll.
_SECRET_MARKERS = (
	"secret",
	"token",
	"authorization",
	"password",
	"apikey",
	"xverify",
	"signature",
)


# Customer contact details. Masked only when a caller asks (``pii=True``) —
# log lines do, because log files are widely readable; the payload stored on
# the Pinelabs Transaction keeps them for debugging a specific payment.
_PII_MARKERS = ("email", "mobile", "phone", "contactnumber")


def _is_secret(key, pii=False) -> bool:
	norm = str(key).replace("-", "").replace("_", "").lower()
	if any(marker in norm for marker in _SECRET_MARKERS):
		return True
	return pii and any(marker in norm for marker in _PII_MARKERS)


def redact(value, *, pii=False):
	"""Return a copy of ``value`` with secret-bearing keys masked.

	Recurses through dicts and lists. Non-container values pass through
	unchanged — a bare string is not inspected, because only the *key* tells
	us whether the content is sensitive. The input is never mutated. With
	``pii=True`` customer email / phone keys are masked as well.
	"""
	if isinstance(value, dict):
		return {k: (MASK if _is_secret(k, pii) else redact(v, pii=pii)) for k, v in value.items()}
	if isinstance(value, (list, tuple)):
		return [redact(v, pii=pii) for v in value]
	return value
