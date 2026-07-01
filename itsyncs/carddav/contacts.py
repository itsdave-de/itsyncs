"""Engine-facing CRUD for the CardDAV mobile address book target.

Mirrors the shape of the Graph (graph/contacts.py) and Exchange
(graph/exchange.py) target modules so itsyncs/sync/engine.py can dispatch to
it uniformly. The "target id" returned/accepted here is the vCard UID, which
is also the .vcf filename stem.
"""

from itsyncs.carddav import store
from itsyncs.carddav.vcard import build_vcard, contact_uid, parse_vcard


def _collection(target_conn) -> str:
	return (target_conn.carddav_collection or "").strip() or (target_conn.title or "default")


def _display(target_conn) -> str:
	return target_conn.title or _collection(target_conn)


def create_carddav_contact(target_conn, contact: dict) -> str:
	uid = contact_uid(contact["id"])
	store.write_vcard(_collection(target_conn), uid, build_vcard(contact, uid), _display(target_conn))
	return uid


def update_carddav_contact(target_conn, target_id: str, contact: dict) -> None:
	store.write_vcard(_collection(target_conn), target_id, build_vcard(contact, target_id), _display(target_conn))


def delete_carddav_contact(target_conn, target_id: str) -> None:
	store.delete_vcard(_collection(target_conn), target_id)


def fetch_all_carddav_contacts(target_conn) -> list[dict]:
	contacts = []
	for text in store.read_all_vcards(_collection(target_conn)):
		parsed = parse_vcard(text)
		if parsed and parsed.get("id"):
			contacts.append(parsed)
	return contacts
