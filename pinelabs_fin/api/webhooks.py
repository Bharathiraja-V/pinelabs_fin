# -*- coding: utf-8 -*-
# Copyright (c) 2024, Your Company and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
import json
from frappe import _
from frappe.utils import now_datetime, flt
from pinelabs_fin.api.host_adapter import get_host_adapter


def _validate_webhook_request():
    """Validate webhook request method and extract data"""
    if frappe.request.method != "POST":
        frappe.throw(_("Only POST requests are allowed"))

    try:
        data = frappe.request.get_json()
    except Exception:
        data = frappe.form_dict

    if not data:
        frappe.throw(_("No data received in webhook"))

    frappe.logger().info(f"Pine Labs Webhook Received: {json.dumps(data, indent=2)}")
    return data


def _extract_webhook_data(data):
    """Extract transaction details from webhook data"""
    transaction_reference = data.get("TransactionNumber") or data.get("transaction_number")
    response_code = data.get("ResponseCode") if "ResponseCode" in data else data.get("response_code")
    response_message = data.get("ResponseMessage") or data.get("response_message")

    if not transaction_reference:
        frappe.logger().error("Webhook missing transaction reference")
        return None, None, None

    if not frappe.db.exists("Pinelabs Transaction Log", {
        "transaction_reference": transaction_reference
    }):
        frappe.logger().error(f"Transaction log not found for: {transaction_reference}")
        return None, None, None

    return transaction_reference, response_code, response_message


def _update_transaction_status(log, response_code, response_message):
    """Update transaction log status based on response code"""
    if response_code == 0 or response_code == "0":
        log.status = "Approved"
        log.completed_at = now_datetime()
    elif response_code in [1, 2, "1", "2"]:
        log.status = "Declined"
        log.completed_at = now_datetime()
        log.error_code = str(response_code)
        log.error_message = response_message or _("Transaction declined")
    else:
        log.status = "Failed"
        log.error_code = str(response_code)
        log.error_message = response_message or _("Transaction failed")


def _update_transaction_details(log, data):
    """Update transaction log with webhook details"""
    log.rrn = data.get("RRN") or data.get("rrn") or log.rrn
    log.approval_code = data.get("ApprovalCode") or data.get("approval_code") or log.approval_code
    log.external_reference = (
        data.get("PlutusTransactionReferenceID") or
        data.get("plutus_transaction_reference_id") or
        log.external_reference
    )

    if log.payment_mode == "Card":
        log.card_number_masked = (
            data.get("CardNumber") or data.get("card_number") or log.card_number_masked
        )
        log.card_type = data.get("CardType") or data.get("card_type") or log.card_type
        log.card_holder_name = (
            data.get("CardHolderName") or
            data.get("card_holder_name") or
            log.card_holder_name
        )
        log.bank_name = data.get("BankName") or data.get("bank_name") or log.bank_name

    current_response = {}
    if log.response_payload:
        try:
            current_response = json.loads(log.response_payload)
        except Exception:
            pass

    current_response["webhook_data"] = data
    current_response["webhook_received_at"] = str(now_datetime())
    log.response_payload = json.dumps(current_response, indent=2)
    log.updated_at = now_datetime()


def _update_settings_sync_time():
    """Update Pine Labs Settings with last sync time"""
    try:
        settings = frappe.get_single("Pinelabs Settings")
        settings.last_sync = now_datetime()
        settings.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.logger().error(f"Failed to update settings last_sync: {str(e)}")


@frappe.whitelist(allow_guest=True)
def handle_pinelabs_webhook():
    """
    Handle webhook notifications from Pine Labs
    This endpoint receives real-time payment status updates

    Webhook URL format:
    https://your-site.com/api/method/pinelabs_fin.api.webhooks.handle_pinelabs_webhook

    Pine Labs will send POST requests with payment status updates
    """
    try:
        data = _validate_webhook_request()
        transaction_ref, response_code, response_message = _extract_webhook_data(data)

        if not transaction_ref:
            return {
                "success": False,
                "error": _("Transaction reference not found in webhook data")
            }

        log = frappe.get_doc("Pinelabs Transaction Log", {
            "transaction_reference": transaction_ref
        })
        previous_status = log.status

        _update_transaction_status(log, response_code, response_message)
        _update_transaction_details(log, data)

        log.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.logger().info(
            f"Webhook processed for {transaction_ref}: "
            f"Status changed from {previous_status} to {log.status}"
        )

        _update_settings_sync_time()

        if log.status == "Approved":
            try:
                auto_update_invoice_payment(log)
            except Exception as e:
                frappe.logger().error(f"Failed to auto-update invoice: {str(e)}")

        return {
            "success": True,
            "message": _("Webhook processed successfully"),
            "transaction_reference": transaction_ref,
            "status": log.status
        }

    except Exception as e:
        frappe.logger().error(f"Webhook processing error: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Pine Labs Webhook Error")
        return {
            "success": False,
            "error": str(e)
        }


def auto_update_invoice_payment(transaction_log):
    """
    Optionally auto-update invoice with payment details
    This is called when webhook receives approved payment

    Args:
        transaction_log: Pinelabs Transaction Log document

    Note: This is optional - you may prefer to handle payment posting
    from the frontend for better user control
    """
    try:
        adapter = get_host_adapter()
        result = adapter.finalize_transaction(transaction_log)

        if result.get("success"):
            frappe.logger().info(
                f"Webhook finalization completed for {transaction_log.transaction_reference}: "
                f"{result.get('invoice_doctype')} {result.get('invoice_name')}"
            )
            return

        frappe.logger().warning(
            f"Webhook finalization did not complete for {transaction_log.transaction_reference}: "
            f"{result.get('error') or result.get('finalization_status')}"
        )

    except Exception as e:
        frappe.logger().error(f"Error in auto_update_invoice_payment: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Pine Labs Auto-Update Invoice Error")
        # Don't raise - we don't want to fail the webhook


@frappe.whitelist()
def get_webhook_url():
    """
    Get the webhook URL to configure in Pine Labs dashboard
    Called from settings page

    Returns:
        Webhook URL string
    """
    try:
        site_url = frappe.utils.get_url()
        webhook_path = "/api/method/pinelabs_fin.api.webhooks.handle_pinelabs_webhook"
        webhook_url = f"{site_url}{webhook_path}"

        # Update settings with webhook URL
        # Note: `Pinelabs Settings` uses `webhooks_url` (plural) in pinelabs_fin.
        # Keep backward-compatible support for `webhook_url` if it exists.
        settings = frappe.get_single("Pinelabs Settings")
        if hasattr(settings, "webhooks_url"):
            settings.webhooks_url = webhook_url
        if hasattr(settings, "webhook_url"):
            settings.webhook_url = webhook_url
        settings.save(ignore_permissions=True)
        frappe.db.commit()

        return {
            "success": True,
            "webhook_url": webhook_url,
            "webhooks_url": webhook_url
        }

    except Exception as e:
        frappe.logger().error(f"Error getting webhook URL: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }
