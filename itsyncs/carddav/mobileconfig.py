"""Build an Apple .mobileconfig profile for a CardDAV mobile device.

iOS installs a com.apple.carddav.account payload natively — one tap and the
read-only address book appears in the Contacts app. UUIDs are derived
deterministically from the device, so re-installing updates the same profile
instead of creating a duplicate account.

The profile points CardDAVPrincipalURL at the exact shared collection so iOS
does not need principal discovery against a per-user home set.
"""

import plistlib
import uuid
from urllib.parse import urlparse

_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # uuid5 URL namespace


def _uuid(*parts) -> str:
	return str(uuid.uuid5(_NS, "itsyncs:" + ":".join(str(p) for p in parts)))


def build_carddav_mobileconfig(
	device_name: str,
	base_url: str,
	username: str,
	password: str,
	principal_path: str,
	book_label: str,
) -> bytes:
	parsed = urlparse(base_url)
	host = parsed.hostname or base_url
	use_ssl = (parsed.scheme or "https") == "https"
	port = parsed.port or (443 if use_ssl else 80)

	carddav_payload = {
		"PayloadType": "com.apple.carddav.account",
		"PayloadVersion": 1,
		"PayloadIdentifier": f"de.itsdave.itsyncs.carddav.{device_name}",
		"PayloadUUID": _uuid(device_name, "carddav"),
		"PayloadDisplayName": book_label,
		"CardDAVAccountDescription": book_label,
		"CardDAVHostName": host,
		"CardDAVUsername": username,
		"CardDAVPassword": password,
		"CardDAVUseSSL": use_ssl,
		"CardDAVPort": port,
		"CardDAVPrincipalURL": principal_path,
	}

	profile = {
		"PayloadType": "Configuration",
		"PayloadVersion": 1,
		"PayloadIdentifier": f"de.itsdave.itsyncs.{device_name}",
		"PayloadUUID": _uuid(device_name, "profile"),
		"PayloadDisplayName": f"{book_label} (itsyncs)",
		"PayloadDescription": f"Synchronisiert „{book_label}“ schreibgeschützt auf dieses iPhone.",
		"PayloadOrganization": "itsdave",
		"PayloadRemovalDisallowed": False,
		"PayloadContent": [carddav_payload],
	}
	return plistlib.dumps(profile)
