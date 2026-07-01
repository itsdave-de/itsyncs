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

	@frappe.whitelist()
	def start_radicale(self):
		"""Enable + start the Radicale CardDAV service."""
		frappe.only_for("System Manager")
		from itsyncs.carddav import service

		self.db_set("radicale_enabled", 1, update_modified=False)
		service.start()
		service.refresh_status()
		self.reload()
		frappe.msgprint(frappe._("Radicale-Dienst gestartet."), alert=True, indicator="green")
		return {"status": self.radicale_status}

	@frappe.whitelist()
	def stop_radicale(self):
		"""Disable + stop the Radicale CardDAV service."""
		frappe.only_for("System Manager")
		from itsyncs.carddav import service

		self.db_set("radicale_enabled", 0, update_modified=False)
		service.stop()
		service.refresh_status()
		self.reload()
		frappe.msgprint(frappe._("Radicale-Dienst gestoppt."), alert=True, indicator="orange")
		return {"status": self.radicale_status}

	@frappe.whitelist()
	def get_carddav_diagnostics(self):
		"""Live CardDAV diagnostics (Radicale, public URL, cert, counts) for the panel."""
		frappe.only_for("System Manager")
		from itsyncs.carddav import diagnostics

		return diagnostics.get_diagnostics()
