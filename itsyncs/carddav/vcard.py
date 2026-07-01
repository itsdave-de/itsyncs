"""Render and parse vCards for the CardDAV mobile address book.

The contact dict is the same normalized format every itsyncs source emits
(see itsyncs/sync/matching.py): display_name, given_name, surname,
email_addresses [{address}], business_phones [str], mobile_phone,
company_name, job_title, department, office_location, business_address.

vCard 3.0 is used deliberately — it has the broadest native support across
iOS and Android/DAVx5 clients.
"""

import hashlib

import vobject


def contact_uid(source_id: str) -> str:
	"""Stable, filesystem-safe UID derived from the source contact id.

	Used both as the vCard UID and as the .vcf filename stem, so the same
	source contact always maps to the same CardDAV item across syncs.
	"""
	digest = hashlib.sha1((source_id or "").encode("utf-8")).hexdigest()[:20]
	return f"itsync-{digest}"


def _emails(contact: dict) -> list[str]:
	return [
		e["address"].strip()
		for e in (contact.get("email_addresses") or [])
		if e.get("address")
	]


def build_vcard(contact: dict, uid: str) -> str:
	"""Render a normalized contact dict to a vCard 3.0 string."""
	card = vobject.vCard()
	card.add("uid").value = uid

	emails = _emails(contact)
	display = (
		(contact.get("display_name") or "").strip()
		or " ".join(p for p in [contact.get("given_name"), contact.get("surname")] if p).strip()
		or (emails[0] if emails else uid)
	)
	card.add("fn").value = display

	n = card.add("n")
	n.value = vobject.vcard.Name(
		family=(contact.get("surname") or "").strip(),
		given=(contact.get("given_name") or "").strip(),
	)

	org_parts = [p for p in [contact.get("company_name"), contact.get("department")] if (p or "").strip()]
	if org_parts:
		card.add("org").value = org_parts

	if (contact.get("job_title") or "").strip():
		card.add("title").value = contact["job_title"].strip()

	for i, addr in enumerate(emails):
		e = card.add("email")
		e.value = addr
		e.type_paramlist = ["WORK", "PREF"] if i == 0 else ["WORK"]

	for phone in (contact.get("business_phones") or []):
		if (phone or "").strip():
			t = card.add("tel")
			t.value = phone.strip()
			t.type_paramlist = ["WORK", "VOICE"]

	if (contact.get("mobile_phone") or "").strip():
		t = card.add("tel")
		t.value = contact["mobile_phone"].strip()
		t.type_paramlist = ["CELL", "VOICE"]

	ba = contact.get("business_address") or {}
	if any((ba.get(k) or "").strip() for k in ("street", "city", "state", "postal_code", "country_or_region")):
		a = card.add("adr")
		a.value = vobject.vcard.Address(
			street=(ba.get("street") or "").strip(),
			city=(ba.get("city") or "").strip(),
			region=(ba.get("state") or "").strip(),
			code=(ba.get("postal_code") or "").strip(),
			country=(ba.get("country_or_region") or "").strip(),
		)
		a.type_param = "WORK"

	return card.serialize()


def parse_vcard(text: str) -> dict | None:
	"""Parse a stored .vcf back into the normalized contact dict.

	Used by the CardDAV target's fetch path so the sync engine can match and
	reconcile against the items it previously wrote. Returns None on parse
	failure (a malformed file should not abort a whole sync).
	"""
	try:
		card = vobject.readOne(text)
	except Exception:
		return None

	def _get(name):
		return getattr(card, name).value if hasattr(card, name) else None

	emails = []
	for e in card.contents.get("email", []):
		if e.value:
			emails.append({"address": e.value.strip()})

	business_phones = []
	mobile_phone = ""
	for t in card.contents.get("tel", []):
		types = [x.upper() for x in (getattr(t, "type_paramlist", None) or [])]
		if "CELL" in types and not mobile_phone:
			mobile_phone = t.value.strip()
		else:
			business_phones.append(t.value.strip())

	name = _get("n")
	org = _get("org") or []
	return {
		"id": _get("uid"),
		"display_name": (_get("fn") or "").strip(),
		"given_name": (getattr(name, "given", "") or "").strip() if name else "",
		"surname": (getattr(name, "family", "") or "").strip() if name else "",
		"email_addresses": emails,
		"business_phones": business_phones,
		"mobile_phone": mobile_phone,
		"company_name": (org[0].strip() if len(org) > 0 else ""),
		"department": (org[1].strip() if len(org) > 1 else ""),
		"job_title": (_get("title") or "").strip(),
		"office_location": "",
		"business_address": {},
	}
