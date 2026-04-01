# -*- coding: utf-8 -*-
# Copyright (c) 2024, Your Company and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
import json
from frappe import _
from frappe.utils import flt
from pinelabs_fin.api.host_adapter import get_host_adapter
from pinelabs_fin.api.pinelabs_client import get_pinelabs_client


def _get_warehouse_from_invoice(invoice):
    """
    Extract warehouse from invoice (POS Invoice or Sales Invoice)
    
    Args:
        invoice: Invoice document
        
    Returns:
        Warehouse name or None
    """
    # Try to get warehouse from POS Profile first
    if hasattr(invoice, 'pos_profile') and invoice.pos_profile:
        pos_profile = frappe.get_doc("POS Profile", invoice.pos_profile)
        if pos_profile.warehouse:
            return pos_profile.warehouse
    
    # Try set_warehouse field
    if hasattr(invoice, 'set_warehouse') and invoice.set_warehouse:
        return invoice.set_warehouse
    
    # Try to get from first item
    if hasattr(invoice, 'items') and invoice.items:
        for item in invoice.items:
            if hasattr(item, 'warehouse') and item.warehouse:
                return item.warehouse
    
    return None


def _get_machine_from_transaction_log(transaction_reference):
    """
    Get machine name from transaction log
    
    Args:
        transaction_reference: Transaction reference
        
    Returns:
        Machine name or None
    """
    try:
        if frappe.db.exists("Pinelabs Transaction Log", {
            "transaction_reference": transaction_reference
        }):
            log = frappe.get_doc("Pinelabs Transaction Log", {
                "transaction_reference": transaction_reference
            })
            return log.machine_name
    except Exception:
        pass
    
    return None


def _get_active_endpoint_name(settings):
    """
    Get active endpoint name from settings
    
    Args:
        settings: Pinelabs Settings document
        
    Returns:
        Active endpoint name or None
    """
    if settings.api_endpoints:
        for endpoint in settings.api_endpoints:
            if endpoint.is_active:
                return (
                    getattr(endpoint, "endpoints_name", None)
                    or getattr(endpoint, "endpoint_name", None)
                )
    return None


@frappe.whitelist()
def initiate_payment(
    invoice_name,
    amount,
    payment_mode="Card",
    source_system=None,
    source_doctype=None,
    source_docname=None,
):
    """
    Initiate Pine Labs payment transaction
    Called from POSawesome frontend

    Args:
        invoice_name: POS Invoice name
        amount: Transaction amount
        payment_mode: "Card" or "UPI"

    Returns:
        JSON response with transaction details
    """
    try:
        # POS-specific validations
        if not invoice_name:
            return {
                "success": False,
                "error": _("Invoice name is required")
            }

        # Verify invoice exists (check both POS Invoice and Sales Invoice)
        invoice_doctype = source_doctype if source_doctype and frappe.db.exists(source_doctype, invoice_name) else None
        if not invoice_doctype and frappe.db.exists("POS Invoice", invoice_name):
            invoice_doctype = "POS Invoice"
        if not invoice_doctype and frappe.db.exists("Sales Invoice", invoice_name):
            invoice_doctype = "Sales Invoice"
        if not invoice_doctype:
            return {
                "success": False,
                "error": _("Invoice {0} not found").format(invoice_name)
            }

        # Get invoice to verify amount and extract warehouse
        invoice = frappe.get_doc(invoice_doctype, invoice_name)
        amount = flt(amount)
        if abs(invoice.grand_total - amount) > 0.01:  # Allow 1 paisa difference for rounding
            frappe.logger().warning(
                f"Amount mismatch for {invoice_name}: "
                f"Requested {amount}, Invoice total {invoice.grand_total}"
            )

        # Get warehouse from invoice (for machine selection)
        warehouse = _get_warehouse_from_invoice(invoice)

        # Initialize Pine Labs client with warehouse (handles generic validations)
        client = get_pinelabs_client(warehouse=warehouse)

        # Initiate payment (client handles amount and payment_mode validation)
        result = client.upload_billed_transaction(
            invoice_name=invoice_name,
            amount=amount,
            payment_mode=payment_mode,
            source_system=source_system or "fin_pos",
            source_doctype=source_doctype or invoice_doctype,
            source_docname=source_docname or invoice_name,
            external_order_id=source_docname or invoice_name,
        )

        if result.get("success"):
            frappe.logger().info(
                f"Payment initiated successfully: {result.get('transaction_reference')} "
                f"for invoice {invoice_name}, Response: {result.get('response_message', 'N/A')}"
            )

            return {
                "success": True,
                "transaction_reference": result.get("transaction_reference"),
                "transaction_log": result.get("transaction_log"),
                "message": _("Payment initiated. Please complete payment on terminal."),
                "amount": amount,
                "payment_mode": payment_mode,
                "invoice_name": invoice_name,
                "invoice_doctype": source_doctype or invoice_doctype,
                "response_code": result.get("response_code"),
                "response_message": result.get("response_message")
            }
        else:
            error_msg = result.get("error", _("Failed to initiate payment"))
            response_code = result.get("response_code")
            
            frappe.logger().error(
                f"Payment initiation failed for {invoice_name}: {error_msg} "
                f"(Response Code: {response_code})"
            )

            return {
                "success": False,
                "error": error_msg,
                "transaction_reference": result.get("transaction_reference"),
                "transaction_log": result.get("transaction_log"),
                "response_code": response_code
            }

    except Exception as e:
        frappe.logger().error(f"Error in initiate_payment: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Pine Labs Payment Initiation Error")

        return {
            "success": False,
            "error": str(e)
        }


