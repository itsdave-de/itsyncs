import secrets

import frappe
from frappe.model.document import Document


class ITSyncMobileDevice(Document):
	def before_insert(self):
		if not self.dav_username:
			self.dav_username = self._generate_username()
		if not self.get_password("dav_password", raise_exception=False):
			self.dav_password = secrets.token_urlsafe(18)
		if not self.enroll_token:
			self.enroll_token = secrets.token_urlsafe(24)
		if not self.enrolled_at:
			self.enrolled_at = frappe.utils.now_datetime()
		if not self.enroll_expires_at:
			self.enroll_expires_at = self._new_expiry()

	def validate(self):
		book = frappe.get_doc("ITSync Connector", self.address_book)
		if book.connector_type != "CardDAV":
			frappe.throw("Address Book must be a CardDAV connector.")

	def on_update(self):
		self._sync_radicale()

	def after_insert(self):
		self._sync_radicale()

	def on_trash(self):
		# regenerate after the row is gone so it drops out of the htpasswd/rights
		frappe.enqueue_doc(self.doctype, self.name, "_sync_radicale_now", queue="short", enqueue_after_commit=True)

	def _sync_radicale(self):
		from itsyncs.carddav import radicale_config

		radicale_config.regenerate()

	def _sync_radicale_now(self):
		# Not whitelisted: only invoked server-side via enqueue_doc on_trash.
		self._sync_radicale()

	@frappe.whitelist()
	def send_email_invite(self):
		frappe.only_for("System Manager")
		from itsyncs.carddav.notify import send_enrollment_email

		self._refresh_enroll_expiry()  # clock starts on send
		send_enrollment_email(self)
		frappe.msgprint(f"Enrollment email queued to {self.email}.", alert=True, indicator="green")

	@frappe.whitelist()
	def send_sms_invite(self):
		frappe.only_for("System Manager")
		from itsyncs.carddav.notify import send_enrollment_sms

		self._refresh_enroll_expiry()  # clock starts on send
		send_enrollment_sms(self)
		frappe.msgprint(f"Enrollment SMS sent to {self.mobile_number}.", alert=True, indicator="green")

	@frappe.whitelist()
	def reissue_enrollment_link(self):
		"""Rotate the enrollment token and refresh its expiry. The old link dies;
		the device (and any already-configured phone) keeps working — ongoing sync
		uses the CardDAV credentials, not this token."""
		frappe.only_for("System Manager")
		self.db_set("enroll_token", secrets.token_urlsafe(24), update_modified=False)
		self.db_set("enroll_expires_at", self._new_expiry(), update_modified=False)
		frappe.msgprint("Neuer Enrollment-Link erzeugt. Der alte Link ist ungültig.", alert=True, indicator="green")
		return {"enroll_token": self.enroll_token}

	def _enroll_validity_hours(self) -> int:
		# Distinguish "unset" (single predates the field) → default 1, from an
		# explicit 0 which means "never expires".
		raw = frappe.db.get_single_value("ITSync Settings", "enroll_token_validity_hours")
		if raw in (None, ""):
			return 1
		return frappe.utils.cint(raw)

	def _new_expiry(self):
		hours = self._enroll_validity_hours()
		if hours <= 0:
			return None  # 0 = never expires (opt-out)
		return frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=hours)

	def _refresh_enroll_expiry(self):
		self.db_set("enroll_expires_at", self._new_expiry(), update_modified=False)

	def _generate_username(self) -> str:
		from itsyncs.carddav.radicale_config import device_username

		collection = frappe.db.get_value("ITSync Connector", self.address_book, "carddav_collection") or self.address_book
		local = (self.email or self.person_name or "device").split("@")[0]
		base = "".join(ch for ch in local.lower() if ch.isalnum()) or "device"
		for _ in range(20):
			candidate = device_username(collection, base, secrets.token_hex(3))
			if not frappe.db.exists("ITSync Mobile Device", {"dav_username": candidate}):
				return candidate
		return device_username(collection, base, secrets.token_hex(8))

	def enrollment_url(self) -> str:
		base = (
			frappe.conf.get("carddav_enroll_base_url")
			or frappe.utils.get_url()
		).rstrip("/")
		return f"{base}/dav-enroll/{self.enroll_token}"

	def dav_server_url(self) -> str:
		"""Public CardDAV base URL the device connects to (from site config)."""
		return (frappe.conf.get("carddav_base_url") or "").rstrip("/")

	def collection_path(self) -> str:
		from itsyncs.carddav.store import _slug

		slug = _slug(frappe.db.get_value("ITSync Connector", self.address_book, "carddav_collection") or self.address_book)
		return f"/addressbooks/{slug}/"
