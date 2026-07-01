"""Map an ITSync Contact document to/from the normalized interchange dict.

The normalized dict is the same format every itsyncs connector speaks (see
itsyncs/sync/matching.py + itsyncs/graph/contacts.py). The native address book
carries the full O365-aligned field set; multi-valued fields (emails, phones,
addresses) live in child tables and are flattened here.
"""


def _addr_from_child(row) -> dict:
	return {
		"street": row.street,
		"city": row.city,
		"state": row.state,
		"postal_code": row.postal_code,
		"country_or_region": row.country,
	}


def contact_to_normalized(doc) -> dict:
	"""ITSync Contact doc → normalized contact dict (id = contact name)."""
	emails = [
		{"address": e.email_address, "name": e.email_name}
		for e in doc.emails
		if e.email_address
	]
	business_phones = [p.number for p in doc.phones if p.phone_type == "Business" and p.number]
	home_phones = [p.number for p in doc.phones if p.phone_type == "Home" and p.number]
	mobiles = [p.number for p in doc.phones if p.phone_type == "Mobile" and p.number]

	def addr(kind):
		for a in doc.addresses:
			if a.address_type == kind and any([a.street, a.city, a.state, a.postal_code, a.country]):
				return _addr_from_child(a)
		return None

	return {
		"id": doc.name,
		"display_name": doc.display_name,
		"given_name": doc.given_name,
		"surname": doc.surname,
		"middle_name": doc.middle_name,
		"nickname": doc.nickname,
		"title": doc.title,
		"generation": doc.generation,
		"initials": doc.initials,
		"file_as": doc.file_as,
		"company_name": doc.company_name,
		"job_title": doc.job_title,
		"department": doc.department,
		"profession": doc.profession,
		"office_location": doc.office_location,
		"business_home_page": doc.business_home_page,
		"manager": doc.manager,
		"assistant_name": doc.assistant_name,
		"email_addresses": emails,
		"business_phones": business_phones,
		"home_phones": home_phones,
		"mobile_phone": mobiles[0] if mobiles else None,
		"business_address": addr("Business"),
		"home_address": addr("Home"),
		"other_address": addr("Other"),
		"birthday": str(doc.birthday) if doc.birthday else None,
		"spouse_name": doc.spouse_name,
		"categories": [c.strip() for c in (doc.categories or "").split(",") if c.strip()],
		"personal_notes": doc.personal_notes,
	}


_SCALAR_FIELDS = (
	"display_name", "given_name", "surname", "middle_name", "nickname", "title",
	"generation", "initials", "file_as", "company_name", "job_title", "department",
	"profession", "office_location", "business_home_page", "manager", "assistant_name",
	"spouse_name", "personal_notes",
)


def _cap(val, n=140):
	"""Truncate to a Data field's max length so pathological source values (an
	over-long company name etc.) don't fail the whole contact insert."""
	if isinstance(val, str) and len(val) > n:
		return val[:n]
	return val


def apply_normalized(doc, data: dict) -> None:
	"""Populate an ITSync Contact doc from a normalized contact dict (target write)."""
	for f in _SCALAR_FIELDS:
		# personal_notes is a Text field — never truncated.
		val = data.get(f)
		doc.set(f, val if f == "personal_notes" else _cap(val))

	doc.birthday = data.get("birthday") or None
	cats = data.get("categories")
	doc.categories = ", ".join(cats) if isinstance(cats, list) else (cats or None)

	doc.set("emails", [])
	primary_assigned = False
	for e in data.get("email_addresses") or []:
		addr = (e.get("address") or "").strip()
		if not addr:
			continue
		doc.append("emails", {
			"email_address": _cap(addr),
			"email_name": _cap(e.get("name")),
			"is_primary": 0 if primary_assigned else 1,
		})
		primary_assigned = True

	doc.set("phones", [])
	for n in data.get("business_phones") or []:
		if n and str(n).strip():
			doc.append("phones", {"number": _cap(n), "phone_type": "Business"})
	for n in data.get("home_phones") or []:
		if n and str(n).strip():
			doc.append("phones", {"number": _cap(n), "phone_type": "Home"})
	mobile = data.get("mobile_phone")
	if mobile and str(mobile).strip():
		doc.append("phones", {"number": _cap(mobile), "phone_type": "Mobile"})

	doc.set("addresses", [])
	for kind, key in (("Business", "business_address"), ("Home", "home_address"), ("Other", "other_address")):
		a = data.get(key)
		if a and any(a.get(k) for k in ("street", "city", "state", "postal_code", "country_or_region")):
			doc.append("addresses", {
				"address_type": kind,
				"street": a.get("street"),
				"city": _cap(a.get("city")),
				"state": _cap(a.get("state")),
				"postal_code": _cap(a.get("postal_code")),
				"country": _cap(a.get("country_or_region")),
			})