def _get_transaction_log(transaction_reference):
    if not transaction_reference:
        return None
    if not frappe.db.exists("Pinelabs Transaction Log", {"transaction_reference": transaction_reference}):
        return None
    return frappe.get_doc("Pinelabs Transaction Log", {"transaction_reference": transaction_reference})


def _normalize_status(status):
    value = (status or "").strip().lower()
    if value == "approved":
        return "Approved"
    if value == "declined":
        return "Declined"
    if value in ("cancel", "cancelled"):
        return "Cancelled"
    if value == "failed":
        return "Failed"
    if value == "timeout":
        return "Timeout"
    if value == "pending":
        return "Pending"
    return status or "Unknown"


def _build_status_response(
    log,
    *,
    success=True,
    message=None,
    error=None,
    response_code=None,
    response_message=None,
    finalization=None,
):
    adapter = get_host_adapter()
    source_doctype, source_docname = adapter.get_order_reference(log)
    order_doc = adapter.get_order_doc(source_docname, doctype=source_doctype) if source_docname else None

    invoice_name = (
        (finalization or {}).get("invoice_name")
        or (order_doc.name if order_doc else None)
        or source_docname
        or log.external_order_id
        or log.invoice
    )
    invoice_doctype = (
        (finalization or {}).get("invoice_doctype")
        or (order_doc.doctype if order_doc else None)
        or source_doctype
    )
    invoice_finalized = bool(
        (finalization or {}).get("invoice_finalized")
        if finalization and finalization.get("invoice_finalized") is not None
        else (adapter.is_order_finalized(order_doc) if order_doc else False)
    )
    finalization_status = (
        (finalization or {}).get("finalization_status")
        or ("already_finalized" if invoice_finalized else "pending")
    )

    return {
        "success": bool(success),
        "status": _normalize_status(log.status),
        "transaction_reference": log.transaction_reference,
        "invoice_name": invoice_name,
        "invoice_doctype": invoice_doctype,
        "invoice_finalized": invoice_finalized,
        "finalization_status": finalization_status,
        "message": message or (finalization or {}).get("message") or response_message,
        "error": error or (finalization or {}).get("error"),
        "response_code": response_code,
        "response_message": response_message,
        "amount": log.amount,
        "payment_mode": log.payment_mode,
        "source_system": log.source_system,
        "source_doctype": log.source_doctype,
        "source_docname": log.source_docname,
        "external_order_id": log.external_order_id or log.invoice,
        "rrn": getattr(log, "rrn", None),
        "approval_code": getattr(log, "approval_code", None),
    }


