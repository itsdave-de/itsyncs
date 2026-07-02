"""CardDAV operations diagnostics for the ITSync Settings panel.

Gathers a live picture of the mobile-sync plumbing: Radicale liveness, whether
it actually answers CardDAV, the public URL's reachability, its TLS certificate
(issuer/expiry/SAN), and vCard/device counts. The key indicators are also
persisted to ITSync Settings so they can be queried directly from the DB.
"""

import datetime
import os
import socket
import ssl
from urllib.parse import urlparse

import frappe


def _now_utc():
	return datetime.datetime.now(datetime.timezone.utc)


def _local_serving(port: int) -> dict:
	"""Does Radicale answer a CardDAV OPTIONS on localhost?"""
	try:
		import httpx

		r = httpx.request("OPTIONS", f"http://127.0.0.1:{port}/", timeout=4)
		dav = r.headers.get("DAV", "")
		return {"ok": True, "carddav": "addressbook" in dav.lower(), "dav_header": dav}
	except Exception as e:
		return {"ok": False, "error": str(e)[:120]}


def _cert_info(host: str, port: int) -> dict:
	"""Fetch and parse the peer TLS certificate (works for public + self-signed)."""
	ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
	ctx.check_hostname = False
	ctx.verify_mode = ssl.CERT_NONE
	try:
		with socket.create_connection((host, port), timeout=6) as sock:
			with ctx.wrap_socket(sock, server_hostname=host) as ssock:
				der = ssock.getpeercert(binary_form=True)
	except Exception as e:
		return {"reachable": False, "error": str(e)[:120]}

	try:
		from cryptography import x509

		cert = x509.load_der_x509_certificate(der)
		try:
			not_after = cert.not_valid_after_utc
		except AttributeError:  # older cryptography
			not_after = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
		try:
			san = cert.extensions.get_extension_for_class(
				x509.SubjectAlternativeName
			).value.get_values_for_type(x509.DNSName)
		except Exception:
			san = []
		info = {
			"reachable": True,
			"subject": cert.subject.rfc4514_string(),
			"issuer": cert.issuer.rfc4514_string(),
			"san": san,
			"not_after": not_after.isoformat(),
			"days_remaining": (not_after - _now_utc()).days,
		}
	except Exception as e:
		return {"reachable": True, "error": f"cert parse: {str(e)[:100]}"}

	# Is it trusted by the default store (public CA), and does the hostname match?
	trusted = True
	try:
		vctx = ssl.create_default_context()
		with socket.create_connection((host, port), timeout=6) as sock:
			with vctx.wrap_socket(sock, server_hostname=host):
				pass
	except Exception:
		trusted = False
	info["trusted"] = trusted
	return info


def _collection_stats() -> list:
	from itsyncs.carddav import store

	books = frappe.get_all("ITSync Connector", filters={"connector_type": "CardDAV"}, fields=["name", "carddav_collection"])
	out = []
	for b in books:
		coll_dir = store.collection_dir(b.carddav_collection or b.name)
		n = len([f for f in os.listdir(coll_dir) if f.endswith(".vcf")]) if os.path.isdir(coll_dir) else 0
		out.append({"connector": b.name, "collection": b.carddav_collection, "vcards": n})
	return out


def _signing_status() -> dict:
	"""Is .mobileconfig signing configured and usable?"""
	cert_path = frappe.conf.get("carddav_profile_sign_cert")
	key_path = frappe.conf.get("carddav_profile_sign_key")
	if not (cert_path and key_path):
		return {"configured": False}
	if not (os.path.isfile(cert_path) and os.access(cert_path, os.R_OK)
			and os.path.isfile(key_path) and os.access(key_path, os.R_OK)):
		return {"configured": True, "active": False, "error": "Cert/Key-Datei fehlt oder nicht lesbar"}
	try:
		from cryptography import x509

		with open(cert_path, "rb") as f:
			cert = x509.load_pem_x509_certificates(f.read())[0]
		try:
			not_after = cert.not_valid_after_utc
		except AttributeError:
			not_after = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
		return {
			"configured": True,
			"active": True,
			"subject": cert.subject.rfc4514_string(),
			"days_remaining": (not_after - _now_utc()).days,
		}
	except Exception as e:
		return {"configured": True, "active": False, "error": str(e)[:100]}


def get_diagnostics() -> dict:
	from itsyncs.carddav import service

	port = service._port()
	base_url = (frappe.conf.get("carddav_base_url") or "").strip()

	diag = {
		"radicale": {
			"enabled": bool(frappe.db.get_single_value("ITSync Settings", "radicale_enabled")),
			"running": service.is_running(),
			"port": port,
			"serving": _local_serving(port),
		},
		"public_url": base_url or None,
		"public": None,
		"collections": _collection_stats(),
		"devices": {
			"active": frappe.db.count("ITSync Mobile Device", {"status": "Active"}),
			"pending": frappe.db.count("ITSync Mobile Device", {"status": "Pending"}),
			"revoked": frappe.db.count("ITSync Mobile Device", {"status": "Revoked"}),
		},
		"profile_signing": _signing_status(),
		"checked_at": frappe.utils.now_datetime().isoformat(),
	}

	if base_url:
		parsed = urlparse(base_url)
		host = parsed.hostname
		p = parsed.port or (443 if parsed.scheme == "https" else 80)
		diag["public"] = _cert_info(host, p) if parsed.scheme == "https" else {"reachable": None, "note": "kein HTTPS"}

	_persist(diag)
	return diag


def _persist(diag: dict) -> None:
	"""Store the key indicators in ITSync Settings for direct DB access."""
	settings = frappe.get_single("ITSync Settings")
	settings.db_set("radicale_status", "Running" if diag["radicale"]["running"] else "Stopped", update_modified=False)
	settings.db_set("radicale_status_checked_at", frappe.utils.now_datetime(), update_modified=False)

	pub = diag.get("public") or {}
	if diag.get("public_url"):
		settings.db_set(
			"carddav_public_status",
			"OK" if pub.get("reachable") else f"Fehler: {pub.get('error', 'nicht erreichbar')[:60]}",
			update_modified=False,
		)
		if pub.get("not_after"):
			settings.db_set("carddav_cert_expiry", pub["not_after"][:19].replace("T", " "), update_modified=False)
	frappe.db.commit()
