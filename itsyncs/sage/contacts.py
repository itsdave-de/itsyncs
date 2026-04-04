"""Sage Office Line contact fetching, normalization, and format conversion."""

from __future__ import annotations

import frappe

from itsyncs.sage.client import get_max_rowversion, get_sage_connection
from itsyncs.sage.normalizer import (
	ContactType,
	NormalizationResult,
	RawContact,
	normalize_contact,
)

# ---------------------------------------------------------------------------
# SQL Queries
# ---------------------------------------------------------------------------

# Ansprechpartner with parent Adresse data
_SQL_CONTACTS_WITH_AP = """
SELECT
	a.Adresse, a.Mandant, a.Name1, a.Name2, a.Anrede AS AdressAnrede,
	a.LieferStrasse, a.LieferPLZ, a.LieferOrt, a.LieferLand,
	a.Telefon AS AdressTelefon, a.Telefax AS AdressTelefax,
	a.Mobilfunk AS AdressMobilfunk,
	a.EMail AS AdressEMail, a.Homepage,
	ap.Nummer AS APNummer, ap.Vorname, ap.Nachname,
	ap.Titel AS APTitel, ap.Anrede AS APAnrede,
	ap.Position AS APPosition, ap.Abteilung AS APAbteilung,
	ap.Telefon AS APTelefon, ap.Telefax AS APTelefax,
	ap.Mobilfunk AS APMobilfunk, ap.EMail AS APEMail
FROM KHKAnsprechpartner ap
INNER JOIN KHKAdressen a ON ap.Adresse = a.Adresse AND ap.Mandant = a.Mandant
WHERE a.Mandant = %s AND a.Aktiv = -1
"""

# Adressen WITHOUT any Ansprechpartner (company-only)
_SQL_COMPANY_ONLY = """
SELECT
	a.Adresse, a.Mandant, a.Name1, a.Name2, a.Anrede AS AdressAnrede,
	a.LieferStrasse, a.LieferPLZ, a.LieferOrt, a.LieferLand,
	a.Telefon AS AdressTelefon, a.Telefax AS AdressTelefax,
	a.Mobilfunk AS AdressMobilfunk,
	a.EMail AS AdressEMail, a.Homepage,
	NULL AS APNummer, NULL AS Vorname, NULL AS Nachname,
	NULL AS APTitel, NULL AS APAnrede,
	NULL AS APPosition, NULL AS APAbteilung,
	NULL AS APTelefon, NULL AS APTelefax,
	NULL AS APMobilfunk, NULL AS APEMail
FROM KHKAdressen a
WHERE a.Mandant = %s AND a.Aktiv = -1
AND NOT EXISTS (
	SELECT 1 FROM KHKAnsprechpartner ap
	WHERE ap.Adresse = a.Adresse AND ap.Mandant = a.Mandant
)
"""

# Changed contacts since last rowversion (Ansprechpartner whose own or parent Adresse row changed)
_SQL_CHANGED_AP = """
SELECT
	a.Adresse, a.Mandant, a.Name1, a.Name2, a.Anrede AS AdressAnrede,
	a.LieferStrasse, a.LieferPLZ, a.LieferOrt, a.LieferLand,
	a.Telefon AS AdressTelefon, a.Telefax AS AdressTelefax,
	a.Mobilfunk AS AdressMobilfunk,
	a.EMail AS AdressEMail, a.Homepage,
	ap.Nummer AS APNummer, ap.Vorname, ap.Nachname,
	ap.Titel AS APTitel, ap.Anrede AS APAnrede,
	ap.Position AS APPosition, ap.Abteilung AS APAbteilung,
	ap.Telefon AS APTelefon, ap.Telefax AS APTelefax,
	ap.Mobilfunk AS APMobilfunk, ap.EMail AS APEMail
FROM KHKAnsprechpartner ap
INNER JOIN KHKAdressen a ON ap.Adresse = a.Adresse AND ap.Mandant = a.Mandant
WHERE a.Mandant = %s AND a.Aktiv = -1
AND (CONVERT(bigint, ap.Timestamp) > %s OR CONVERT(bigint, a.Timestamp) > %s)
"""