@frappe.whitelist()
def check_payment_status(transaction_reference):
    """
    Check Pine Labs payment transaction status
    Called from POSawesome frontend

    Args:
        transaction_reference: Unique transaction reference

    Returns:
        JSON response with status details
    """
    try:
        log = _get_transaction_log(transaction_reference)
        if not log:
            return {
                "success": False,
                "status": "Unknown",
                "transaction_reference": transaction_reference,
                "invoice_name": None,
                "invoice_doctype": None,
                "invoice_finalized": False,
                "finalization_status": "missing",
                "message": None,
                "error": _("Transaction {0} not found").format(transaction_reference),
            }

        result = None
        if _normalize_status(log.status) == "Pending":
            machine_name = _get_machine_from_transaction_log(transaction_reference)
            client = get_pinelabs_client(machine_name=machine_name)
            result = client.get_transaction_status(transaction_reference)
            refreshed_log = _get_transaction_log(transaction_reference)
            if refreshed_log:
                log = refreshed_log

            if result and not result.get("success") and _normalize_status(log.status) == "Pending":
                return _build_status_response(
                    log,
                    success=True,
                    message=result.get("error") or _("Waiting for payment confirmation."),
                    response_code=result.get("response_code"),
                    response_message=result.get("response_message"),
                )

        finalization = None
        status = _normalize_status(log.status)
        if status == "Approved":
            finalization = get_host_adapter().finalize_transaction(log)
            refreshed_log = _get_transaction_log(transaction_reference)
            if refreshed_log:
                log = refreshed_log
            return _build_status_response(
                log,
                success=bool(finalization.get("success", False)),
                message=(finalization or {}).get("message") or _("Payment approved."),
                error=(finalization or {}).get("error"),
                response_code=(result or {}).get("response_code"),
                response_message=(result or {}).get("response_message"),
                finalization=finalization,
            )

        return _build_status_response(
            log,
            success=True,
            message=(result or {}).get("response_message"),
            response_code=(result or {}).get("response_code"),
            response_message=(result or {}).get("response_message"),
        )

    except Exception as e:
        frappe.logger().error(f"Error in check_payment_status: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Pine Labs Status Check Error")

        return {
            "success": False,
            "error": str(e),
            "status": "Unknown"
        }


@frappe.whitelist()
def cancel_payment(transaction_reference):
    """
    Cancel Pine Labs payment transaction
    Called from POSawesome frontend

    Args:
        transaction_reference: Unique transaction reference

    Returns:
        JSON response with cancellation result
    """
    try:
        log = _get_transaction_log(transaction_reference)
        if not log:
            return {
                "success": False,
                "status": "Unknown",
                "transaction_reference": transaction_reference,
                "invoice_name": None,
                "invoice_doctype": None,
                "invoice_finalized": False,
                "finalization_status": "missing",
                "error": _("Transaction {0} not found").format(transaction_reference),
            }

        current_status = _normalize_status(log.status)
        if current_status in ["Approved", "Declined", "Cancelled", "Failed", "Timeout"]:
            return _build_status_response(
                log,
                success=False,
                error=_("Cannot cancel transaction with status: {0}").format(current_status),
            )

        # Get machine from transaction log
        machine_name = _get_machine_from_transaction_log(transaction_reference)
        
        # Initialize Pine Labs client with machine (handles validation, log check, and status check)
        client = get_pinelabs_client(machine_name=machine_name)

        # Cancel transaction (client validates and checks if cancellation is allowed)
        result = client.cancel_transaction(transaction_reference)

        if result.get("success"):
            frappe.logger().info(
                f"Transaction cancelled successfully: {transaction_reference}"
            )

            refreshed_log = _get_transaction_log(transaction_reference) or log
            return _build_status_response(
                refreshed_log,
                success=True,
                message=result.get("message") or _("Transaction cancelled successfully"),
                response_code=result.get("response_code"),
                response_message=result.get("response_message"),
            )
        else:
            frappe.logger().error(
                f"Cancellation failed for {transaction_reference}: {result.get('error')}"
            )

            refreshed_log = _get_transaction_log(transaction_reference) or log
            return _build_status_response(
                refreshed_log,
                success=False,
                error=result.get("error", _("Failed to cancel transaction")),
                response_code=result.get("response_code"),
                response_message=result.get("response_message"),
            )

    except Exception as e:
        frappe.logger().error(f"Error in cancel_payment: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Pine Labs Cancellation Error")

        return {
            "success": False,
            "error": str(e)
        }


