# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Server-side gates for the payment entry points.

Why this module exists
----------------------
Every switch in ``Pinelabs Settings`` used to be enforced **only in the
browser** — ``public/js/pinelabs_buttons.js`` reads the Settings single and
decides which buttons to draw. The whitelisted API methods themselves ran no
such check, so turning a switch off hid the button but left the endpoint
callable by any logged-in user:

  - ``enabled`` (master switch)      — gated the Terminal flow via
    ``pinelabs_client``, but the Plural flow never read it at all.
  - ``enable_payment_links``         — gated the button and
    ``config.test_plural_connection``, never ``initiate_payment_link``.
  - ``payable_doctypes`` rows        — read only by the JS; the API accepted
    any doctype, listed or not.
  - No permission check of any kind  — a zero-privilege user could start a
    real card charge against any document, and read/cancel any transaction.

These helpers close that gap. They are called at the top of both
``initiate_*`` entry points (before anything touches Pine Labs / Plural, so a
refused call never leaves a customer charged) and on the transaction-scoped
endpoints that take a bare ``transaction_name``.

Deliberately NOT enforced here
------------------------------
``show_on_docstatus`` stays a presentation-only hint. It says *show the
button on drafts / submitted docs*; the real docstatus rules live in each
flow's ``_load_eligible_source`` (Terminal accepts drafts and auto-submits
them at finalize time, Plural requires a submitted doc). Enforcing the
display hint here would break the documented draft flow.

