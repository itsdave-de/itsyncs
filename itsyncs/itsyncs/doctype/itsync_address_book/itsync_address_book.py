import frappe
from frappe.model.document import Document


class ITSyncAddressBook(Document):
	def recount(self):
		count = frappe.db.count("ITSync Contact", {"address_book": self.name})
		self.db_set("contact_count", count, update_modified=False)
