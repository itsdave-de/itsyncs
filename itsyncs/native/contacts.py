"""Engine-facing CRUD + fetch for the native address book connector.

Mirrors the shape of graph/contacts.py and carddav/contacts.py so the sync
engine dispatches uniformly. The "target id" / "source id" is the ITSync
Contact document name.
"""

import frappe

from itsyncs.native.format import apply_normalized, contact_to_normalized


def _book(conn) -> str:
	return conn.address_book


def fetch_all_native_contacts(conn) -> list[dict]:
	names = frappe.get_all("ITSync Contact", filters={"address_book": _book(conn)}, pluck="name")
	return [contact_to_normalized(frappe.get_doc("ITSync Contact", n)) for n in names]


def fetch_native_delta(conn, since_iso, pair_name):
	"""Return (changed, deleted_ids, new_token) for the native source.

	changed = contacts modified since the token (timestamp delta).
	deleted_ids = mapped source_ids no longer present in the book (full-id compare —
	one cheap query, since timestamp deltas cannot report deletions).
	"""
	book = _book(conn)
	changed_filters = {"address_book": book}
	if since_iso:
		changed_filters["modified"] = [">", since_iso]
	changed_names = frappe.get_all("ITSync Contact", filters=changed_filters, pluck="name")
	changed = [contact_to_normalized(frappe.get_doc("ITSync Contact", n)) for n in changed_names]

	present = set(frappe.get_all("ITSync Contact", filters={"address_book": book}, pluck="name"))
	mapped = frappe.get_all(
		"ITSync Mapping",
		filters={"sync_pair": pair_name, "status": ["!=", "Conflict"]},
		pluck="source_id",
	)
	deleted_ids = [sid for sid in mapped if sid not in present]

	new_token = frappe.utils.now_datetime().isoformat()
	return changed, deleted_ids, new_token


def create_native_contact(conn, data: dict) -> str:
	doc = frappe.new_doc("ITSync Contact")
	doc.address_book = _book(conn)
	apply_normalized(doc, data)
	doc.insert(ignore_permissions=True)
	return doc.name


def update_native_contact(conn, target_id: str, data: dict) -> None:
	doc = frappe.get_doc("ITSync Contact", target_id)
	apply_normalized(doc, data)
	doc.save(ignore_permissions=True)


def delete_native_contact(conn, target_id: str) -> None:
	if frappe.db.exists("ITSync Contact", target_id):
		frappe.delete_doc("ITSync Contact", target_id, ignore_permissions=True, force=True)