@frappe.whitelist()
def get_payment_settings():
    """
    Get Pine Labs settings for frontend validation
    Called from POSawesome frontend

    Returns:
        JSON response with settings
    """
    try:
        settings = frappe.get_single("Pinelabs Settings")

        # Get active endpoint name
        active_endpoint_name = _get_active_endpoint_name(settings)

        # Get first enabled machine for settings (if any)
        machine_info = {}
        if settings.machines:
            for machine in settings.machines:
                if machine.enabled:
                    machine_info = {
                        "client_id": machine.client_id or "",
                        "store_id": machine.store_id or "",
                        "device_number": machine.device_number or ""
                    }
                    break

        return {
            "success": True,
            "enabled": settings.enabled,
            "active_endpoint": active_endpoint_name,
            "client_id": machine_info.get("client_id", ""),
            "store_id": machine_info.get("store_id", ""),
            "device_number": machine_info.get("device_number", "")
        }

    except Exception as e:
        frappe.logger().error(f"Error in get_payment_settings: {str(e)}")

        return {
            "success": False,
            "error": str(e),
            "enabled": False
        }


@frappe.whitelist()
def test_credentials():
    """
    Test Pine Labs credentials and API connection
    This makes a minimal API call to verify credentials are correct
    
    Returns:
        JSON response with test results
    """
    try:
        # For test, use default machine (no warehouse specified)
        # Initialize Pine Labs client (this validates settings exist)
        client = get_pinelabs_client()
        
        # Test 1: Verify all credentials are configured
        validation_results = {
            "merchant_id": bool(client.merchant_id),
            "security_token": bool(client.security_token),
            "client_id": bool(client.client_id),
            "store_id": bool(client.store_id),
            "device_number": bool(client.device_number),
            "api_url": bool(client.base_url)
        }
        
        if not all(validation_results.values()):
            missing = [k for k, v in validation_results.items() if not v]
            return {
                "success": False,
                "error": _("Missing credentials: {0}").format(", ".join(missing)),
                "validation_results": validation_results
            }
        
        # Test 2: Make a test API call with minimal amount
        # Use a test transaction with 0.01 amount to verify connection
        test_transaction_ref = client._generate_transaction_reference()
        test_payload = client._build_transaction_payload(
            test_transaction_ref, 
            0.01,  # Minimal test amount
            "Card"
        )
        
        # Make the API call
        endpoint = client.upload_endpoint
        response = client._make_request(endpoint, test_payload)
        
        # Parse response
        response_data = response.get("data", {})
        response_code = response_data.get("ResponseCode")
        response_message = response_data.get("ResponseMessage", "")
        
        # Log the test
        frappe.logger().info(
            f"Pine Labs Credential Test: "
            f"ResponseCode={response_code}, "
            f"ResponseMessage={response_message}, "
            f"HTTP Status={response.get('status_code')}"
        )
        
        # Get active endpoint name (used in all response paths)
        active_endpoint_name = _get_active_endpoint_name(client.settings)
        
        # Interpret results
        if response.get("status_code") == 200:
            if response_code == 0 or response_code is None:
                return {
                    "success": True,
                    "message": _("Credentials are valid! API connection successful."),
                    "response_code": response_code,
                    "response_message": response_message,
                    "api_url": client.base_url,
                    "active_endpoint": active_endpoint_name,
                    "validation_results": validation_results
                }
            else:
                # API responded but with error code
                return {
                    "success": False,
                    "error": _("API connection successful but transaction was declined. "
                              "Response Code: {0}, Message: {1}").format(
                        response_code, response_message
                    ),
                    "response_code": response_code,
                    "response_message": response_message,
                    "api_url": client.base_url,
                    "active_endpoint": active_endpoint_name,
                    "validation_results": validation_results,
                    "note": _("This might indicate: invalid Store ID, terminal not connected, "
                            "or credentials mismatch")
                }
        else:
            # HTTP error
            return {
                "success": False,
                "error": _("API connection failed. HTTP Status: {0}").format(
                    response.get("status_code")
                ),
                "http_status": response.get("status_code"),
                "api_url": client.base_url,
                "active_endpoint": active_endpoint_name,
                "validation_results": validation_results,
                "note": _("Check: API URL, network connectivity, firewall settings")
            }
            
    except Exception as e:
        frappe.logger().error(f"Error testing Pine Labs credentials: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Pine Labs Credential Test Error")
        
        return {
            "success": False,
            "error": str(e),
            "note": _("Check: Settings configuration, enabled status, required fields")
        }


@frappe.whitelist()
def is_pinelabs_available():
    """
    Check if Pinelabs integration is available and enabled
    Called from frontend to check if Pinelabs features should be shown
    
    Returns:
        Dictionary with availability status
    """
    try:
        settings = frappe.get_single("Pinelabs Settings")
        return {
            "available": True,
            "enabled": settings.enabled,
            "active_endpoint": _get_active_endpoint_name(settings)
        }
    except Exception:
        # App not installed or settings don't exist
        return {
            "available": False,
            "enabled": False,
            "active_endpoint": None
        }


