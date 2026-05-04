from frappe.model.document import Document


class PinelabsTestOrder(Document):
	def apply_pinelabs_payment_success(self, transaction_log=None, config=None):
		target_status = (config or {}).get("paid_status_value") or "Paid"
		transaction_reference = getattr(transaction_log, "transaction_reference", None) or None

		if self.docstatus == 0:
			self.payment_status = target_status
			if transaction_reference:
				self.pinelabs_transaction_reference = transaction_reference

			self.flags.ignore_permissions = True
			if str(target_status).strip().lower() in ("submitted", "paid"):
				self.submit()
				return {"message": "Submitted Pinelabs Test Order after PineLabs approval."}

			self.save(ignore_permissions=True)
			return {"message": "Updated Pinelabs Test Order after PineLabs approval."}

		updates = {}
		if self.payment_status != target_status:
			updates["payment_status"] = target_status
		if transaction_reference and self.pinelabs_transaction_reference != transaction_reference:
			updates["pinelabs_transaction_reference"] = transaction_reference

		if updates:
			self.db_set(updates, notify=True)
			return {"message": "Updated Pinelabs Test Order after PineLabs approval."}

		return {"message": "Pinelabs Test Order already reflects PineLabs approval."}
