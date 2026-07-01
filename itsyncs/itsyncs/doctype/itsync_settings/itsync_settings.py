import frappe
from frappe.model.document import Document


class ITSyncSettings(Document):
	@frappe.whitelist()
	def send_test_sms(self, to: str, message: str | None = None):
		"""Send a test SMS via seven.io and log it. Used by the Settings action."""
		frappe.only_for("System Manager")
		from itsyncs.carddav.notify import _seven_send

		text = message or "itsyncs Test-SMS ✓"
		result = _seven_send(to, text)
		frappe.msgprint(
			frappe._("Test-SMS an {0} versendet (Log: {1}).").format(to, result["log"]),
			alert=True,
			indicator="green",
		)
		return result

	@frappe.whitelist()
	def get_sms_balance(self):
		"""Show the seven.io account balance, store the outcome, validate the key."""
		frappe.only_for("System Manager")
		from itsyncs.carddav.notify import check_and_store_balance

		result = check_and_store_balance()
		self.reload()
		if not result["ok"]:
			frappe.throw(result["error"])

		amount = frappe.utils.fmt_money(result["amount"], currency=result["currency"])
		indicator = "red" if result["amount"] < 1 else "green"
		frappe.msgprint(
			frappe._("seven.io Guthaben: {0}").format(amount),
			title=frappe._("Guthaben"),
			indicator=indicator,
		)
		return result
