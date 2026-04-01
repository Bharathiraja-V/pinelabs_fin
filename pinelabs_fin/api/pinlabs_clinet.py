# -*- coding: utf-8 -*-
# Copyright (c) 2024, Your Company and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
import requests
import json
import hashlib
import uuid
from datetime import datetime
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime, get_url


class PineLabsClient:
    """
    Client for Pine Labs Cloud API
    Handles payment initiation, status checks, and cancellation
    
    Supports both single machine mode (legacy) and multiple machine mode
    """

    def __init__(self, machine_name=None, warehouse=None):
        """
        Initialize the Pine Labs client
        
        Args:
            machine_name: Optional machine name (for multiple machine mode)
            warehouse: Optional warehouse name (will resolve to machine)
        """
        self.settings = frappe.get_single("Pinelabs Settings")

        if not self.settings.enabled:
            frappe.throw(_("Pine Labs integration is not enabled"))

        # Always use multi-machine mode (machines configured in child table)
        # If warehouse provided, resolve machine from warehouse
        if warehouse:
            # Resolve machine from warehouse
            machine_name = self._get_machine_for_warehouse(warehouse)
            if not machine_name:
                frappe.throw(
                    _("No Pinelabs machine found for warehouse '{0}'. "
                      "Please configure machine mapping in Pinelabs Settings.").format(warehouse)
                )
        
        # If no machine_name yet, try to get first enabled machine
        if not machine_name:
            if self.settings.machines:
                # Find first enabled machine
                for m in self.settings.machines:
                    if m.enabled:
                        machine_name = m.machine_name
                        break
        
        if not machine_name:
            frappe.throw(
                _("No Pinelabs machine configured. "
                  "Please add at least one machine in Pinelabs Settings.")
            )
        
        # Load machine configuration from child table
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

    def _get_machine_for_warehouse(self, warehouse):
        """
        Get machine name for a given warehouse from settings child table
        
        Args:
            warehouse: Warehouse name
            
        Returns:
            Machine name or None if not found
        """
        if not warehouse:
            return None
        
        # Find enabled machine in settings that has this warehouse mapped
        if not self.settings.machines:
            return None
        
        for machine in self.settings.machines:
            if not machine.enabled:
                continue
            
            # Check direct warehouse field (not child table)
            if machine.warehouse == warehouse:
                return machine.machine_name
        
        return None

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
            frappe.logger().error(f"Pine Labs API error: {str(exception)}")
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
                f"Failed to parse JSON response: {str(e)}, "
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

    def upload_billed_transaction(
        self,
        invoice_name,
        amount,
        payment_mode="Card",
        source_system=None,
        source_doctype=None,
        source_docname=None,
        external_order_id=None,
    ):
        """
        Initiate payment transaction with Pine Labs

        Args:
            invoice_name: Invoice name (can be POS Invoice or any identifier)
            amount: Transaction amount
            payment_mode: "Card" or "UPI"
            source_system: Host application name (example: ERPNext)
            source_doctype: Source document type in host app
            source_docname: Source document name in host app
            external_order_id: Generic external order identifier

        Returns:
            Dictionary with transaction details
        """
        # Validate generic inputs
        is_valid, error_msg = self._validate_payment_inputs(amount, payment_mode)
        if not is_valid:
            return {
                "success": False,
                "error": error_msg
            }
        
        transaction_ref = self._generate_transaction_reference()
        payload = self._build_transaction_payload(transaction_ref, amount, payment_mode)

        endpoint = self.upload_endpoint
        response = self._make_request(endpoint, payload)

        transaction_log = self._create_transaction_log(
            transaction_reference=transaction_ref,
            invoice=invoice_name,
            amount=amount,
            payment_mode=payment_mode,
            request_payload=payload,
            response_payload=response.get("data", {}),
            status="Pending",
            machine_name=self.machine_name,
            source_system=source_system,
            source_doctype=source_doctype,
            source_docname=source_docname,
            external_order_id=external_order_id or invoice_name,
        )

        return self._handle_transaction_response(response, transaction_ref, transaction_log)

    def get_transaction_status(self, transaction_reference):
        """
        Check transaction status from Pine Labs
        
        Note: GetStatus API requires PlutusTransactionReferenceID, not TransactionNumber

        Args:
            transaction_reference: Unique transaction reference

        Returns:
            Dictionary with status details
        """
        # Validate input
        if not transaction_reference:
            return {
                "success": False,
                "error": _("Transaction reference is required"),
                "status": "Unknown"
            }
        
        # First, get the transaction log to retrieve PlutusTransactionReferenceID
        try:
            if not frappe.db.exists("Pinelabs Transaction Log", {
                "transaction_reference": transaction_reference
            }):
                return {
                    "success": False,
                    "error": _("Transaction {0} not found").format(transaction_reference),
                    "status": "Unknown"
                }
            
            log = frappe.get_doc("Pinelabs Transaction Log", {
                "transaction_reference": transaction_reference
            })
            plutus_ref_id = log.external_reference or log.get("external_reference")
            
            if not plutus_ref_id:
                frappe.logger().error(
                    f"PlutusTransactionReferenceID not found for transaction {transaction_reference}. "
                    f"Cannot check status. Transaction may not have been uploaded successfully."
                )
                return {
                    "success": False,
                    "error": _("PlutusTransactionReferenceID not found. Transaction may not be uploaded."),
                    "status": "Unknown"
                }
            
            # Convert to integer (Pine Labs API expects integer)
            try:
                plutus_ref_id = int(plutus_ref_id)
            except (ValueError, TypeError):
                frappe.logger().error(
                    f"Invalid PlutusTransactionReferenceID format: {plutus_ref_id} for transaction {transaction_reference}"
                )
                return {
                    "success": False,
                    "error": _("Invalid PlutusTransactionReferenceID format"),
                    "status": "Unknown"
                }
                
        except Exception as e:
            frappe.logger().error(f"Error getting transaction log: {str(e)}")
            return {
                "success": False,
                "error": _("Transaction log not found: {0}").format(str(e)),
                "status": "Unknown"
            }
        
        # Build request payload - Use PlutusTransactionReferenceID instead of TransactionNumber
        payload = {
            "MerchantID": self.merchant_id,
            "SecurityToken": self.security_token,
            "ClientID": self.client_id,
            "StoreID": self.store_id,
            "PlutusTransactionReferenceID": plutus_ref_id  # Must be integer
        }
        
        frappe.logger().info(
            f"GetStatus API call for transaction {transaction_reference} "
            f"using PlutusTransactionReferenceID: {plutus_ref_id}"
        )

        # Make API request
        endpoint = self.status_endpoint
        frappe.logger().info(
            f"Making GetStatus API request for transaction {transaction_reference} "
            f"to endpoint: {endpoint}"
        )
        response = self._make_request(endpoint, payload)

        # Log API response status
        http_status = response.get("status_code", "Unknown")
        api_success = response.get("success", False)
        frappe.logger().info(
            f"GetStatus API Response received for {transaction_reference}: "
            f"HTTP Status={http_status}, API Success={api_success}, "
            f"Has Data={bool(response.get('data'))}"
        )

        if response.get("success"):
            response_data = response.get("data", {})
            
            # Log the full API response for debugging
            frappe.logger().info(
                f"GetStatus API Response Data for {transaction_reference}: "
                f"{json.dumps(response_data, indent=2)}"
            )
            
            # Verify we actually got response data
            if not response_data:
                frappe.logger().warning(
                    f"GetStatus API returned success but no data for {transaction_reference}"
                )

            # Update transaction log with response
            # update_transaction_from_status returns the updated log to avoid duplicate reads
            updated_log = self.update_transaction_from_status(transaction_reference, response_data)
            log_status = updated_log.status if updated_log else None

            # Parse status from API response
            status_code = response_data.get("ResponseCode")
            
            # Log the status code and log status for debugging
            frappe.logger().info(
                f"Status check for {transaction_reference}: "
                f"ResponseCode={status_code}, LogStatus={log_status}, "
                f"RRN={response_data.get('RRN')}, ApprovalCode={response_data.get('ApprovalCode')}"
            )

            # ResponseCode mapping for GetStatus API:
            # 0 = Approved (payment completed)
            # 1001 = TXN UPLOADED (transaction uploaded, still pending payment)
            # 1, 2 = Declined
            # Other = Pending/Failed
            # Check for both integer and string values (API may return either)
            if status_code == 0 or status_code == "0":
                api_status = "APPROVED"  # Use uppercase to match frontend expectation
            elif status_code == 1001 or status_code == "1001":
                # ResponseCode 1001 means "TXN UPLOADED" - transaction is still pending
                api_status = "Pending"
                frappe.logger().info(
                    f"Transaction {transaction_reference} still pending "
                    f"(ResponseCode 1001: TXN UPLOADED - waiting for payment completion)"
                )
            elif status_code in [1, 2, "1", "2"]:
                api_status = "Declined"
            else:
                api_status = "Pending"

            # Priority: Trust API ResponseCode 0 (Approved) immediately
            # Only use log status if API is unclear
            if status_code == 0 or status_code == "0":
                # API says Approved - trust it immediately (payment is completed)
                status = "APPROVED"  # Use uppercase to match frontend expectation
                frappe.logger().info(
                    f"Transaction {transaction_reference} status set to APPROVED "
                    f"(API returned ResponseCode 0 - payment completed)"
                )
            elif status_code in [1, 2, "1", "2"]:
                # API says Declined - check if log confirms it
                if log_status == "Declined":
                    status = "Declined"
                else:
                    # Might be terminal initialization, keep as Pending
                    status = "Pending"
            else:
                # API unclear - use log status
                status = log_status or api_status
                
            # Log final status being returned
            frappe.logger().info(
                f"Final status returned for {transaction_reference}: {status}"
            )

            return {
                "success": True,
                "status": status,
                "response_code": status_code,
                "response_message": response_data.get("ResponseMessage", ""),
                "rrn": response_data.get("RRN", ""),
                "approval_code": response_data.get("ApprovalCode", ""),
                "transaction_data": response_data
            }
        else:
            # API request failed - log detailed error information
            error_msg = response.get("error", _("Failed to get transaction status"))
            http_status = response.get("status_code", "Unknown")
            raw_response = response.get("raw_response", "")
            
            frappe.logger().error(
                f"GetStatus API FAILED for transaction {transaction_reference}: "
                f"HTTP Status={http_status}, Error={error_msg}, "
                f"Raw Response={raw_response[:500] if raw_response else 'No response'}"
            )
            
            return {
                "success": False,
                "status": "Unknown",
                "error": error_msg,
                "http_status": http_status,
                "api_response_received": bool(raw_response)
            }

    def cancel_transaction(self, transaction_reference):
        """
        Cancel a pending transaction

        Args:
            transaction_reference: Unique transaction reference

        Returns:
            Dictionary with cancellation result
        """
        # Validate input
        if not transaction_reference:
            return {
                "success": False,
                "error": _("Transaction reference is required")
            }
        
        # Verify transaction log exists
        if not frappe.db.exists("Pinelabs Transaction Log", {
            "transaction_reference": transaction_reference
        }):
            return {
                "success": False,
                "error": _("Transaction {0} not found").format(transaction_reference)
            }
        
        # Get transaction log to retrieve PlutusTransactionReferenceID
        # (Pine Labs cancel API needs PlutusTransactionReferenceID, not TransactionNumber)
        try:
            log = frappe.get_doc("Pinelabs Transaction Log", {
                "transaction_reference": transaction_reference
            })
            
            # Don't cancel if already completed
            if log.status in ["Approved", "Declined", "Cancelled"]:
                return {
                    "success": False,
                    "error": _("Cannot cancel transaction with status: {0}").format(log.status)
                }
            
            # Get PlutusTransactionReferenceID (external reference from Pine Labs)
            plutus_ref_id = log.external_reference or log.get("external_reference")
            
            if not plutus_ref_id:
                frappe.logger().error(
                    f"PlutusTransactionReferenceID not found for transaction {transaction_reference}. "
                    f"Cannot cancel. Transaction may not have been uploaded successfully."
                )
                return {
                    "success": False,
                    "error": _("PlutusTransactionReferenceID not found. Transaction may not be uploaded.")
                }
            
            # Convert to integer (Pine Labs API expects integer)
            try:
                plutus_ref_id = int(plutus_ref_id)
            except (ValueError, TypeError):
                frappe.logger().error(
                    f"Invalid PlutusTransactionReferenceID format: {plutus_ref_id} for transaction {transaction_reference}"
                )
                return {
                    "success": False,
                    "error": _("Invalid PlutusTransactionReferenceID format")
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": _("Error checking transaction status: {0}").format(str(e))
            }
        
        # Build request payload - Use PlutusTransactionReferenceID instead of TransactionNumber
        payload = {
            "MerchantID": self.merchant_id,
            "SecurityToken": self.security_token,
            "PlutusTransactionReferenceID": plutus_ref_id,  # Use external reference, not TransactionNumber
            "UserID": frappe.session.user
        }
        
        frappe.logger().info(
            f"Cancel Transaction API call for {transaction_reference} "
            f"using PlutusTransactionReferenceID: {plutus_ref_id}"
        )

        # Make API request
        endpoint = self.cancel_endpoint
        response = self._make_request(endpoint, payload)

        # Check HTTP status first
        if response.get("success") and response.get("status_code") == 200:
            # Check Pine Labs ResponseCode in the response data
            response_data = response.get("data", {})
            response_code = response_data.get("ResponseCode")
            response_message = response_data.get("ResponseMessage", "")
            
            # Log the response for debugging
            frappe.logger().info(
                f"Cancel Transaction API Response for {transaction_reference}: "
                f"ResponseCode={response_code}, ResponseMessage={response_message}, "
                f"Full Response={json.dumps(response_data, indent=2)}"
            )
            
            # For cancel, if HTTP 200 is returned, consider it successful
            # Update transaction log to Cancelled regardless of ResponseCode
            # (Pine Labs cancel API might return different ResponseCodes)
            try:
                log_name = frappe.db.get_value(
                    "Pinelabs Transaction Log",
                    {"transaction_reference": transaction_reference},
                    "name"
                )
                
                if log_name:
                    frappe.db.set_value(
                        "Pinelabs Transaction Log",
                        log_name,
                        {
                            "status": "Cancelled",
                            "updated_at": now_datetime(),
                            "response_payload": json.dumps(response_data)
                        }
                    )
                    frappe.db.commit()
                    frappe.logger().info(
                        f"Transaction log updated to Cancelled for {transaction_reference}"
                    )
            except Exception as e:
                frappe.logger().error(f"Error updating transaction log: {str(e)}")
            
            # Check ResponseCode for detailed success/failure message
            if response_code == 0 or response_code is None:
                # ResponseCode 0 or None typically means success
                return {
                    "success": True,
                    "message": _("Transaction cancelled successfully")
                }
            else:
                # ResponseCode indicates some issue, but log is updated
                # Still return success since HTTP 200 means cancel was processed
                frappe.logger().warning(
                    f"Cancel API returned ResponseCode {response_code} for {transaction_reference}, "
                    f"but HTTP 200 indicates request was processed. Marking as cancelled."
                )
                return {
                    "success": True,
                    "message": _("Transaction cancellation processed"),
                    "response_code": response_code,
                    "response_message": response_message
                }
        else:
            # HTTP request failed
            error_msg = response.get("error", _("Failed to cancel transaction"))
            frappe.logger().error(
                f"Cancel API HTTP request failed for {transaction_reference}: {error_msg}"
            )
            return {
                "success": False,
                "error": error_msg
            }

    def _create_transaction_log(
        self,
        transaction_reference,
        invoice,
        amount,
        payment_mode,
        request_payload,
        response_payload,
        status,
        machine_name=None,
        source_system=None,
        source_doctype=None,
        source_docname=None,
        external_order_id=None,
    ):
        """
        Create transaction log entry

        Args:
            transaction_reference: Unique transaction ID
            invoice: POS Invoice name
            amount: Transaction amount
            payment_mode: Card or UPI
            request_payload: API request data
            response_payload: API response data
            status: Transaction status
            machine_name: Optional machine name
            source_system: Host system (example: ERPNext)
            source_doctype: Source document type in host system
            source_docname: Source document name in host system
            external_order_id: Generic order identifier for cross-app use

        Returns:
            Transaction Log document
        """
        log = frappe.get_doc({
            "doctype": "Pinelabs Transaction Log",
            "transaction_reference": transaction_reference,
            "invoice": invoice,
            "amount": amount,
            "payment_mode": payment_mode,
            "status": status,
            "machine_name": machine_name,
            "merchant_id": self.merchant_id,
            "store_id": self.store_id,
            "source_system": source_system,
            "source_doctype": source_doctype,
            "source_docname": source_docname,
            "external_order_id": external_order_id or invoice,
            "request_payload": json.dumps(request_payload, indent=2),
            "response_payload": json.dumps(response_payload, indent=2),
            "created_at": now_datetime(),
            "updated_at": now_datetime()
        })

        log.insert(ignore_permissions=True)
        frappe.db.commit()

        return log

    def update_transaction_from_status(self, transaction_reference, status_data):
        """
        Update transaction log with status response data

        Args:
            transaction_reference: Unique transaction reference
            status_data: Status response from Pine Labs
            
        Returns:
            Updated transaction log document (or None if error)
        """
        try:
            log = frappe.get_doc("Pinelabs Transaction Log", {
                "transaction_reference": transaction_reference
            })
            updates = {}

            # Update status based on ResponseCode
            # ResponseCode 0 from GetStatus API means Approved - trust it immediately
            # ResponseCode 1001 means "TXN UPLOADED" - keep as Pending
            # For declined cases, only mark if we have completion data (definitive decline)
            response_code = status_data.get("ResponseCode")
            
            # Check for completion indicators (RRN or ApprovalCode) - these indicate payment completed
            has_rrn = bool(status_data.get("RRN"))
            has_approval_code = bool(status_data.get("ApprovalCode"))
            has_completion_data = has_rrn or has_approval_code
            
            # Log the status data for debugging
            frappe.logger().info(
                f"Updating transaction log {transaction_reference}: "
                f"ResponseCode={response_code} (type={type(response_code).__name__}), "
                f"RRN={status_data.get('RRN')}, ApprovalCode={status_data.get('ApprovalCode')}, "
                f"has_completion_data={has_completion_data}, current_status={log.status}"
            )
            
            # Check for both integer 0 and string "0" (API may return either)
            if response_code == 0 or response_code == "0":
                # ResponseCode 0 from GetStatus API means Approved - trust it
                # This is called from get_transaction_status() which means payment is completed
                updates["status"] = "Approved"
                updates["completed_at"] = now_datetime()
            elif response_code == 1001 or response_code == "1001":
                # ResponseCode 1001 means "TXN UPLOADED" - transaction uploaded but payment not completed yet
                # However, if we have RRN/ApprovalCode, payment is actually completed
                if has_completion_data:
                    # Payment completed despite ResponseCode 1001 (may happen with UPI)
                    updates["status"] = "Approved"
                    updates["completed_at"] = now_datetime()
                    frappe.logger().info(
                        f"Transaction {transaction_reference} marked as Approved "
                        f"(ResponseCode 1001 but has RRN/ApprovalCode - payment completed)"
                    )
                else:
                    # Keep status as Pending
                    updates["status"] = "Pending"
                    frappe.logger().info(
                        f"Transaction {transaction_reference} still pending "
                        f"(ResponseCode 1001: TXN UPLOADED - waiting for payment completion)"
                    )
            elif response_code in [1, 2, "1", "2"]:
                # For declined, only mark if we have completion data (definitive decline)
                # This prevents marking as declined while terminal is initializing
                if has_completion_data:
                    updates["status"] = "Declined"
                    updates["completed_at"] = now_datetime()
                    updates["error_code"] = str(response_code)
                    updates["error_message"] = status_data.get("ResponseMessage", _("Transaction declined"))
                # If no completion data, keep as Pending (might be terminal initialization)
            else:
                # For other response codes or None, check if we have completion data
                # If RRN/ApprovalCode present, payment is likely approved (common with UPI)
                if has_completion_data:
                    updates["status"] = "Approved"
                    updates["completed_at"] = now_datetime()
                    frappe.logger().info(
                        f"Transaction {transaction_reference} marked as Approved "
                        f"(ResponseCode={response_code} but has RRN/ApprovalCode - payment completed)"
                    )
                # Otherwise keep status as Pending

            # Update transaction details (always update these, but preserve existing if new is empty)
            updates["rrn"] = status_data.get("RRN", "") or log.rrn
            updates["approval_code"] = status_data.get("ApprovalCode", "") or log.approval_code
            updates["external_reference"] = status_data.get("PlutusTransactionReferenceID", "") or log.external_reference

            # Update card details if available
            if log.payment_mode == "Card":
                updates["card_number_masked"] = status_data.get("CardNumber", "") or log.card_number_masked
                updates["card_type"] = status_data.get("CardType", "") or log.card_type
                updates["card_holder_name"] = status_data.get("CardHolderName", "") or log.card_holder_name
                updates["bank_name"] = status_data.get("BankName", "") or log.bank_name

            # Update response payload
            updates["response_payload"] = json.dumps(status_data, indent=2)
            updates["updated_at"] = now_datetime()

            frappe.db.set_value("Pinelabs Transaction Log", log.name, updates, update_modified=True)
            frappe.db.commit()

            # Return the updated log to avoid stale in-memory timestamps under webhook/poll races.
            return frappe.get_doc("Pinelabs Transaction Log", log.name)

        except Exception as e:
            frappe.logger().error(f"Error updating transaction log: {str(e)}")
            frappe.log_error(frappe.get_traceback(), "Pine Labs Transaction Update")
            return None


# Utility function to get client instance
def get_pinelabs_client(machine_name=None, warehouse=None):
    """
    Get PineLabsClient instance
    
    Args:
        machine_name: Optional machine name (for multiple machine mode)
        warehouse: Optional warehouse name (will resolve to machine)
        
    Returns:
        PineLabsClient instance
    """
    return PineLabsClient(machine_name=machine_name, warehouse=warehouse)