@frappe.whitelist()
def update_invoice_payment_from_transaction(invoice_name, transaction_reference, mode_of_payment):
    """
    Update POS Invoice payment amount from approved Pinelabs transaction
    Called from frontend after payment approval
    
    Args:
        invoice_name: POS Invoice name
        transaction_reference: Pinelabs transaction reference
        mode_of_payment: Payment method name (e.g., "Credit Card", "UPI")
    
    Returns:
        Updated invoice document
    """
    try:
        # Verify transaction is approved
        status_result = check_payment_status(transaction_reference)
        
        if not status_result.get("success"):
            frappe.throw(_("Payment verification failed"))
        
        status = (status_result.get("status") or "").upper()
        if status not in ["APPROVED", "APPROVED"]:
            frappe.throw(_("Transaction {0} is not approved. Status: {1}").format(
                transaction_reference, status
            ))
        
        # Determine invoice doctype
        invoice_doctype = None
        if frappe.db.exists("POS Invoice", invoice_name):
            invoice_doctype = "POS Invoice"
        elif frappe.db.exists("Sales Invoice", invoice_name):
            invoice_doctype = "Sales Invoice"
        else:
            frappe.throw(_("Invoice {0} not found").format(invoice_name))
        
        # Get invoice
        invoice = frappe.get_doc(invoice_doctype, invoice_name)
        
        # Find payment method and update amount
        payment_amount = flt(status_result.get("amount") or 0)
        
        # Determine payment mode type from mode_of_payment parameter
        mode_of_payment_lower = (mode_of_payment or "").lower()
        is_card = ("card" in mode_of_payment_lower or "credit card" in mode_of_payment_lower)
        is_upi = "upi" in mode_of_payment_lower
        
        # First, clear all payment amounts (including Cash)
        # Then set only the matching Card/UPI payment amount
        payment_found = False
        matched_payment = None
        
        for payment in invoice.payments:
            payment_mode_lower = (payment.mode_of_payment or "").lower()
            
            # Try exact match first
            if payment.mode_of_payment == mode_of_payment:
                matched_payment = payment
                payment_found = True
            # If exact match fails, try flexible matching for Card/UPI
            elif is_card and ("card" in payment_mode_lower or "credit card" in payment_mode_lower):
                matched_payment = payment
                payment_found = True
            elif is_upi and "upi" in payment_mode_lower:
                matched_payment = payment
                payment_found = True
            
            # Clear payment amounts (will be set for matched payment below)
            if payment != matched_payment:
                payment.amount = 0
                if hasattr(payment, 'base_amount') and payment.base_amount is not None:
                    payment.base_amount = 0
        
        if not payment_found:
            available_methods = ", ".join([p.mode_of_payment for p in invoice.payments if p.mode_of_payment])
            frappe.logger().error(
                f"Pinelabs: Payment method '{mode_of_payment}' not found in invoice {invoice_name}. "
                f"Available payments: {[(p.mode_of_payment, p.amount) for p in invoice.payments]}"
            )
            frappe.throw(_("Payment method {0} not found in invoice. Available methods: {1}").format(
                mode_of_payment, 
                available_methods
            ))
        
        # Set amount for matched payment
        matched_payment.amount = payment_amount
        if hasattr(matched_payment, 'base_amount') and matched_payment.base_amount is not None:
            matched_payment.base_amount = payment_amount
        # Store transaction reference in payment row (if field exists)
        if hasattr(matched_payment, 'transaction_reference'):
            matched_payment.transaction_reference = transaction_reference
        
        invoice.flags.ignore_permissions = True
        invoice.save()
        
        return invoice.as_dict()
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Update Invoice Payment Error")
        frappe.throw(_("Failed to update invoice payment: {0}").format(str(e)))