There is **no permissive path**: a doctype must have an enabled row in
Pinelabs Settings → Payable Documents before any payment can be started on it.
Previously a doctype with no row was let through, which meant any user with
write access to *any* document type could arm the store's terminal and, with
``create_payment_entry=1``, have a Payment Entry posted that they could never
create themselves. Configuration is now the allow-list.
"""

import frappe
from frappe import _
from frappe.utils import cint

# flow → (Payable Documents per-flow toggle, human label)
_FLOW_TOGGLE = {
	"Terminal": ("enable_pay_on_machine", "Pay on Machine"),
	"Plural": ("enable_send_payment_link", "Send Payment Link"),
}


def assert_integration_enabled(flow):
	"""Refuse when the integration (or this flow) is switched off in Settings.

	``enabled`` is the master switch and covers both flows —
	``pinelabs_client`` already enforced it for Terminal, but nothing enforced
	it for Plural, so a payment link could still be created and delivered to a
	customer with the integration fully "off".
	"""
	settings = frappe.get_single("Pinelabs Settings")

	if not cint(settings.enabled):
		frappe.throw(
			_("Pine Labs integration is not enabled. Tick 'Enabled' in Pinelabs Settings."),
			title=_("Integration Disabled"),
		)

	if flow == "Plural" and not cint(getattr(settings, "enable_payment_links", 0)):
		frappe.throw(
			_("Payment Links are not enabled. Tick 'Enable Payment Links' in Pinelabs Settings."),
			title=_("Payment Links Disabled"),
		)


def assert_doctype_payable(reference_doctype, flow):
	"""Refuse unless the doctype has an enabled Payable Documents row.

	Both ``is_enabled`` and the per-flow toggle must be on. A doctype with no
	row is refused — configuration is the allow-list.
	"""
	from pinelabs_fin.api import transaction as txn_service

	config = txn_service._get_payable_config(reference_doctype)
	if not config:
		frappe.throw(
			_("Pine Labs payments are not configured for {0}. Add it under Pinelabs Settings → Payable Documents first.").format(
				reference_doctype
			),
			title=_("Document Type Not Configured"),
		)

	if not cint(config.get("is_enabled")):
		frappe.throw(
			_("Pine Labs payments are disabled for {0}. Tick 'Enabled' on its row in Pinelabs Settings → Payable Documents.").format(
				reference_doctype
			),
			title=_("Document Type Disabled"),
		)

	toggle_field, label = _FLOW_TOGGLE.get(flow, (None, None))
	if toggle_field and not cint(config.get(toggle_field, 1)):
		frappe.throw(
			_("'{0}' is disabled for {1}. Tick it on the {1} row in Pinelabs Settings → Payable Documents.").format(
				label, reference_doctype
			),
			title=_("Payment Method Disabled"),
		)


def _denied(reference_doctype, reference_name):
	# One message for "does not exist" and "not permitted", so a caller without
	# access cannot use the error text to probe which documents exist. Frappe's
	# permission check queues its own explanation ("User X does not have
	# doctype access … for document Sales Invoice"), which would sit beside ours
	# in the response and give the game away — drop whatever it queued.
	frappe.clear_messages()
	frappe.throw(
		_("{0} {1} was not found, or you do not have permission to take a payment on it.").format(
			reference_doctype, reference_name
		),
		frappe.PermissionError,
	)


def assert_can_pay(reference_doctype, reference_name, source_doc=None):
	"""Refuse when the session user may not take a payment on the source document.

	Taking a payment against a document mutates its financial state (a
	Payment Entry is posted against it, or a reference field on it is
	written), so write permission on the source doc is the bar. A **draft**
	is auto-submitted when the payment succeeds, so for a draft of a
	submittable doctype the user also needs submit permission — otherwise a
	user who can edit but not submit invoices could post one just by taking a
	payment.

	Administrator and the cron/webhook contexts (which run as Administrator)
	bypass this the same way they bypass every other Frappe permission check.
	"""
	if not frappe.db.exists(reference_doctype, reference_name):
		_denied(reference_doctype, reference_name)

	doc = source_doc or frappe.get_doc(reference_doctype, reference_name)
	# print_logs=False: otherwise Frappe queues its own explanatory message
	# ("User X does not have doctype access … for document Sales Invoice"),
	# which reaches the client next to ours and defeats the uniform error.
	if not frappe.permissions.has_permission(reference_doctype, "write", doc=doc, print_logs=False):
		_denied(reference_doctype, reference_name)

	if (
		cint(getattr(doc, "docstatus", 0)) == 0
		and frappe.get_meta(reference_doctype).is_submittable
		and not frappe.permissions.has_permission(reference_doctype, "submit", doc=doc, print_logs=False)
	):
		frappe.throw(
			_("{0} {1} is a draft and a payment would submit it; you do not have permission to submit {0}.").format(
				reference_doctype, reference_name
			),
			frappe.PermissionError,
		)


def assert_initiate_allowed(reference_doctype, reference_name, *, flow, source_doc=None):
	"""Single gate for both ``initiate_*`` entry points.

	Runs the Settings switches, then the per-doctype row, then the permission
	check — cheapest and most likely-to-refuse first. Must be called BEFORE
	the gateway is contacted so a refusal can never leave a customer charged.
	"""
	assert_integration_enabled(flow)
	assert_doctype_payable(reference_doctype, flow)
	assert_can_pay(reference_doctype, reference_name, source_doc=source_doc)


def assert_txn_access(txn):
	"""Refuse when the session user may not act on this transaction.

	The transaction-scoped endpoints (``poll_terminal_status``,
	``check_terminal_status``, ``cancel_terminal_payment``,
	``retry_failed_terminal_payment``, ``refresh_payment_link_status``) take a
	bare ``transaction_name``. Without this check any logged-in user could
	read another user's payment records, or cancel a live payment they have
	nothing to do with, just by guessing a ``PLT-…`` name.

	Access is derived from the *source document*, not the transaction row, so
	it stays consistent with :func:`assert_can_pay` — whoever may take the
	payment may also watch and cancel it.

	A transaction whose source doc has since been deleted falls back to a read
	check on Pinelabs Transaction itself, so the row stays reachable for
	reconciliation instead of becoming permanently inaccessible.
	"""
	reference_doctype = getattr(txn, "reference_doctype", None)
	reference_name = getattr(txn, "reference_name", None)

	if reference_doctype and reference_name and frappe.db.exists(reference_doctype, reference_name):
		assert_can_pay(reference_doctype, reference_name)
		return

	frappe.has_permission(doctype="Pinelabs Transaction", ptype="read", throw=True)


def load_transaction_for_user(transaction_name):
	"""Load a Pinelabs Transaction by name, enforcing :func:`assert_txn_access`.

	"Does not exist" and "not permitted" share one message, so a caller cannot
	enumerate ``PLT-…`` names (they are sequential and guessable) by comparing
	error text.
	"""
	if not transaction_name or not frappe.db.exists("Pinelabs Transaction", transaction_name):
		frappe.throw(
			_("Pinelabs Transaction {0} was not found, or you do not have permission to access it.").format(
				transaction_name
			),
			frappe.PermissionError,
		)
	txn = frappe.get_doc("Pinelabs Transaction", transaction_name)
	try:
		assert_txn_access(txn)
	except frappe.PermissionError:
		# Re-raise with the SAME message as "not found": the source-document
		# denial would otherwise name the invoice behind the transaction.
		frappe.clear_messages()
		frappe.throw(
			_("Pinelabs Transaction {0} was not found, or you do not have permission to access it.").format(
				transaction_name
			),
			frappe.PermissionError,
		)
	return txn


@frappe.whitelist(methods=["POST"])
def get_payable_doctypes() -> dict:
	"""Which doctypes get Pine Labs buttons — the only Settings data the UI needs.

	``pinelabs_buttons.js`` used to read the whole ``Pinelabs Settings`` single
	with ``frappe.client.get``. That doctype is System Manager-only (it holds
	machine ids and routing), so the cashiers who actually take payments got a
	403 and no buttons — and the easy "fix" is to grant them Settings read,
	which exposes everything. This returns just the per-doctype switches.

	The integration master switch and ``enable_payment_links`` are folded in,
	so a switched-off integration yields no buttons. The endpoint decides what
	to *draw*; the ``initiate_*`` methods still enforce every rule server-side.
	"""
	settings = frappe.get_cached_doc("Pinelabs Settings")
	if not cint(settings.enabled):
		return {"payable_doctypes": []}

	links_on = cint(getattr(settings, "enable_payment_links", 0))
	rows = []
	for row in settings.payable_doctypes or []:
		if not (row.doctype_name and cint(row.is_enabled)):
			continue
		rows.append(
			{
				"doctype_name": row.doctype_name,
				"is_enabled": 1,
				"enable_pay_on_machine": cint(row.enable_pay_on_machine),
				"enable_send_payment_link": 1 if (links_on and cint(row.enable_send_payment_link)) else 0,
				"show_on_docstatus": row.show_on_docstatus,
			}
		)
	return {"payable_doctypes": rows}
