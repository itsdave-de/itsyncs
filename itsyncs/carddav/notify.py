"""Deliver enrollment invitations by email and SMS (seven.io).

Both channels carry the same short enrollment URL — the landing page itself
(itsyncs/www/dav_enroll) detects the device and serves the right path (iOS
profile / Android DAVx5 / QR). A configuration profile cannot travel inside an
SMS, so SMS only ever carries the link.

seven.io credentials live in ITSync Settings:
    sms_api_key (Password), sms_sender (Data, optional)
"""

import frappe


def _book_label(device) -> str:
	return frappe.db.get_value("ITSync Connector", device.address_book, "title") or "itsyncs Kontakte"


def _email_html(device, url: str, book: str) -> str:
	return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:auto">
	<h2 style="margin:0 0 6px">{frappe.utils.escape_html(book)} auf deinem Handy</h2>
	<p style="color:#475569;font-size:14px">Hallo {frappe.utils.escape_html(device.person_name or "")},
	mit einem Tipp richtest du deine Firmenkontakte schreibgeschützt auf deinem Smartphone ein.</p>
	<p style="margin:22px 0">
		<a href="{url}" style="background:#2563eb;color:#fff;text-decoration:none;
		padding:14px 22px;border-radius:10px;font-weight:600;display:inline-block">Jetzt einrichten</a>
	</p>
	<p style="color:#94a3b8;font-size:12px">Funktioniert auf iPhone (Profil) und Android (DAVx⁵).
	Falls der Button nicht geht: <a href="{url}">{url}</a></p>