@frappe.whitelist()
def validate_pinelabs_payment_before_submit(invoice_name, payment_methods):
    """
    Validate Pinelabs payment status before invoice submission
    Called from frontend before submitting invoice
    
    Args:
        invoice_name: Invoice name (POS Invoice or Sales Invoice)
        payment_methods: List of payment methods with transaction references
        
    Returns:
        Dictionary with validation result
    """
    try:
        # Determine invoice doctype
        invoice_doctype = None
        if frappe.db.exists("POS Invoice", invoice_name):
            invoice_doctype = "POS Invoice"
        elif frappe.db.exists("Sales Invoice", invoice_name):
            invoice_doctype = "Sales Invoice"
        else:
            return {
                "valid": False,
                "error": _("Invoice {0} not found").format(invoice_name)
            }
        
        # Parse payment methods if string
        if isinstance(payment_methods, str):
            import json
            payment_methods = json.loads(payment_methods)
        
        # Check each payment method
        for payment in payment_methods or []:
            mode_of_payment = payment.get("mode_of_payment", "")
            transaction_reference = payment.get("transaction_reference")
            amount = flt(payment.get("amount") or 0)
            
            # Check if this is a Pinelabs payment (Card/UPI)
            mode_lower = (mode_of_payment or "").lower()
            is_pinelabs_payment = (
                "card" in mode_lower or 
                "credit card" in mode_lower or 
                "upi" in mode_lower
            )
            
            if is_pinelabs_payment and amount > 0:
                # Must have transaction reference
                if not transaction_reference:
                    return {
                        "valid": False,
                        "error": _("Please complete the payment process for {0} before submitting.").format(mode_of_payment),
                        "payment_method": mode_of_payment
                    }
                
                # Check transaction status
                status_result = check_payment_status(transaction_reference)
                
                if not status_result.get("success"):
                    return {
                        "valid": False,
                        "error": _("Failed to verify payment status for {0}").format(mode_of_payment),
                        "payment_method": mode_of_payment
                    }
                
                status = (status_result.get("status") or "").upper()
                
                # Block submit if cancelled
                if status in ["CANCELLED", "CANCEL"]:
                    return {
                        "valid": False,
                        "error": _("Payment initiated was cancelled. Cannot submit invoice. Please use a different payment method."),
                        "payment_method": mode_of_payment,
                        "status": status
                    }
                
                # Block submit if not approved
                if status != "APPROVED":
                    return {
                        "valid": False,
                        "error": _("Payment is not approved yet. Please wait for payment approval."),
                        "payment_method": mode_of_payment,
                        "status": status
                    }
        
        # All validations passed
        return {
            "valid": True,
            "message": _("All payments validated successfully")
        }
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Validate Pinelabs Payment Error")
        return {
            "valid": False,
            "error": _("Error validating payment: {0}").format(str(e))
        }


