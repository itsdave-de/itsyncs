import frappe
from frappe.model.document import Document


class ITSyncContact(Document):
	def validate(self):
		self._ensure_display_name()
		self._ensure_single_primary_email()

	def after_insert(self):
		self._adjust_book_count(1)

	def on_trash(self):
		self._adjust_book_count(-1)

	def _ensure_display_name(self):
		if self.display_name:
			return
		name = " ".join(p for p in [self.given_name, self.surname] if p).strip()
		self.display_name = name or self.file_as or (self.emails[0].email_address if self.emails else None) or "Kontakt"

	def _ensure_single_primary_email(self):
		primary_seen = False
		for e in self.emails:
			if e.is_primary and not primary_seen:
				primary_seen = True
			elif e.is_primary:
				e.is_primary = 0
		if self.emails and not primary_seen:
			self.emails[0].is_primary = 1

	def _adjust_book_count(self, delta: int):
		"""O(1) atomic counter bump — avoids a full COUNT on every insert during a
		bulk initial sync. ITSync Address Book.recount() stays as the safety net."""
		if self.address_book:
			frappe.db.sql(
				"UPDATE `tabITSync Address Book` SET contact_count = GREATEST(0, contact_count + %s) WHERE name = %s",
				(delta, self.address_book),
			)
