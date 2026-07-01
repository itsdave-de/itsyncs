"""Enrollment landing-page helpers + the .mobileconfig download endpoint.

The landing page (itsyncs/www/dav-enroll) is User-Agent aware: iOS gets a
one-tap configuration profile, Android gets DAVx5 instructions + a QR code,
desktop gets a QR to hand off to a phone.
"""

import base64
import io

import frappe


def get_device_by_token(token: str):
	name = frappe.db.get_value("ITSync Mobile Device", {"enroll_token": token}, "name")
	if not name:
		frappe.throw("Unbekannter Enrollment-Link.", frappe.DoesNotExistError)

	device = frappe.get_doc("ITSync Mobile Device", name)
	expires = device.enroll_expires_at
	if expires and frappe.utils.get_datetime(expires) < frappe.utils.now_datetime():
		frappe.throw(
			"Dieser Einrichtungs-Link ist abgelaufen. Bitte fordere beim Administrator einen neuen an.",
			frappe.DoesNotExistError,
		)
	return device


def detect_platform(user_agent: str | None) -> str:
	ua = (user_agent or "").lower()
	if any(x in ua for x in ("iphone", "ipad", "ipod", "mac os")):
		return "iOS"
	if "android" in ua:
		return "Android"
	return "Desktop"


def qr_data_uri(text: str) -> str:
	import qrcode

	img = qrcode.make(text)
	buf = io.BytesIO()
	img.save(buf, format="PNG")
	return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


@frappe.whitelist(allow_guest=True)
def download_profile(token: str):
	"""Return the iOS .mobileconfig for the device behind this token."""
	from itsyncs.carddav.mobileconfig import build_carddav_mobileconfig

	device = get_device_by_token(token)
	book_label = frappe.db.get_value("ITSync Connector", device.address_book, "title") or "itsyncs Kontakte"

	profile = build_carddav_mobileconfig(
		device_name=device.name,
		base_url=device.dav_server_url(),
		username=device.dav_username,
		password=device.get_password("dav_password"),
		principal_path=device.collection_path(),
		book_label=book_label,
	)

	_mark_active(device, "iOS")

	frappe.local.response.filename = "itsyncs-kontakte.mobileconfig"
	frappe.local.response.filecontent = profile
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "attachment"
	frappe.local.response.content_type = "application/x-apple-aspen-config"


def _mark_active(device, platform: str | None = None):
	updates = {"last_seen": frappe.utils.now_datetime()}
	if device.status == "Pending":
		updates["status"] = "Active"
	if platform and device.platform in (None, "", "Unknown"):
		updates["platform"] = platform
	frappe.db.set_value("ITSync Mobile Device", device.name, updates, update_modified=False)
	frappe.db.commit()
