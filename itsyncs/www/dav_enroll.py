import frappe

from itsyncs.carddav.enroll import get_device_by_token, qr_data_uri

no_cache = 1


def get_context(context):
	token = frappe.form_dict.get("token")
	if not token:
		frappe.throw("Missing enrollment token.", frappe.DoesNotExistError)

	device = get_device_by_token(token)
	book_label = frappe.db.get_value("ITSync Connector", device.address_book, "title") or "itsyncs Kontakte"

	context.no_cache = 1
	context.book_label = book_label
	context.person_name = device.person_name
	context.dav_server = device.dav_server_url()
	context.dav_username = device.dav_username
	context.dav_password = device.get_password("dav_password")
	context.collection_url = device.dav_server_url() + device.collection_path()
	context.profile_url = f"/api/method/itsyncs.carddav.enroll.download_profile?token={token}"
	context.qr_uri = qr_data_uri(context.collection_url)

	frappe.db.set_value(
		"ITSync Mobile Device",
		device.name,
		{"last_seen": frappe.utils.now_datetime()},
		update_modified=False,
	)
	frappe.db.commit()
	return context
