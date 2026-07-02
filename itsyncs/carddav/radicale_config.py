"""Generate Radicale's htpasswd + rights files from ITSync Mobile Device docs.

itsyncs owns the CardDAV access control: every enrolled device is one Radicale
user, scoped read-only to exactly the address book collection it belongs to.
Writes flow through store.py (direct filesystem), so devices never get write
access — the rights file grants read (rR) only.

Called from the ITSync Mobile Device controller on insert/update/trash, so a
single device revoke regenerates both files without disturbing the others.
Radicale re-reads both files per request, so changes take effect immediately.
"""

import os

import bcrypt
import frappe

from itsyncs.carddav.store import _slug


def _users_file() -> str:
	return frappe.get_site_path("carddav", "users")


def _rights_file() -> str:
	return frappe.get_site_path("carddav", "rights")


def _atomic_write(path: str, text: str) -> None:
	os.makedirs(os.path.dirname(path), exist_ok=True)
	tmp = f"{path}.tmp"
	with open(tmp, "w", encoding="utf-8") as f:
		f.write(text)
	os.replace(tmp, path)


# Static rights policy. NEVER changes per device, so Radicale's start-up cache
# of this file stays valid — only the htpasswd file (re-read per request) needs
# to change when devices are added/revoked, so no Radicale restart is required.
#
# The collection a device may read is encoded in its username as "<slug>.<rand>".
# Radicale substitutes the captured group into the collection regex via {0} and
# the full login via {user}, so a static rule set scopes every device to exactly
# its own address book, read-only.
#
# iOS discovers address books via the principal collection ("/<user>/") and its
# addressbook-home-set — it never uses a direct collection URL. regenerate()
# therefore materializes a principal home per device containing a symlink to the
# shared book, and the rules below grant read on home + linked book. The direct
# path (addressbooks/<slug>) stays readable for DAVx5/manual setups.
RIGHTS_POLICY = """\
# Managed by itsyncs — do not edit. Device access is controlled via htpasswd.
[root]
user = .+
collection =
permissions = R

[device-home]
user = .+
collection = {user}
permissions = R

[device-home-books]
user = ([^.]+)\\..+
collection = {user}/{0}
permissions = rR

[device-collection]
user = ([^.]+)\\..+
collection = addressbooks/{0}
permissions = rR
"""


def device_username(collection_slug: str, base: str, rand: str) -> str:
	"""Build a username that encodes the address book collection slug."""
	return f"{_slug(collection_slug)}.{base}-{rand}"


def regenerate() -> None:
	"""Rewrite the htpasswd file from all non-revoked devices.

	The rights file is static (RIGHTS_POLICY) and rewritten idempotently so it
	self-heals if missing; its content never varies with the device set.
	"""
	devices = frappe.get_all(
		"ITSync Mobile Device",
		filters={"status": ["!=", "Revoked"]},
		fields=["name", "dav_username", "address_book"],
	)

	htpasswd_lines = []
	for d in devices:
		if not d.dav_username or not d.address_book:
			continue
		from frappe.utils.password import get_decrypted_password

		pw = get_decrypted_password(
			"ITSync Mobile Device", d.name, "dav_password", raise_exception=False
		)
		if not pw:
			continue
		hashed = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
		htpasswd_lines.append(f"{d.dav_username}:{hashed}")

	_atomic_write(_users_file(), "\n".join(htpasswd_lines) + ("\n" if htpasswd_lines else ""))
	_atomic_write(_rights_file(), RIGHTS_POLICY)
	_materialize_device_homes(devices)


def _materialize_device_homes(devices) -> None:
	"""Create a principal home per device with a symlink to its address book.

	iOS resolves contacts via principal → addressbook-home-set → children of the
	home collection. Radicale's home set IS the principal collection, so the
	shared book must appear inside "/<user>/". A relative symlink to
	../addressbooks/<slug> serves the same data without duplicating it.
	Homes of removed/revoked devices are pruned (only if they contain nothing
	but our symlinks, so real collections can never be deleted by accident).
	"""
	root = frappe.get_site_path("carddav", "collections", "collection-root")
	os.makedirs(os.path.join(root, "addressbooks"), exist_ok=True)

	slug_cache: dict[str, str] = {}

	def _slug_for(connector_name: str) -> str:
		if connector_name not in slug_cache:
			coll = frappe.db.get_value("ITSync Connector", connector_name, "carddav_collection") or connector_name
			slug_cache[connector_name] = _slug(coll)
		return slug_cache[connector_name]

	keep = set()
	for d in devices:
		if not d.dav_username or not d.address_book:
			continue
		slug = _slug_for(d.address_book)
		home = os.path.join(root, d.dav_username)
		os.makedirs(home, exist_ok=True)
		link = os.path.join(home, slug)
		target = os.path.join("..", "addressbooks", slug)
		if os.path.islink(link):
			if os.readlink(link) != target:
				os.remove(link)
				os.symlink(target, link)
		elif not os.path.exists(link):
			os.symlink(target, link)
		keep.add(d.dav_username)

	for entry in os.listdir(root):
		path = os.path.join(root, entry)
		if entry == "addressbooks" or entry in keep or not os.path.isdir(path):
			continue
		children = os.listdir(path)
		if all(os.path.islink(os.path.join(path, c)) for c in children):
			for c in children:
				os.remove(os.path.join(path, c))
			os.rmdir(path)
