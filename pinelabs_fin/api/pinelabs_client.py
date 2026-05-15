# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""HTTP client for Pine Labs APIs.

Wraps three distinct surfaces behind one ``PineLabsClient`` class:

  * **Pine Labs Cloud** terminal endpoints (UploadBilledTransaction,
    GetCloudBasedTxnStatus, CancelTransaction) — used by the Pay-on-Machine
    flow in ``api/terminal.py``.
  * **Plural Pay-by-Link** (OAuth token + payment-link create/get) — used
    by the Send Payment Link flow in ``api/plural.py``.

The class pulls credentials from ``Pinelabs Settings`` (and per-machine
``Pinelabs Machine Config`` rows). Token caching, signature hashing, and
response parsing live here so the API modules stay focused on the state
machine in ``api/transaction.py``.

A future refactor (Phase 3 of the standards pass) splits this into
per-surface modules under ``api/clients/``.
"""

import hashlib
import json
import uuid
from datetime import datetime

import frappe
import requests
from frappe import _
from frappe.utils import cint, now_datetime


class PineLabsClient:
	"""
	Client for Pine Labs Cloud API
	Handles payment initiation, status checks, and cancellation

	Supports both single machine mode (legacy) and multiple machine mode
	"""

	def __init__(self, machine_name=None):
		"""
		Initialize the Pine Labs client.

		Args:
			machine_name: Optional machine name. If omitted, falls back to the
						  first enabled machine in Settings. Routing-based
						  resolution (user / warehouse / company / POS profile)
						  lives in
						  ``pinelabs_settings.resolve_machine_for_context`` and
						  must be done by the caller before constructing the
						  client.
		"""
		self.settings = frappe.get_single("Pinelabs Settings")

		if not self.settings.enabled:
			frappe.throw(_("Pine Labs integration is not enabled"))

		# If no machine_name yet, fall back to the first enabled machine.
		if not machine_name:
			for m in (self.settings.machines or []):
				if m.enabled:
					machine_name = m.machine_name
					break

		if not machine_name:
			frappe.throw(
				_("No Pinelabs machine configured. "
				  "Please add at least one machine in Pinelabs Settings.")
			)

		# Load machine configuration from child table.
		self.machine = self._load_machine_from_child_table(machine_name)
		self.machine_name = machine_name


	def _load_machine_from_child_table(self, machine_name):
		"""Load machine configuration from settings child table"""
		if not self.settings.machines:
			frappe.throw(_("No machines configured in Pinelabs Settings"))

		# Find machine in child table
		machine = None
		for m in self.settings.machines:
			if m.machine_name == machine_name:
				machine = m
				break

		if not machine:
			frappe.throw(_("Pinelabs Machine '{0}' not found in Settings").format(machine_name))

		if not machine.enabled:
			frappe.throw(_("Pinelabs Machine '{0}' is not enabled").format(machine_name))

		# Get active API endpoint from global settings
		active_endpoint = None
		if self.settings.api_endpoints:
			for endpoint in self.settings.api_endpoints:
				if endpoint.is_active:
					active_endpoint = endpoint
					break

		if not active_endpoint:
			frappe.throw(
				_("No active API endpoint configured in Pinelabs Settings. "
				  "Please add at least one active endpoint.")
			)

		self.base_url = (
			getattr(active_endpoint, "base_api_url", None)
			or getattr(active_endpoint, "api_url", None)
		)
		if not self.base_url:
			frappe.throw(_("Active API endpoint URL is not configured for machine '{0}'").format(machine_name))

		# Store endpoint paths
		self.upload_endpoint = (
			getattr(active_endpoint, "upload_transaction_endpoint", None)
			or getattr(active_endpoint, "upload_endpoint", None)
			or "/API/CloudBasedIntegration/V1/UploadBilledTransaction"
		)
		self.status_endpoint = (
			getattr(active_endpoint, "get_status_endpoint", None)
			or getattr(active_endpoint, "status_endpoint", None)
			or "/API/CloudBasedIntegration/V1/GetCloudBasedTxnStatus"
		)
		self.cancel_endpoint = (
			getattr(active_endpoint, "cancel_transaction_endpoint", None)
			or getattr(active_endpoint, "cancel_endpoint", None)
			or "/API/CloudBasedIntegration/V1/CancelTransaction"
		)

		# Store credentials from machine
		self.merchant_id = machine.merchant_id
		self.security_token = machine.get_password("security_token")
		self.client_id = machine.client_id
		self.store_id = machine.store_id
		self.device_number = machine.device_number or ""
		# Auto-cancel: per-machine override > Pinelabs Settings > default 3
		machine_mins = getattr(machine, "auto_cancel_duration_in_minutes", None)
		if machine_mins is not None and cint(machine_mins) > 0:
			self.auto_cancel_duration = cint(machine_mins)
		else:
			settings_mins = getattr(self.settings, "auto_cancel_duration_minutes", None)
			if settings_mins is not None and cint(settings_mins) > 0:
				self.auto_cancel_duration = cint(settings_mins)
			else:
				self.auto_cancel_duration = 3

		# Validate required credentials
		if not all([self.merchant_id, self.security_token, self.client_id, self.store_id]):
			frappe.throw(
				_("Please configure all required Pine Labs credentials for machine '{0}'").format(machine_name)
			)

		return machine

	def _generate_transaction_reference(self):
		"""Generate unique transaction reference"""
		timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
		unique_id = str(uuid.uuid4())[:8].upper()
		return f"TXN{timestamp}{unique_id}"

	def _calculate_hash(self, data_string):
		"""
		Calculate SHA256 hash for authentication
		Format: MerchantID|SecurityToken|DataString
		"""
		hash_string = f"{self.merchant_id}|{self.security_token}|{data_string}"
		return hashlib.sha256(hash_string.encode()).hexdigest()

	def _prepare_request(self, endpoint, payload):
		"""Prepare request URL and headers"""
		url = f"{self.base_url}{endpoint}"
		headers = {"Content-Type": "application/json"}
		return url, headers

	def _handle_request_exception(self, exception):
		"""Handle request exceptions and return error response"""
		if isinstance(exception, requests.exceptions.Timeout):
			frappe.logger().error("Pine Labs API request timeout")
			return {
				"success": False,
				"error": _("Request timeout - Pine Labs server did not respond"),
				"status_code": 0
			}
		elif isinstance(exception, requests.exceptions.ConnectionError):
			frappe.logger().error("Pine Labs API connection error")
			return {
				"success": False,
				"error": _("Connection error - Unable to reach Pine Labs server"),
				"status_code": 0
			}
		else:
			frappe.logger().error(f"Pine Labs API error: {exception!s}")
			return {
				"success": False,
				"error": str(exception),
				"status_code": 0
			}

	def _parse_transaction_data_array(self, transaction_data_array):
		"""
		Extract values from TransactionData array

		Args:
			transaction_data_array: List of {Tag: "key", Value: "value"} objects

		Returns:
			Dictionary with extracted values
		"""
		extracted = {}
		if not transaction_data_array:
			return extracted

		for item in transaction_data_array:
			if isinstance(item, dict):
				tag = item.get("Tag", "").lower()
				value = item.get("Value", "")

				# Map common tags to standardized field names
				tag_mapping = {
					"rrn": "RRN",
					"approvalcode": "ApprovalCode",
					"approval code": "ApprovalCode",
					"cardnumber": "CardNumber",
					"cardtype": "CardType",
					"cardholdername": "CardHolderName",
					"bankname": "BankName",
					"tid": "TID",
					"mid": "MID",
					"paymentmode": "PaymentMode",
					"amount": "Amount",
					"invoicenumber": "InvoiceNumber"
				}

				mapped_key = tag_mapping.get(tag, tag.upper())
				if value:
					extracted[mapped_key] = value

		return extracted

	def _parse_response(self, response):
		"""Parse API response and return structured data"""
		try:
			response_data = response.json() if response.text else {}
		except (ValueError, json.JSONDecodeError) as e:
			frappe.logger().warning(
				f"Failed to parse JSON response: {e!s}, "
				f"Response text: {response.text[:500] if response.text else 'Empty'}"
			)
			response_data = {}

		# Extract values from TransactionData array if present
		if "TransactionData" in response_data:
			extracted_data = self._parse_transaction_data_array(response_data.get("TransactionData", []))
			# Merge extracted data with response_data (extracted takes precedence)
			response_data.update(extracted_data)

		frappe.logger().info(
			f"Pine Labs API Response received: "
			f"HTTP Status={response.status_code}, "
			f"Has Data={bool(response_data)}, "
			f"Response Keys={list(response_data.keys()) if isinstance(response_data, dict) else 'N/A'}, "
			f"Full Response={json.dumps(response_data, indent=2) if response_data else 'Empty'}"
		)
		return {
			"success": response.status_code == 200,
			"status_code": response.status_code,
			"data": response_data,
			"raw_response": response.text
		}

	def _make_request(self, endpoint, payload):
		"""
		Make HTTP request to Pine Labs API

		Args:
			endpoint: API endpoint (e.g., '/API/CloudBasedIntegration/V1/UploadBilledTransaction')
			payload: Request payload dictionary

		Returns:
			Response dictionary
		"""
		url, headers = self._prepare_request(endpoint, payload)
		frappe.logger().info(f"Pine Labs API Request to {url}: {json.dumps(payload, indent=2)}")

		try:
			# SSL verification - can be configured per endpoint if needed
			# For now, disable SSL verification (UAT servers often use self-signed certificates)
			# In production, consider enabling SSL verification
			verify_ssl = False
			response = requests.post(url, headers=headers, json=payload, timeout=30, verify=verify_ssl)
			return self._parse_response(response)
		except Exception as e:
			return self._handle_request_exception(e)

	def _build_transaction_payload(self, transaction_ref, amount, payment_mode):
		"""
		Build request payload for transaction upload

		Note: Pine Labs API expects:
		- Amount in paisa (smallest currency unit for INR) as integer
		- Example: 159.00 INR = 15900 paisa
		- SequenceNumber as integer
		- AllowedPaymentMode: 1=Card, 11=UPI
		"""
		allowed_payment_mode = 1 if payment_mode == "Card" else 11

		# Pine Labs API expects amount in paisa (smallest currency unit)
		# Convert rupees to paisa: multiply by 100
		# Example: 159.00 INR → 15900 paisa
		amount_in_paisa = int(float(amount) * 100)

		# Get auto-cancel duration (from machine or settings)
		auto_cancel_duration = getattr(self, 'auto_cancel_duration', 1)

		payload = {
			"MerchantID": str(self.merchant_id),
			"SecurityToken": str(self.security_token),
			"ClientID": str(self.client_id),
			"StoreID": str(self.store_id),
			"TransactionNumber": str(transaction_ref),
			"SequenceNumber": 1,  # Integer, not string
			"AllowedPaymentMode": allowed_payment_mode,
			"Amount": amount_in_paisa,  # Amount in paisa as integer (e.g., 15900 for 159.00 INR)
			"TotalInvoiceAmount": amount_in_paisa,  # Same as Amount
			"UserID": str(frappe.session.user),
			"AutoCancelDurationInMinutes": int(auto_cancel_duration),
			"ForceCancelOnBack": False,
			"IsMQTTDisabled": False
		}

		# # Add DeviceNumber if configured
		# if self.device_number:
		#     payload["DeviceNumber"] = str(self.device_number)

		# Validate payload fields are not empty
		for key, value in payload.items():
			if value is None or (isinstance(value, str) and not value.strip()):
				frappe.logger().error(f"Pine Labs payload validation failed: {key} is empty or None")
				frappe.throw(_("Invalid payload: {0} cannot be empty").format(key))

		# Log the payload for debugging
		frappe.logger().info(
			f"Pine Labs Payload - Amount: {amount} INR = {amount_in_paisa} paisa, "
			f"Mode: {payment_mode}, AutoCancel: {auto_cancel_duration} min"
		)

		return payload

	def _handle_transaction_response(self, response, transaction_ref, transaction_log):
		"""Handle transaction upload response"""
		# Check HTTP response success
		if not response.get("success"):
			transaction_log.status = "Failed"
			transaction_log.error_message = response.get("error", _("Unknown error"))
			transaction_log.save()
			frappe.db.commit()
			return {
				"success": False,
				"transaction_reference": transaction_ref,
				"transaction_log": transaction_log.name,
				"error": response.get("error", _("Failed to initiate payment"))
			}

		# Check Pine Labs API response code in the response body
		response_data = response.get("data", {})
		response_code = response_data.get("ResponseCode")
		response_message = response_data.get("ResponseMessage", "")

		# Log the full response for debugging
		frappe.logger().info(
			f"Pine Labs UploadBilledTransaction Response: "
			f"ResponseCode={response_code}, ResponseMessage={response_message}, "
			f"Full Response={json.dumps(response_data, indent=2)}"
		)

		# ResponseCode meanings for Upload Billed Transaction:
		# 0 = Transaction accepted for processing (should remain Pending until terminal completes)
		# 1 = INVALID INPUT (payload validation error - truly declined)
		# 2 = Other decline reasons (truly declined)
		# Other values = Error/Declined

		if response_code == 0:
			# Transaction accepted for processing - keep as Pending
			# Status will be updated to Approved/Declined after terminal processes payment
			transaction_log.status = "Pending"

			# Store PlutusTransactionReferenceID from upload response
			# This is required for GetStatus API calls
			plutus_ref_id = response_data.get("PlutusTransactionReferenceID")
			if plutus_ref_id:
				transaction_log.external_reference = str(plutus_ref_id)
				frappe.logger().info(
					f"Stored PlutusTransactionReferenceID {plutus_ref_id} for transaction {transaction_ref}"
				)

			transaction_log.response_payload = json.dumps(response_data, indent=2)
			transaction_log.save()
			frappe.db.commit()

			return {
				"success": True,
				"transaction_reference": transaction_ref,
				"transaction_log": transaction_log.name,
				"message": _("Payment initiated successfully. Please complete payment on terminal."),
				"response_code": response_code,
				"response_message": response_message
			}
		elif response_code is not None and response_code != 0:
			# Transaction was declined/rejected by Pine Labs at upload stage
			# This is a true decline (invalid payload, etc.)
			transaction_log.status = "Declined"
			transaction_log.error_code = str(response_code)

			# Provide more detailed error message for ResponseCode 1 (INVALID INPUT)
			if response_code == 1:
				error_msg = response_message or _("INVALID INPUT - Please check: Amount format (should be in paisa), TransactionDateTime format, and all required fields")
				frappe.logger().error(
					f"Pine Labs INVALID INPUT error for transaction {transaction_ref}. "
					f"ResponseMessage: {response_message}. "
					f"This usually indicates a payload format issue. Check: "
					f"1. Amount is in paisa (e.g., 12000 for 120.00 INR), "
					f"2. TransactionDateTime format is correct (YYYY-MM-DD HH:MM:SS), "
					f"3. All required fields are present and valid."
				)
			else:
				error_msg = response_message or _("Transaction declined by Pine Labs")

			transaction_log.error_message = error_msg
			transaction_log.response_payload = json.dumps(response_data, indent=2)
			transaction_log.save()
			frappe.db.commit()

			return {
				"success": False,
				"transaction_reference": transaction_ref,
				"transaction_log": transaction_log.name,
				"error": error_msg,
				"response_code": response_code,
				"response_message": response_message
			}
		else:
			# ResponseCode is None or unexpected - keep as Pending and let status check handle it
			transaction_log.status = "Pending"
			transaction_log.response_payload = json.dumps(response_data, indent=2)
			transaction_log.save()
			frappe.db.commit()

			return {
				"success": True,
				"transaction_reference": transaction_ref,
				"transaction_log": transaction_log.name,
				"message": _("Payment initiated. Please complete payment on terminal."),
				"response_code": response_code,
				"response_message": response_message
			}

	def _validate_payment_inputs(self, amount, payment_mode):
		"""
		Validate generic payment inputs (not POS-specific)

		Args:
			amount: Transaction amount
			payment_mode: "Card" or "UPI"

		Returns:
			Tuple (is_valid, error_message)
		"""
		from frappe.utils import flt

		amount = flt(amount)
		if amount <= 0:
			return False, _("Invalid amount. Amount must be greater than 0")

		if payment_mode not in ["Card", "UPI"]:
			return False, _("Invalid payment mode. Must be 'Card' or 'UPI'")

		return True, None

	# ─────────────────────────────────────────────────────────────────────
	# Lean pure-HTTP methods used by terminal.py. All transaction state
	# writes go through pinelabs_fin.api.transaction.
	# ─────────────────────────────────────────────────────────────────────

	def upload_billed_raw(self, transaction_ref, amount, allowed_payment_mode):
		"""Pure-HTTP wrapper around UploadBilledTransaction.

		Returns the raw response dict (the same shape `_make_request` produces),
		with ``request_payload`` added so the caller can persist exactly what
		the app sent to Pine Labs on this transaction's Pinelabs Transaction row.
		"""
		# _build_transaction_payload expects "Card" or "UPI" — translate from int code.
		mode = "Card" if int(allowed_payment_mode) == 1 else "UPI"
		payload = self._build_transaction_payload(transaction_ref, amount, mode)
		response = self._make_request(self.upload_endpoint, payload)
		response["request_payload"] = payload
		return response

	def get_status_raw(self, plutus_ref_id):
		"""Pure-HTTP wrapper around GetCloudBasedTxnStatus."""
		payload = {
			"MerchantID": self.merchant_id,
			"SecurityToken": self.security_token,
			"ClientID": self.client_id,
			"StoreID": self.store_id,
			"PlutusTransactionReferenceID": int(plutus_ref_id),
		}
		response = self._make_request(self.status_endpoint, payload)
		response["request_payload"] = payload
		return response

	def cancel_raw(self, plutus_ref_id):
		"""Pure-HTTP wrapper around CancelTransaction."""
		payload = {
			"MerchantID": self.merchant_id,
			"SecurityToken": self.security_token,
			"PlutusTransactionReferenceID": int(plutus_ref_id),
			"UserID": frappe.session.user,
		}
		response = self._make_request(self.cancel_endpoint, payload)
		response["request_payload"] = payload
		return response

	# ─────────────────────────────────────────────────────────────────────
	# Plural API plumbing — moved to ``api/plural_client.py``.
	# Use ``from pinelabs_fin.api.plural_client import get_plural_client``.
	# ─────────────────────────────────────────────────────────────────────



# Utility function to get client instance
def get_pinelabs_client(machine_name=None):
	"""
	Get PineLabsClient instance.

	Routing-based machine resolution lives in
	pinelabs_settings.resolve_machine_for_context — call that first and pass
	the resolved machine_name in.
	"""
	return PineLabsClient(machine_name=machine_name)
