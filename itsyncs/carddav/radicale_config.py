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
# Radicale substitutes the captured group into the collection regex via {0}, so a
# single rule scopes every device to exactly its own address book, read-only.
RIGHTS_POLICY = """\
# Managed by itsyncs — do not edit. Device access is controlled via htpasswd.
[device-collection]
user = ([^.]+)\\..+
collection = addressbooks/{0}
permissions = rR

[own-principal]
user = (.+)
collection = {0}
permissions = R
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
		pw = frappe.utils.password.get_decrypted_password(
			"ITSync Mobile Device", d.name, "dav_password", raise_exception=False
		)
		if not pw:
			continue
		hashed = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
		htpasswd_lines.append(f"{d.dav_username}:{hashed}")

	_atomic_write(_users_file(), "\n".join(htpasswd_lines) + ("\n" if htpasswd_lines else ""))
	_atomic_write(_rights_file(), RIGHTS_POLICY)