@frappe.whitelist()
def submit_pos_invoice_from_transaction_log(invoice_name):
    """
    Submit POS Invoice if there's an approved Pine Labs transaction log OR if invoice has Card/UPI payments.
    This ensures POS Invoice is submitted directly (not Sales Invoice) to avoid Payment Entry reference_doctype errors.
    
    Args:
        invoice_name: POS Invoice name
        
    Returns:
        JSON response with submission status
    """
    try:
        # Check if invoice exists (try both POS Invoice and Sales Invoice)
        invoice_doctype = None
        if frappe.db.exists("POS Invoice", invoice_name):
            invoice_doctype = "POS Invoice"
        elif frappe.db.exists("Sales Invoice", invoice_name):
            invoice_doctype = "Sales Invoice"
        else:
            return {
                "success": False,
                "error": _("Invoice {0} not found").format(invoice_name)
            }
        
        # Get the invoice document to check for Card/UPI payments
        invoice_doc = frappe.get_doc(invoice_doctype, invoice_name)
        
        # Check if invoice has Card or UPI payments
        has_card_or_upi_payment = False
        if invoice_doc.get("payments"):
            for payment in invoice_doc.payments:
                if payment.get("amount") and flt(payment.get("amount")) > 0:
                    payment_mode = (payment.get("mode_of_payment") or "").lower()
                    if "card" in payment_mode or "credit card" in payment_mode or "upi" in payment_mode:
                        has_card_or_upi_payment = True
                        break
        
        # First, check for cancelled transaction logs - if found, block submission
        cancelled_logs = frappe.get_all(
            "Pinelabs Transaction Log",
            filters={
                "invoice": invoice_name,
                "status": "Cancelled"
            },
            fields=["name", "status", "amount", "payment_mode", "transaction_reference"],
            limit=1,
            order_by="creation desc"
        )
        
        if cancelled_logs and has_card_or_upi_payment:
            # Transaction was cancelled but invoice still has Card/UPI payment amount
            # Clear the payment amount and return error
            cancelled_log = cancelled_logs[0]
            return {
                "success": False,
                "error": _("Payment initiated was cancelled. Cannot submit invoice. Please change the payment method or try again."),
                "cancelled": True,
                "transaction_reference": cancelled_log.transaction_reference
            }
        
        # Check for approved or pending transaction log for this invoice
        transaction_logs = frappe.get_all(
            "Pinelabs Transaction Log",
            filters={
                "invoice": invoice_name,
                "status": ["in", ["Approved", "Pending"]]
            },
            fields=["name", "status", "amount", "payment_mode"],
            limit=1,
            order_by="creation desc"
        )
        
        # If invoice is Sales Invoice, we cannot submit it directly as it will cause Payment Entry errors
        # Only POS Invoice can be submitted directly without Payment Entry reference_doctype issues
        if invoice_doctype == "Sales Invoice":
            # If it's Sales Invoice, we need to check if it should have been POS Invoice
            # But we can't convert it here, so we return should_use_regular_submit
            # However, this will cause the Payment Entry error
            # The real fix is to ensure invoice is created as POS Invoice from the start
            return {
                "success": False,
                "error": _("Invoice {0} is Sales Invoice. Cannot submit directly with Card/UPI payment.").format(invoice_name),
                "should_use_regular_submit": True
            }
        
        # If no transaction log AND no Card/UPI payment, use regular submit
        if not transaction_logs and not has_card_or_upi_payment:
            return {
                "success": False,
                "error": _("No Pine Labs transaction or Card/UPI payment found for invoice {0}").format(invoice_name),
                "should_use_regular_submit": True
            }
        
        # At this point, we have POS Invoice with Card/UPI payment or transaction log
        # Submit it directly without going through regular submit_invoice flow
        
        # Check if already submitted
        if invoice_doc.docstatus == 1:
            return {
                "success": True,
                "message": _("Invoice {0} is already submitted").format(invoice_name),
                "name": invoice_doc.name,
                "status": invoice_doc.docstatus
            }
        
        # Update payment amounts based on transaction log or Card/UPI payments
        # This ensures the correct payment method (Card/UPI) has the amount, not Cash
        if transaction_logs:
            transaction_log = transaction_logs[0]
            transaction_amount = flt(transaction_log.amount)
            transaction_mode = (transaction_log.payment_mode or "").lower()
            
            # First, check if any Card/UPI payment already has the correct amount
            # If so, just ensure other Card/UPI payments are reset (preserve Cash)
            payment_already_set = False
            for payment in invoice_doc.payments:
                payment_mode_lower = (payment.mode_of_payment or "").lower()
                is_card_or_upi = ("card" in payment_mode_lower or 
                                  "credit card" in payment_mode_lower or 
                                  "upi" in payment_mode_lower)
                
                if is_card_or_upi and flt(payment.amount) == transaction_amount:
                    # Payment already correctly set - just reset other Card/UPI payments
                    payment_already_set = True
                    for other_payment in invoice_doc.payments:
                        if other_payment != payment:
                            other_payment_mode_lower = (other_payment.mode_of_payment or "").lower()
                            # Only reset other Card/UPI payments, keep Cash and other methods
                            if ("card" in other_payment_mode_lower or 
                                "credit card" in other_payment_mode_lower or 
                                "upi" in other_payment_mode_lower):
                                other_payment.amount = 0
                                if hasattr(other_payment, 'base_amount') and other_payment.base_amount is not None:
                                    other_payment.base_amount = 0
                    break
            
            # If payment not already set, find and set the matching payment method
            if not payment_already_set:
                payment_found = False
                for payment in invoice_doc.payments:
                    payment_mode_lower = (payment.mode_of_payment or "").lower()
                    
                    # Check if this payment matches the transaction log payment mode
                    is_card_match = (transaction_mode == "card" and 
                                    ("card" in payment_mode_lower or "credit card" in payment_mode_lower))
                    is_upi_match = (transaction_mode == "upi" and "upi" in payment_mode_lower)
                    
                    if is_card_match or is_upi_match:
                        # Set amount for matching payment
                        payment.amount = transaction_amount
                        if hasattr(payment, 'base_amount') and payment.base_amount is not None:
                            payment.base_amount = transaction_amount
                        payment_found = True
                        
                        # Reset other Card/UPI payments to 0 (but keep Cash and other non-Card/UPI payments)
                        for other_payment in invoice_doc.payments:
                            if other_payment != payment:
                                other_payment_mode_lower = (other_payment.mode_of_payment or "").lower()
                                # Only reset Card/UPI payments, not Cash or other payment methods
                                if ("card" in other_payment_mode_lower or 
                                    "credit card" in other_payment_mode_lower or 
                                    "upi" in other_payment_mode_lower):
                                    other_payment.amount = 0
                                    if hasattr(other_payment, 'base_amount') and other_payment.base_amount is not None:
                                        other_payment.base_amount = 0
                        break
                
                # If no matching payment found, check if there's already a Card/UPI payment with correct amount
                if not payment_found:
                    # Check if any Card/UPI payment already has the correct amount
                    for payment in invoice_doc.payments:
                        payment_mode_lower = (payment.mode_of_payment or "").lower()
                        is_card_or_upi = ("card" in payment_mode_lower or 
                                          "credit card" in payment_mode_lower or 
                                          "upi" in payment_mode_lower)
                        
                        if is_card_or_upi and flt(payment.amount) == transaction_amount:
                            # Payment already correctly set, just reset other Card/UPI payments
                            payment_found = True
                            for other_payment in invoice_doc.payments:
                                if other_payment != payment:
                                    other_payment_mode_lower = (other_payment.mode_of_payment or "").lower()
                                    if ("card" in other_payment_mode_lower or 
                                        "credit card" in other_payment_mode_lower or 
                                        "upi" in other_payment_mode_lower):
                                        other_payment.amount = 0
                                        if hasattr(other_payment, 'base_amount') and other_payment.base_amount is not None:
                                            other_payment.base_amount = 0
                            break
                    
                    # If still not found, log warning but don't reset existing payments
                    if not payment_found:
                        frappe.logger().warning(
                            f"Pinelabs: No matching payment method found for transaction log. "
                            f"Transaction mode: '{transaction_log.payment_mode}', Amount: {transaction_amount}. "
                            f"Invoice payments: {[(p.mode_of_payment, p.amount) for p in invoice_doc.payments]}. "
                            f"Keeping existing payment amounts."
                        )
        elif has_card_or_upi_payment:
            # If no transaction log but has Card/UPI payment, ensure only that payment has amount
            # Reset only other Card/UPI payments, keep Cash and other non-Card/UPI payments
            for payment in invoice_doc.payments:
                payment_mode_lower = (payment.mode_of_payment or "").lower()
                is_card_or_upi = ("card" in payment_mode_lower or 
                                  "credit card" in payment_mode_lower or 
                                  "upi" in payment_mode_lower)
                
                if not is_card_or_upi:
                    # For non-Card/UPI payments, only reset if they're not Cash
                    is_cash = payment_mode_lower == "cash" or "cash" in payment_mode_lower
                    if not is_cash and flt(payment.amount) > 0:
                        payment.amount = 0
                        if hasattr(payment, 'base_amount') and payment.base_amount is not None:
                            payment.base_amount = 0
                # Keep Card/UPI payment amounts as they are (already set in frontend)
        
        # Import necessary functions from posawesome
        from posawesome.posawesome.api.invoices import (
            _deduplicate_free_items,
            _apply_item_name_overrides,
            _auto_set_return_batches,
            set_batch_nos_for_bundels,
            _validate_stock_on_invoice,
        )
        
        # Apply necessary updates
        _deduplicate_free_items(invoice_doc)
        _apply_item_name_overrides(invoice_doc)
        _auto_set_return_batches(invoice_doc)
        set_batch_nos_for_bundels(invoice_doc, "warehouse", throw=True)
        _validate_stock_on_invoice(invoice_doc)
        
        # Set flags and save
        invoice_doc.flags.ignore_permissions = True
        frappe.flags.ignore_account_permission = True
        invoice_doc.posa_is_printed = 1
        invoice_doc.save()
        
        # Submit the invoice
        invoice_doc.submit()
        
        frappe.logger().info(
            f"POS Invoice {invoice_name} submitted successfully via transaction log"
        )
        
        return {
            "success": True,
            "message": _("POS Invoice {0} submitted successfully").format(invoice_name),
            "name": invoice_doc.name,
            "status": invoice_doc.docstatus,
            "doctype": invoice_doc.doctype
        }
        
    except Exception as e:
        frappe.logger().error(f"Error submitting POS Invoice from transaction log: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Submit POS Invoice from Transaction Log Error")
        
        return {
            "success": False,
            "error": str(e)
        }
