"""Filesystem layer for the Radicale CardDAV storage.

itsyncs writes vCards directly into Radicale's multifilesystem storage (one
.vcf per contact) rather than going through a CardDAV client — the sidecar is
co-located, so a direct write is simpler and avoids an HTTP round-trip in the
sync hot path. Radicale serves read-only; all writes flow through here.

Storage layout (relative to the Frappe site directory):

    <site>/carddav/collections/collection-root/addressbooks/<slug>/
        .Radicale.props          # {"tag": "VADDRESSBOOK", ...}
        <uid>.vcf                # one per contact

The Radicale config's storage.filesystem_folder points at
<site>/carddav/collections, so both processes agree on the path.
"""

import json
import os

import frappe


def _slug(collection: str) -> str:
	"""Sanitize a collection name into a filesystem-safe slug."""
	keep = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in (collection or "").strip().lower())
	return keep.strip("-") or "default"


def collection_dir(collection: str) -> str:
	"""Absolute path to the address book collection directory."""
	return frappe.get_site_path(
		"carddav", "collections", "collection-root", "addressbooks", _slug(collection)
	)


def ensure_collection(collection: str, display_name: str | None = None) -> str:
	"""Create the address book collection (and its Radicale props) if absent."""
	path = collection_dir(collection)
	os.makedirs(path, exist_ok=True)
	props_file = os.path.join(path, ".Radicale.props")
	if not os.path.exists(props_file):
		props = {
			"tag": "VADDRESSBOOK",
			"{DAV:}displayname": display_name or collection,
			"{urn:ietf:params:xml:ns:carddav}addressbook-description": f"itsyncs · {display_name or collection}",
		}
		_atomic_write(props_file, json.dumps(props))
	return path


def write_vcard(collection: str, uid: str, vcard_text: str, display_name: str | None = None) -> None:
	path = ensure_collection(collection, display_name)
	_atomic_write(os.path.join(path, f"{uid}.vcf"), vcard_text)


def delete_vcard(collection: str, uid: str) -> None:
	path = os.path.join(collection_dir(collection), f"{uid}.vcf")
	try:
		os.remove(path)
	except FileNotFoundError:
		pass


def read_all_vcards(collection: str) -> list[str]:
	"""Return the raw text of every .vcf in the collection (empty if none)."""
	path = collection_dir(collection)
	if not os.path.isdir(path):
		return []
	out = []
	for fname in os.listdir(path):
		if not fname.endswith(".vcf"):
			continue
		try:
			with open(os.path.join(path, fname), encoding="utf-8") as f:
				out.append(f.read())
		except OSError:
			continue
	return out


def _atomic_write(path: str, text: str) -> None:
	tmp = f"{path}.tmp"
	with open(tmp, "w", encoding="utf-8") as f:
		f.write(text)
	os.replace(tmp, path)