# Changed company-only contacts
_SQL_CHANGED_COMPANY = """
SELECT
	a.Adresse, a.Mandant, a.Name1, a.Name2, a.Anrede AS AdressAnrede,
	a.LieferStrasse, a.LieferPLZ, a.LieferOrt, a.LieferLand,
	a.Telefon AS AdressTelefon, a.Telefax AS AdressTelefax,
	a.Mobilfunk AS AdressMobilfunk,
	a.EMail AS AdressEMail, a.Homepage,
	NULL AS APNummer, NULL AS Vorname, NULL AS Nachname,
	NULL AS APTitel, NULL AS APAnrede,
	NULL AS APPosition, NULL AS APAbteilung,
	NULL AS APTelefon, NULL AS APTelefax,
	NULL AS APMobilfunk, NULL AS APEMail
FROM KHKAdressen a
WHERE a.Mandant = %s AND a.Aktiv = -1
AND CONVERT(bigint, a.Timestamp) > %s
AND NOT EXISTS (
	SELECT 1 FROM KHKAnsprechpartner ap
	WHERE ap.Adresse = a.Adresse AND ap.Mandant = a.Mandant
)
"""

# All active sync IDs (for delete detection)
_SQL_ALL_ACTIVE_IDS = """
SELECT
	'sage:' + CAST(a.Mandant AS VARCHAR) + ':' + CAST(a.Adresse AS VARCHAR)
		+ ':' + CAST(ap.Nummer AS VARCHAR) AS sync_id
FROM KHKAnsprechpartner ap
INNER JOIN KHKAdressen a ON ap.Adresse = a.Adresse AND ap.Mandant = a.Mandant
WHERE a.Mandant = %s AND a.Aktiv = -1
UNION ALL
SELECT
	'sage:' + CAST(a.Mandant AS VARCHAR) + ':' + CAST(a.Adresse AS VARCHAR)
		+ ':0' AS sync_id
FROM KHKAdressen a
WHERE a.Mandant = %s AND a.Aktiv = -1
AND NOT EXISTS (
	SELECT 1 FROM KHKAnsprechpartner ap
	WHERE ap.Adresse = a.Adresse AND ap.Mandant = a.Mandant
)
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_all_sage_contacts(connector) -> list[dict]:
	"""Fetch all active contacts from Sage, normalize, and return in Exchange format.

	Skipped contacts (functional names, junk) are logged but not returned.
	"""
	logger = frappe.logger("itsyncs.sage", allow_site=True)
	conn = get_sage_connection(connector)
	mandant = connector.sql_mandant

	try:
		cursor = conn.cursor(as_dict=True)

		# Fetch Ansprechpartner + Adresse
		cursor.execute(_SQL_CONTACTS_WITH_AP, (mandant,))
		ap_rows = cursor.fetchall()

		# Fetch company-only Adressen
		cursor.execute(_SQL_COMPANY_ONLY, (mandant,))
		company_rows = cursor.fetchall()

		all_rows = ap_rows + company_rows
	finally:
		conn.close()

	contacts = []
	skipped_count = 0
	norm_issues = []

	for row in all_rows:
		raw = _build_raw_contact(row, mandant)
		result = normalize_contact(raw)

		if result.issues:
			norm_issues.extend(_format_issues(result))

		if result.skipped:
			skipped_count += 1
			continue

		contacts.append(_to_exchange_format(result.contact))

	logger.info(
		f"Sage fetch complete: {len(all_rows)} raw rows, "
		f"{len(contacts)} contacts, {skipped_count} skipped, "
		f"{len(norm_issues)} normalization issues"
	)

	return contacts


def fetch_sage_delta(
	connector, last_rowversion: int,
) -> tuple[list[dict], list[str], int]:
	"""Fetch changed contacts since last_rowversion.

	Returns (changed_contacts, deleted_ids, new_max_rowversion).
	"""
	logger = frappe.logger("itsyncs.sage", allow_site=True)
	conn = get_sage_connection(connector)
	mandant = connector.sql_mandant

	try:
		cursor = conn.cursor(as_dict=True)

		# Changed Ansprechpartner (own row or parent Adresse changed)
		cursor.execute(_SQL_CHANGED_AP, (mandant, last_rowversion, last_rowversion))
		changed_ap_rows = cursor.fetchall()

		# Changed company-only Adressen
		cursor.execute(_SQL_CHANGED_COMPANY, (mandant, last_rowversion))
		changed_company_rows = cursor.fetchall()

		changed_rows = changed_ap_rows + changed_company_rows

		# All active IDs for delete detection
		cursor.execute(_SQL_ALL_ACTIVE_IDS, (mandant, mandant))
		active_ids = {row["sync_id"] for row in cursor.fetchall()}
	finally:
		conn.close()

	# Normalize changed contacts
	changed_contacts = []
	for row in changed_rows:
		raw = _build_raw_contact(row, mandant)
		result = normalize_contact(raw)
		if not result.skipped:
			changed_contacts.append(_to_exchange_format(result.contact))

	# Detect deletes: find mappings whose source_id is no longer in active set
	pair_name = _get_pair_name_for_connector(connector.name)
	deleted_ids = []
	if pair_name:
		existing_mappings = frappe.get_all(
			"ITSync Mapping",
			filters={"sync_pair": pair_name, "status": ["!=", "Orphaned"]},
			fields=["source_id"],
		)
		for mapping in existing_mappings:
			if mapping.source_id not in active_ids:
				deleted_ids.append(mapping.source_id)

	# New max rowversion
	new_rv = get_max_rowversion(connector)

	logger.info(
		f"Sage delta: {len(changed_contacts)} changed, "
		f"{len(deleted_ids)} deleted, rowversion {last_rowversion} → {new_rv}"
	)

	return changed_contacts, deleted_ids, new_rv


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_raw_contact(row: dict, mandant: int) -> RawContact:
	"""Convert a SQL result row to a RawContact dataclass."""
	return RawContact(
		adress_nr=row["Adresse"],
		mandant=mandant,
		name1=row.get("Name1"),
		name2=row.get("Name2"),
		anrede=row.get("AdressAnrede"),
		strasse=row.get("LieferStrasse"),
		plz=row.get("LieferPLZ"),
		ort=row.get("LieferOrt"),
		land=row.get("LieferLand"),
		telefon=row.get("AdressTelefon"),
		telefax=row.get("AdressTelefax"),
		mobilfunk=row.get("AdressMobilfunk"),
		email=row.get("AdressEMail"),
		homepage=row.get("Homepage"),
		ansprech_nr=row.get("APNummer"),
		vorname=row.get("Vorname"),
		nachname=row.get("Nachname"),
		ap_titel=row.get("APTitel"),
		ap_anrede=row.get("APAnrede"),
		ap_position=row.get("APPosition"),
		ap_abteilung=row.get("APAbteilung"),
		ap_telefon=row.get("APTelefon"),
		ap_telefax=row.get("APTelefax"),
		ap_mobilfunk=row.get("APMobilfunk"),
		ap_email=row.get("APEMail"),
	)


def _to_exchange_format(nc) -> dict:
	"""Convert a NormalizedContact to the standard itsyncs contact dict format."""
	email_addresses = []
	if nc.email:
		email_addresses.append({"address": nc.email, "name": nc.display_name})
	if nc.email_secondary:
		email_addresses.append({"address": nc.email_secondary, "name": ""})

	business_phones = []
	if nc.phone:
		business_phones.append(nc.phone)

	# Build personal_notes with homepage if available
	personal_notes = ""
	if nc.homepage:
		personal_notes = f"Homepage: {nc.homepage}"

	return {
		"id": nc.sync_id,
		"display_name": nc.display_name,
		"given_name": nc.first_name,
		"surname": nc.last_name,
		"email_addresses": email_addresses,
		"business_phones": business_phones,
		"mobile_phone": nc.phone_mobile or None,
		"company_name": nc.company_name,
		"job_title": nc.job_title or None,
		"department": nc.department or None,
		"office_location": None,
		"business_address": {
			"street": nc.street,
			"city": nc.city,
			"state": "",
			"postal_code": nc.postal_code,
			"country_or_region": nc.country,
		} if nc.street or nc.city or nc.postal_code else None,
		"personal_notes": personal_notes or None,
	}


def _format_issues(result: NormalizationResult) -> list[dict]:
	"""Format normalization issues for logging."""
	return [
		{
			"sync_id": result.contact.sync_id,
			"contact": result.contact.display_name,
			"severity": issue.severity.value,
			"field": issue.field,
			"message": issue.message,
			"original": issue.original_value,
			"corrected": issue.corrected_value,
		}
		for issue in result.issues
	]


def _get_pair_name_for_connector(connector_name: str) -> str | None:
	"""Find the sync pair that uses this connector as source."""
	return frappe.db.get_value(
		"ITSync Pair",
		{"source": connector_name},
		"name",
	)
