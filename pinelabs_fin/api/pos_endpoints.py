# -*- coding: utf-8 -*-
# Copyright (c) 2024, Your Company and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
import json
from frappe import _
from frappe.utils import cint, flt
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
def get_pinelabs_payment_modes():
    """Return enabled PineLabs Mode of Payment rows from settings."""
    from pinelabs_fin.api import services

    if not services.is_pinelabs_enabled():
        frappe.throw(_("Pine Labs integration is not enabled."))

    payment_modes = services.get_all_enabled_pinelabs_modes()
    if not payment_modes:
        frappe.throw(
            _("No enabled payment modes configured. Please set up payment modes in Pinelabs Settings.")
        )

    return payment_modes


@frappe.whitelist()
def get_payable_doctypes():
    """
    Return enabled rows from the Pine Labs Settings payable_doctypes child table.
    Called by the frontend form button injector on page load.
    """
    from pinelabs_fin.api import services

    settings = frappe.get_single("Pinelabs Settings")
    return [
        services.serialize_payable_doctype_config(row)
        for row in settings.get("payable_doctypes", [])
        if cint(row.is_enabled) == 1 and row.doctype_name
    ]


@frappe.whitelist()
def get_trigger_context(doctype, docname=None, mode_of_payment=None, context=None):
    """Return the PineLabs context for a source document."""
    from pinelabs_fin.api import services
    return services.evaluate_pinelabs_context(
        doctype=doctype,
        docname=docname,
        mode_of_payment=mode_of_payment,
        context=context,
    )


@frappe.whitelist()
def initiate_payment(
    invoice_name,
    amount,
    payment_mode="Card",
    allowed_payment_mode=None,
    mode_of_payment=None,
    source_system=None,
    source_doctype=None,
    source_docname=None,
    machine_name=None,
    context=None,
):
    """Validate the request and start PineLabs terminal payment."""
    from pinelabs_fin.api import services

    try:
        if not invoice_name:
            return {"success": False, "error": _("Source document name is required")}

        # Resolve source doctype if not provided
        resolved_doctype = source_doctype
        if not resolved_doctype or not frappe.db.exists(resolved_doctype, invoice_name):
            if frappe.db.exists("Payment Entry", invoice_name):
                resolved_doctype = "Payment Entry"
            elif frappe.db.exists("POS Invoice", invoice_name):
                resolved_doctype = "POS Invoice"
            elif frappe.db.exists("Sales Invoice", invoice_name):
                resolved_doctype = "Sales Invoice"

        if not resolved_doctype:
            return {
                "success": False,
                "error": _("Source document {0} not found").format(invoice_name),
            }

        # If mode_of_payment not provided, try to derive from payment_mode label
        # (legacy compatibility)
        if not mode_of_payment:
            for pm in services.get_all_enabled_pinelabs_modes():
                if pm["label"] == payment_mode:
                    mode_of_payment = pm["mode_of_payment"]
                    break

        return services.handle_mode_of_payment_trigger(
            doctype=resolved_doctype,
            docname=invoice_name,
            mode_of_payment=mode_of_payment,
            amount=flt(amount),
            machine_name=machine_name,
            context=context,
        )

    except Exception as e:
        frappe.logger().error(f"Error in initiate_payment: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Pine Labs Payment Initiation Error")
        return {"success": False, "error": str(e)}


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
        "payment_mode_label": getattr(log, "payment_mode_label", None) or log.payment_mode,
        "mode_of_payment": getattr(log, "mode_of_payment", None),
        "allowed_payment_mode": getattr(log, "allowed_payment_mode", None),
        "source_system": log.source_system,
        "source_doctype": log.source_doctype,
        "source_docname": log.source_docname,
        "external_order_id": log.external_order_id or log.invoice,
        "rrn": getattr(log, "rrn", None),
        "approval_code": getattr(log, "approval_code", None),
        "payment_entry": getattr(log, "payment_entry", None),
    }


@frappe.whitelist()
def check_payment_status(transaction_reference):
    """
    Check Pine Labs payment transaction status

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
            1
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
def get_available_machines():
    """Return list of enabled PineLabs machines for frontend machine selector."""
    settings = frappe.get_single("Pinelabs Settings")
    if not settings.enabled:
        return []
    return [
        {
            "machine_name": m.machine_name,
            "warehouse": m.warehouse,
            "description": m.description,
        }
        for m in (settings.machines or [])
        if m.enabled
    ]
