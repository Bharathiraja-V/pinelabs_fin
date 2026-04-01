import frappe

from pinelabs_fin.api import pos_endpoints


class PinelabsFinPosTerminalHandler:
	def supports_mode(self, mode_of_payment):
		label = (mode_of_payment or "").strip().lower()
		return "upi" in label or "card" in label or "credit" in label

	def has_transaction(self, transaction_reference):
		return bool(
			transaction_reference
			and frappe.db.exists(
				"Pinelabs Transaction Log",
				{"transaction_reference": transaction_reference},
			)
		)

	def get_config(self, mode_of_payment=None):
		config = pos_endpoints.get_payment_settings() or {}
		config["available"] = bool(config.get("success")) and self.supports_mode(mode_of_payment or "Card")
		return config

	def initiate_payment(self, invoice_name, amount, mode_of_payment, metadata=None):
		payment_mode = self._normalize_payment_mode(mode_of_payment)
		metadata = metadata or {}
		return pos_endpoints.initiate_payment(
			invoice_name=invoice_name,
			amount=amount,
			payment_mode=payment_mode,
			source_system=metadata.get("source_system"),
			source_doctype=metadata.get("source_doctype"),
			source_docname=metadata.get("source_docname"),
		)

	def check_status(self, transaction_reference):
		return pos_endpoints.check_payment_status(transaction_reference=transaction_reference)

	def cancel_payment(self, transaction_reference):
		return pos_endpoints.cancel_payment(transaction_reference=transaction_reference)

	def _normalize_payment_mode(self, mode_of_payment):
		label = (mode_of_payment or "").strip().lower()
		if "upi" in label:
			return "UPI"
		return "Card"