</div>"""


def send_enrollment_email(device) -> None:
	if not device.email:
		frappe.throw("This device has no email address.")
	book = _book_label(device)
	url = device.enrollment_url()
	frappe.sendmail(
		recipients=[device.email],
		subject=f"{book} auf deinem Handy einrichten",
		message=_email_html(device, url, book),
		reference_doctype=device.doctype,
		reference_name=device.name,
	)


def send_enrollment_sms(device) -> dict:
	if not device.mobile_number:
		frappe.throw("This device has no mobile number.")
	book = _book_label(device)
	url = device.enrollment_url()
	text = f"{book}: Kontakte aufs Handy einrichten → {url}"
	return _seven_send(device.mobile_number, text, device=device.name)


def _seven_send(recipient: str, message: str, device: str | None = None) -> dict:
	"""Send one SMS via seven.io and record an ITSync SMS Log entry.

	Every attempt — success, API error, or network failure — produces a log row
	with the full message text and raw API response before any error is raised.
	"""
	import requests

	settings = frappe.get_doc("ITSync Settings")
	api_key = (settings.get_password("sms_api_key", raise_exception=False) or "").strip()
	if not api_key:
		frappe.throw("seven.io API key not configured (ITSync Settings → SMS-Versand).")

	# json=1 makes seven.io return a structured object; without it the API
	# replies with a bare status code (e.g. 100), which is not a JSON dict.
	data = {"to": _normalize_msisdn(recipient), "text": message, "json": "1"}
	sender = (settings.sms_sender or "").strip()
	if sender:
		data["from"] = sender

	status, error, raw = "Failed", None, ""
	code, messages, balance = "", [], None
	try:
		resp = requests.post(
			"https://gateway.seven.io/api/sms",
			headers={"X-Api-Key": api_key, "Accept": "application/json"},
			data=data,
			timeout=20,
		)
		raw = resp.text
		try:
			body = resp.json()
		except ValueError:
			body = None
		if isinstance(body, dict):
			code = str(body.get("success", "")).strip()
			messages = body.get("messages") or []
			balance = body.get("balance")
		else:
			# Plain scalar/text response: the value itself is the status code.
			code = (str(body) if body is not None else raw).strip().strip('"')
		# seven.io signals success with status code "100".
		if resp.status_code == 200 and code == "100":
			status = "Sent"
		else:
			error = f"HTTP {resp.status_code}, code {code or raw[:200]}"
	except Exception as e:
		error = str(e)
		raw = raw or error

	first = messages[0] if messages else {}
	log_name = _log_sms(
		recipient=recipient, message=message, status=status, device=device,
		sms_id=first.get("id"), price=first.get("price"), balance=balance,
		error=error, raw=raw or "",
	)

	if status != "Sent":
		frappe.throw(f"seven.io SMS failed: {error}")

	return {"ok": True, "recipient": recipient, "log": log_name, "balance": balance}


def _log_sms(recipient, message, status, device=None, sms_id=None, price=None, balance=None, error=None, raw=None) -> str:
	doc = frappe.new_doc("ITSync SMS Log")
	doc.recipient = recipient
	doc.message = message
	doc.status = status
	doc.device = device
	doc.provider = "seven.io"
	doc.sent_at = frappe.utils.now_datetime()
	doc.sms_id = str(sms_id) if sms_id is not None else None
	doc.price = str(price) if price is not None else None
	doc.balance = str(balance) if balance is not None else None
	doc.error = error
	doc.api_response = raw
	doc.insert(ignore_permissions=True)
	# Commit so the log survives the frappe.throw that follows a failed send.
	frappe.db.commit()
	return doc.name


@frappe.whitelist()
def bulk_invite(address_book: str | None = None, channel: str = "email") -> dict:
	"""Invite all Pending devices (optionally for one address book) via a channel."""
	# Cost-bearing (SMS) + outbound messaging — restrict to admins.
	frappe.only_for("System Manager")
	filters = {"status": "Pending"}
	if address_book:
		filters["address_book"] = address_book
	names = frappe.get_all("ITSync Mobile Device", filters=filters, pluck="name")

	sent = 0
	failed = []
	for name in names:
		device = frappe.get_doc("ITSync Mobile Device", name)
		try:
			if channel == "sms":
				send_enrollment_sms(device)
			else:
				send_enrollment_email(device)
			sent += 1
		except Exception as e:
			failed.append({"device": name, "error": str(e)})
	return {"sent": sent, "failed": failed}


_SEVEN_ERROR_CODES = {
	"900": "API-Key ungültig (Authentifizierung fehlgeschlagen)",
	"902": "API-Key hat keine Berechtigung für diese Aktion",
	"903": "Server-IP nicht freigegeben",
}


def seven_balance() -> dict:
	"""Query the seven.io account balance. Doubles as an API-key validator —
	a wrong key returns code 900 instead of a balance amount."""
	import requests

	settings = frappe.get_doc("ITSync Settings")
	api_key = (settings.get_password("sms_api_key", raise_exception=False) or "").strip()
	if not api_key:
		frappe.throw("seven.io API key not configured (ITSync Settings → SMS-Versand).")

	try:
		resp = requests.get(
			"https://gateway.seven.io/api/balance",
			headers={"X-Api-Key": api_key, "Accept": "application/json"},
			timeout=15,
		)
	except Exception as e:
		frappe.throw(f"seven.io nicht erreichbar: {e}")

	try:
		body = resp.json()
	except ValueError:
		body = (resp.text or "").strip()

	# Valid key → {"amount": 0.5, "currency": "EUR"}; invalid key → bare code "900".
	if isinstance(body, dict) and body.get("amount") is not None:
		return {"amount": float(body["amount"]), "currency": body.get("currency") or "EUR"}

	code = str(body).strip().strip('"')
	frappe.throw(f"Guthaben-Abruf fehlgeschlagen: {_SEVEN_ERROR_CODES.get(code, f'Code {code}')}")


def check_and_store_balance() -> dict:
	"""Query the balance and persist the outcome (amount + date + result) to
	ITSync Settings. Never raises — used by both the manual button and the
	daily scheduled check. Commits so the stored result survives a later throw.
	"""
	settings = frappe.get_single("ITSync Settings")
	now = frappe.utils.now_datetime()
	try:
		res = seven_balance()
		settings.db_set("sms_balance", f"{res['amount']:.2f} {res['currency']}", update_modified=False)
		settings.db_set("sms_balance_result", "OK", update_modified=False)
		settings.db_set("sms_balance_checked_at", now, update_modified=False)
		frappe.db.commit()
		return {"ok": True, "amount": res["amount"], "currency": res["currency"]}
	except Exception as e:
		settings.db_set("sms_balance_result", str(e)[:200], update_modified=False)
		settings.db_set("sms_balance_checked_at", now, update_modified=False)
		frappe.db.commit()
		return {"ok": False, "error": str(e)}


def _normalize_msisdn(number: str) -> str:
	"""Best-effort E.164 normalization for German numbers."""
	n = "".join(ch for ch in (number or "") if ch.isdigit() or ch == "+")
	if n.startswith("+"):
		return n
	if n.startswith("00"):
		return "+" + n[2:]
	if n.startswith("0"):
		return "+49" + n[1:]
	return "+" + n
