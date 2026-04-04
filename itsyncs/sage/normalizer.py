"""Contact normalizer for Sage Office Line → Exchange sync.

Takes raw Sage address/contact data and produces cleaned, normalized
contact records suitable for Exchange/Outlook import. Handles common
data quality issues found in Sage OLKFB databases without requiring
the customer to fix the source data.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FUNCTIONAL_NAMES: frozenset[str] = frozenset({
	"rechnungen", "rechnung", "rechnungsversand", "rechnunsversand",
	"e-rechnung", "x-rechnung",
	"info", "information",
	"avis", "avise", "zahlungsavis",
	"mahnungen", "mahnung",
	"buchhaltung", "kostenrechnung",
	"einkauf", "disposition", "versand", "lager",
	"zentrale", "empfang",
	"wiederkehrende prüfungen", "fällige sicherheitsprüfungen",
	"prüfungen/erinnerung", "prüfungen/erinnerungen soltau",
	"rg prüfung",
	"auftragsbearbeitung",
})

FUNCTIONAL_PATTERNS: list[re.Pattern[str]] = [
	re.compile(r"^rechnungen?\b", re.IGNORECASE),
	re.compile(r"^mahnungen?\b", re.IGNORECASE),
	re.compile(r"^avise?\b", re.IGNORECASE),
	re.compile(r"\bprüfung", re.IGNORECASE),
	re.compile(r"^mahnung\b", re.IGNORECASE),
]

VALID_ANREDE: dict[str, str] = {
	"firma": "Firma",
	"herr": "Herr",
	"herrn": "Herr",
	"frau": "Frau",
}

COMMON_TLDS = {"de", "com", "net", "org", "eu", "at", "ch", "info", "biz"}

MIN_PHONE_DIGITS = 6
MAX_PHONE_DIGITS = 15


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ContactType(enum.Enum):
	PERSON = "person"
	COMPANY_ONLY = "company_only"
	FUNCTIONAL = "functional"
	JUNK = "junk"


class IssueSeverity(enum.Enum):
	INFO = "info"
	WARNING = "warning"
	ERROR = "error"
	SKIP = "skip"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class RawContact:
	"""Raw record as read from Sage (KHKAdressen + KHKAnsprechpartner)."""

	adress_nr: int = 0
	mandant: int = 1
	name1: Optional[str] = None
	name2: Optional[str] = None
	anrede: Optional[str] = None
	strasse: Optional[str] = None
	plz: Optional[str] = None
	ort: Optional[str] = None
	land: Optional[str] = None
	telefon: Optional[str] = None
	telefax: Optional[str] = None
	mobilfunk: Optional[str] = None
	email: Optional[str] = None
	homepage: Optional[str] = None

	ansprech_nr: Optional[int] = None
	vorname: Optional[str] = None
	nachname: Optional[str] = None
	ap_titel: Optional[str] = None
	ap_anrede: Optional[str] = None
	ap_position: Optional[str] = None
	ap_abteilung: Optional[str] = None
	ap_telefon: Optional[str] = None
	ap_telefax: Optional[str] = None
	ap_mobilfunk: Optional[str] = None
	ap_email: Optional[str] = None


@dataclass
class NormalizedContact:
	"""Cleaned contact ready for Exchange/Outlook mapping."""

	sync_id: str = ""
	contact_type: ContactType = ContactType.JUNK

	company_name: str = ""
	company_name2: str = ""
	first_name: str = ""
	last_name: str = ""
	display_name: str = ""
	salutation: str = ""
	title: str = ""
	job_title: str = ""
	department: str = ""

	phone: str = ""
	phone_mobile: str = ""
	fax: str = ""
	email: str = ""
	email_secondary: str = ""
	homepage: str = ""

	street: str = ""
	postal_code: str = ""
	city: str = ""
	country: str = ""


@dataclass
class NormalizationIssue:
	severity: IssueSeverity
	field: str
	message: str
	original_value: str = ""
	corrected_value: str = ""


@dataclass
class NormalizationResult:
	contact: NormalizedContact
	issues: list[NormalizationIssue] = field(default_factory=list)
	skipped: bool = False


# ---------------------------------------------------------------------------
# Normalizer functions
# ---------------------------------------------------------------------------


def _strip(val: Optional[str]) -> str:
	return (val or "").strip()


def normalize_phone(raw: Optional[str], field_name: str = "phone") -> tuple[str, list[NormalizationIssue]]:
	issues: list[NormalizationIssue] = []
	original = _strip(raw)
	if not original:
		return "", issues

	number = re.sub(r"\s+", "", original)
	number = re.sub(r"\(0\)", "", number)
	number = number.replace("(", "").replace(")", "")
	number = number.replace("/", "").replace("-", "")

	if number.startswith("0049"):
		number = "+49" + number[4:]
	elif number.startswith("+"):
		pass
	elif number.startswith("0"):
		number = "+49" + number[1:]
	elif number.startswith("49") and len(number) > 10:
		number = "+49" + number[2:]

	digits_only = re.sub(r"[^0-9]", "", number)
	if len(digits_only) < MIN_PHONE_DIGITS:
		issues.append(NormalizationIssue(
			IssueSeverity.ERROR, field_name,
			f"Telefonnummer zu kurz ({len(digits_only)} Ziffern)",
			original, number,
		))
		return original, issues

	if len(digits_only) > MAX_PHONE_DIGITS:
		issues.append(NormalizationIssue(
			IssueSeverity.ERROR, field_name,
			f"Telefonnummer zu lang ({len(digits_only)} Ziffern)",
			original, number,
		))
		return original, issues

	number = "+" + digits_only

	if number != original.replace(" ", "").replace("-", "").replace("/", "").replace("(", "").replace(")", ""):
		issues.append(NormalizationIssue(
			IssueSeverity.INFO, field_name,
			"Telefonnummer normalisiert",
			original, number,
		))

	return number, issues


def normalize_email(raw: Optional[str], field_name: str = "email") -> tuple[str, str, list[NormalizationIssue]]:
	issues: list[NormalizationIssue] = []
	original = _strip(raw)
	if not original:
		return "", "", issues

	email = original

	if email != (raw or ""):
		issues.append(NormalizationIssue(
			IssueSeverity.INFO, field_name, "Leerzeichen entfernt", raw or "", email,
		))

	if "(at)" in email.lower():
		email = re.sub(r"\(at\)", "@", email, flags=re.IGNORECASE)
		issues.append(NormalizationIssue(
			IssueSeverity.WARNING, field_name, "(at) durch @ ersetzt", original, email,
		))

	comment_match = re.match(r"^(\S+@\S+)\s+-\s+.+", email)
	if comment_match:
		comment = email[comment_match.end(1):]
		email = comment_match.group(1)
		issues.append(NormalizationIssue(
			IssueSeverity.WARNING, field_name,
			f"Kommentar entfernt: '{comment.strip()}'",
			original, email,
		))

	secondary = ""
	tokens = email.split()
	if len(tokens) > 1:
		email_tokens = [t for t in tokens if "@" in t]
		if len(email_tokens) >= 2:
			email = email_tokens[0]
			secondary = email_tokens[1]
			issues.append(NormalizationIssue(
				IssueSeverity.WARNING, field_name,
				"Mehrere E-Mails in einem Feld, aufgeteilt",
				original, f"{email} | {secondary}",
			))
		elif len(email_tokens) == 1:
			email = email_tokens[0]

	for tld in COMMON_TLDS:
		pattern = f"-{tld}"
		if email.lower().endswith(pattern):
			email = email[: -len(pattern)] + f".{tld}"
			issues.append(NormalizationIssue(
				IssueSeverity.WARNING, field_name,
				f"Bindestrich vor TLD durch Punkt ersetzt (-.{tld})",
				original, email,
			))
			break

	email_regex = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
	if email and not email_regex.match(email):
		if "@" not in email:
			issues.append(NormalizationIssue(
				IssueSeverity.ERROR, field_name,
				"Kein @-Zeichen in E-Mail — manuelle Korrektur nötig",
				original, "",
			))
		elif not re.search(r"@.+\..+", email):
			issues.append(NormalizationIssue(
				IssueSeverity.ERROR, field_name,
				"Kein Punkt nach @ (fehlende Domain/TLD)",
				original, "",
			))
		else:
			issues.append(NormalizationIssue(
				IssueSeverity.ERROR, field_name, "E-Mail-Format ungültig", original, "",
			))

	return email, secondary, issues


def normalize_name(
	vorname: Optional[str],
	nachname: Optional[str],
	abteilung: Optional[str],
) -> tuple[str, str, str, ContactType, list[NormalizationIssue]]:
	issues: list[NormalizationIssue] = []
	first = _strip(vorname)
	last = _strip(nachname)
	dept = _strip(abteilung)

	dept_has_email = bool(dept and "@" in dept)
	if dept_has_email:
		issues.append(NormalizationIssue(
			IssueSeverity.WARNING, "abteilung", "E-Mail-Adresse im Abteilungsfeld", dept, "",
		))
		dept = ""

	if not first and not last:
		severity = IssueSeverity.SKIP
		if dept_has_email:
			issues.append(NormalizationIssue(
				severity, "name", "Kein Name, nur E-Mail im Abteilungsfeld — Funktionseintrag", "", "",
			))
			return "", "", dept, ContactType.FUNCTIONAL, issues
		else:
			issues.append(NormalizationIssue(
				severity, "name", "Kein Vor- oder Nachname vorhanden", "", "",
			))
			return "", "", dept, ContactType.JUNK, issues

	if first and last and first.lower() == last.lower():
		if _is_functional(first):
			issues.append(NormalizationIssue(
				IssueSeverity.SKIP, "name",
				f"Vorname=Nachname='{first}' — Funktionseintrag",
				f"{first} / {last}", "",
			))
			return "", "", dept, ContactType.FUNCTIONAL, issues
		else:
			issues.append(NormalizationIssue(
				IssueSeverity.WARNING, "name",
				f"Vorname und Nachname identisch: '{first}'",
				f"{first} / {last}", "",
			))

	if _is_functional(last) and not first:
		issues.append(NormalizationIssue(
			IssueSeverity.SKIP, "nachname",
			f"Nachname '{last}' ist ein Funktionsname",
			last, "",
		))
		return "", "", dept, ContactType.FUNCTIONAL, issues

	if _is_functional(last) and first and _is_functional(first):
		issues.append(NormalizationIssue(
			IssueSeverity.SKIP, "name",
			f"'{first} {last}' — Funktionseinträge",
			f"{first} / {last}", "",
		))
		return "", "", dept, ContactType.FUNCTIONAL, issues

	if first and not last:
		issues.append(NormalizationIssue(
			IssueSeverity.INFO, "name", "Nur Vorname vorhanden, kein Nachname", first, "",
		))

	return first, last, dept, ContactType.PERSON, issues


def _is_functional(name: str) -> bool:
	if not name:
		return False
	lower = name.lower().strip()
	if lower in FUNCTIONAL_NAMES:
		return True
	for pattern in FUNCTIONAL_PATTERNS:
		if pattern.search(lower):
			return True
	return False


def normalize_anrede(raw: Optional[str]) -> tuple[str, list[NormalizationIssue]]:
	issues: list[NormalizationIssue] = []
	original = _strip(raw)
	if not original:
		return "", issues

	lower = original.lower()
	if lower in VALID_ANREDE:
		return VALID_ANREDE[lower], issues

	if re.search(r"\d", original) or len(original.split()) > 2:
		issues.append(NormalizationIssue(
			IssueSeverity.WARNING, "anrede", "Ungültiger Anrede-Wert verworfen", original, "",
		))
		return "", issues

	issues.append(NormalizationIssue(
		IssueSeverity.INFO, "anrede", "Unbekannter Anrede-Wert beibehalten", original, original,
	))
	return original, issues


def normalize_address(
	strasse: Optional[str],
	plz: Optional[str],
	ort: Optional[str],
	land: Optional[str],
) -> tuple[str, str, str, str, list[NormalizationIssue]]:
	issues: list[NormalizationIssue] = []

	street = _strip(strasse)
	postal = _strip(plz)
	city = _strip(ort)
	country = _strip(land)

	if plz and plz != postal:
		issues.append(NormalizationIssue(
			IssueSeverity.INFO, "plz", "Leerzeichen in PLZ entfernt", plz, postal,
		))

	if not country:
		country = "DE"

	if postal and country == "DE" and not re.match(r"^\d{5}$", postal):
		issues.append(NormalizationIssue(
			IssueSeverity.WARNING, "plz",
			f"PLZ '{postal}' ist kein gültiges deutsches Format (5 Ziffern)",
			postal, postal,
		))

	if not street:
		issues.append(NormalizationIssue(IssueSeverity.INFO, "strasse", "Straße fehlt", "", ""))
	if not postal:
		issues.append(NormalizationIssue(IssueSeverity.INFO, "plz", "PLZ fehlt", "", ""))
	if not city:
		issues.append(NormalizationIssue(IssueSeverity.INFO, "ort", "Ort fehlt", "", ""))

	return street, postal, city, country, issues


def normalize_homepage(raw: Optional[str]) -> tuple[str, list[NormalizationIssue]]:
	issues: list[NormalizationIssue] = []
	original = _strip(raw)
	if not original:
		return "", issues

	if original.lower().startswith(("http://", "https://")):
		return original, issues

	if "." in original:
		url = f"https://{original}"
		issues.append(NormalizationIssue(
			IssueSeverity.INFO, "homepage", "https:// ergänzt", original, url,
		))
		return url, issues

	issues.append(NormalizationIssue(
		IssueSeverity.WARNING, "homepage", "Kein gültiges URL-Format", original, original,
	))
	return original, issues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_display_name(first: str, last: str, company: str, contact_type: ContactType) -> str:
	if contact_type == ContactType.COMPANY_ONLY:
		return company
	parts = [p for p in (first, last) if p]
	person = " ".join(parts)
	if person and company:
		return f"{person} ({company})"
	if person:
		return person
	return company


def build_sync_id(mandant: int, adress_nr: int, ansprech_nr: Optional[int]) -> str:
	ap = ansprech_nr if ansprech_nr is not None else 0
	return f"sage:{mandant}:{adress_nr}:{ap}"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def normalize_contact(raw: RawContact) -> NormalizationResult:
	issues: list[NormalizationIssue] = []

	first, last, dept, contact_type, name_issues = normalize_name(
		raw.vorname, raw.nachname, raw.ap_abteilung,
	)
	issues.extend(name_issues)

	if raw.ansprech_nr is None:
		contact_type = ContactType.COMPANY_ONLY

	skipped = contact_type in (ContactType.FUNCTIONAL, ContactType.JUNK)

	phone, ph_issues = normalize_phone(raw.ap_telefon or raw.telefon, "telefon")
	issues.extend(ph_issues)

	mobile, mob_issues = normalize_phone(raw.ap_mobilfunk or raw.mobilfunk, "mobilfunk")
	issues.extend(mob_issues)

	fax, fax_issues = normalize_phone(raw.ap_telefax or raw.telefax, "telefax")
	issues.extend(fax_issues)

	email_raw = raw.ap_email or raw.email
	email, email2, em_issues = normalize_email(email_raw, "email")
	issues.extend(em_issues)

	if not email and raw.ap_abteilung and "@" in (raw.ap_abteilung or ""):
		dept_email = _strip(raw.ap_abteilung)
		if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$", dept_email):
			email = dept_email

	anrede, anr_issues = normalize_anrede(raw.ap_anrede or raw.anrede)
	issues.extend(anr_issues)

	street, plz, city, country, addr_issues = normalize_address(
		raw.strasse, raw.plz, raw.ort, raw.land,
	)
	issues.extend(addr_issues)

	homepage, hp_issues = normalize_homepage(raw.homepage)
	issues.extend(hp_issues)

	company = _strip(raw.name1)
	company2 = _strip(raw.name2)
	display = build_display_name(first, last, company, contact_type)
	sync_id = build_sync_id(raw.mandant, raw.adress_nr, raw.ansprech_nr)

	contact = NormalizedContact(
		sync_id=sync_id,
		contact_type=contact_type,
		company_name=company,
		company_name2=company2,
		first_name=first,
		last_name=last,
		display_name=display,
		salutation=anrede,
		title=_strip(raw.ap_titel),
		job_title=_strip(raw.ap_position),
		department=dept,
		phone=phone,
		phone_mobile=mobile,
		fax=fax,
		email=email,
		email_secondary=email2,
		homepage=homepage,
		street=street,
		postal_code=plz,
		city=city,
		country=country,
	)

	return NormalizationResult(contact=contact, issues=issues, skipped=skipped)


def normalize_batch(
	contacts: list[RawContact],
) -> tuple[list[NormalizationResult], dict]:
	results = [normalize_contact(c) for c in contacts]
	summary = {
		"total": len(results),
		"synced": sum(1 for r in results if not r.skipped),
		"skipped": sum(1 for r in results if r.skipped),
		"by_type": {t.value: sum(1 for r in results if r.contact.contact_type == t) for t in ContactType},
		"issues_by_severity": {
			s.value: sum(1 for r in results for i in r.issues if i.severity == s)
			for s in IssueSeverity
		},
		"total_issues": sum(len(r.issues) for r in results),
	}
	return results, summary
