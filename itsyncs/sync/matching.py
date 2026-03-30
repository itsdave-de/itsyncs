import hashlib
import json


def compute_field_hash(contact: dict) -> str:
	"""Compute a SHA-256 hash of the relevant contact fields for change detection."""
	normalized = {
		"display_name": (contact.get("display_name") or "").strip().lower(),
		"given_name": (contact.get("given_name") or "").strip().lower(),
		"surname": (contact.get("surname") or "").strip().lower(),
		"emails": sorted(
			[e["address"].strip().lower() for e in (contact.get("email_addresses") or []) if e.get("address")]
		),
		"business_phones": sorted(contact.get("business_phones") or []),
		"mobile_phone": (contact.get("mobile_phone") or "").strip(),
		"company_name": (contact.get("company_name") or "").strip().lower(),
		"job_title": (contact.get("job_title") or "").strip().lower(),
		"department": (contact.get("department") or "").strip().lower(),
		"office_location": (contact.get("office_location") or "").strip().lower(),
		"business_address": _normalize_address(contact.get("business_address")),
	}
	raw = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_primary_email(contact: dict) -> str | None:
	"""Extract the primary email address from a contact."""
	emails = contact.get("email_addresses") or []
	if emails:
		return emails[0]["address"].strip().lower()
	return None


def find_match(source_contact: dict, target_contacts: list[dict], strategy: str, threshold: float = 0.85) -> dict | None:
	"""Find a matching contact in the target list for the given source contact."""
	if strategy == "Email":
		return _match_by_email(source_contact, target_contacts)
	elif strategy == "Email + Name":
		return _match_by_email_and_name(source_contact, target_contacts)
	elif strategy == "Fuzzy":
		return _match_fuzzy(source_contact, target_contacts, threshold)
	return None


def _match_by_email(source: dict, targets: list[dict]) -> dict | None:
	source_email = get_primary_email(source)
	if not source_email:
		return None

	for target in targets:
		target_emails = [
			e["address"].strip().lower()
			for e in (target.get("email_addresses") or [])
			if e.get("address")
		]
		if source_email in target_emails:
			return target
	return None


def _match_by_email_and_name(source: dict, targets: list[dict]) -> dict | None:
	# Try email first
	match = _match_by_email(source, targets)
	if match:
		return match

	# Fall back to name + company
	source_name = (source.get("display_name") or "").strip().lower()
	source_company = (source.get("company_name") or "").strip().lower()
	if not source_name:
		return None

	for target in targets:
		target_name = (target.get("display_name") or "").strip().lower()
		target_company = (target.get("company_name") or "").strip().lower()
		if source_name == target_name and source_company == target_company:
			return target
	return None


def _match_fuzzy(source: dict, targets: list[dict], threshold: float) -> dict | None:
	# Try exact email first
	match = _match_by_email(source, targets)
	if match:
		return match

	# Fuzzy match on name
	from thefuzz import fuzz

	source_name = (source.get("display_name") or "").strip()
	if not source_name:
		return None

	best_match = None
	best_score = 0

	for target in targets:
		target_name = (target.get("display_name") or "").strip()
		if not target_name:
			continue

		score = fuzz.token_sort_ratio(source_name, target_name) / 100.0

		# Boost score if company matches
		source_company = (source.get("company_name") or "").strip().lower()
		target_company = (target.get("company_name") or "").strip().lower()
		if source_company and target_company and source_company == target_company:
			score = min(1.0, score + 0.1)

		if score > best_score and score >= threshold:
			best_score = score
			best_match = target

	return best_match


def _normalize_address(address: dict | None) -> dict:
	if not address:
		return {}
	return {
		"street": (address.get("street") or "").strip().lower(),
		"city": (address.get("city") or "").strip().lower(),
		"state": (address.get("state") or "").strip().lower(),
		"postal_code": (address.get("postal_code") or "").strip(),
		"country_or_region": (address.get("country_or_region") or "").strip().lower(),
	}
