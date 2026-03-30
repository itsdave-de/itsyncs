import csv
import io
import json
import zipfile
from datetime import datetime

import frappe

from itsyncs.graph.client import get_graph_client
from itsyncs.graph.contacts import fetch_all_contacts
from itsyncs.graph.gal import fetch_all_gal_entries


def run_backup(backup_name: str):
	"""Run a contact backup for the given backup document."""
	backup = frappe.get_doc("ITSync Backup", backup_name)
	connector = frappe.get_doc("ITSync Connector", backup.connector)
	tenant = frappe.get_doc("ITSync Tenant", connector.tenant)

	try:
		client = get_graph_client(tenant)

		# Fetch contacts based on connector type
		if connector.connector_type in ("Mailbox", "Shared Mailbox"):
			contacts = fetch_all_contacts(client, connector.email_address, connector.contact_folder or None)
		elif connector.connector_type == "GAL":
			contacts = fetch_all_gal_entries(client, connector.gal_include)
		else:
			frappe.throw(f"Unsupported connector type: {connector.connector_type}")

		if not contacts:
			backup.db_set("status", "Success")
			backup.db_set("contact_count", 0)
			backup.db_set("completed_at", frappe.utils.now_datetime())
			frappe.db.commit()
			return

		# Generate all three formats
		vcf_data = _generate_vcf(contacts)
		csv_data = _generate_csv(contacts)
		json_data = _generate_json(contacts, connector)

		# Package as ZIP
		zip_buffer = io.BytesIO()
		timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
		base_name = _safe_filename(connector.title)

		with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
			zf.writestr(f"{base_name}_{timestamp}.vcf", vcf_data)
			zf.writestr(f"{base_name}_{timestamp}.csv", csv_data)
			zf.writestr(f"{base_name}_{timestamp}.json", json_data)

		zip_buffer.seek(0)
		zip_bytes = zip_buffer.getvalue()

		# Save as Frappe file
		zip_filename = f"{base_name}_{timestamp}.zip"
		file_doc = frappe.get_doc({
			"doctype": "File",
			"file_name": zip_filename,
			"content": zip_bytes,
			"attached_to_doctype": "ITSync Backup",
			"attached_to_name": backup_name,
			"is_private": 1,
		})
		file_doc.save(ignore_permissions=True)

		# Format file size
		size_bytes = len(zip_bytes)
		if size_bytes < 1024:
			size_str = f"{size_bytes} B"
		elif size_bytes < 1024 * 1024:
			size_str = f"{size_bytes / 1024:.1f} KB"
		else:
			size_str = f"{size_bytes / (1024 * 1024):.1f} MB"

		backup.db_set("status", "Success")
		backup.db_set("contact_count", len(contacts))
		backup.db_set("file_size", size_str)
		backup.db_set("backup_file", file_doc.file_url)
		backup.db_set("completed_at", frappe.utils.now_datetime())

	except Exception as e:
		backup.db_set("status", "Failed")
		backup.db_set("completed_at", frappe.utils.now_datetime())
		backup.db_set("error_details", str(e))
		frappe.log_error(f"ITSync Backup Error for {backup_name}", str(e))

	finally:
		frappe.db.commit()
		frappe.publish_realtime(
			"itsync_backup_complete",
			{
				"backup": backup_name,
				"status": backup.status,
				"contact_count": backup.contact_count or 0,
			},
			doctype="ITSync Backup",
			docname=backup_name,
		)


def _generate_vcf(contacts: list[dict]) -> str:
	"""Generate vCard 3.0 format for all contacts."""
	lines = []

	for c in contacts:
		lines.append("BEGIN:VCARD")
		lines.append("VERSION:3.0")

		# Name
		surname = _vcf_escape(c.get("surname") or "")
		given = _vcf_escape(c.get("given_name") or "")
		display = _vcf_escape(c.get("display_name") or "")
		lines.append(f"N:{surname};{given};;;")
		lines.append(f"FN:{display}")

		# Organization
		if c.get("company_name"):
			lines.append(f"ORG:{_vcf_escape(c['company_name'])}")
		if c.get("job_title"):
			lines.append(f"TITLE:{_vcf_escape(c['job_title'])}")
		if c.get("department"):
			lines.append(f"X-DEPARTMENT:{_vcf_escape(c['department'])}")

		# Email addresses
		for i, email in enumerate(c.get("email_addresses") or []):
			if email.get("address"):
				pref = ";PREF" if i == 0 else ""
				lines.append(f"EMAIL;TYPE=INTERNET{pref}:{email['address']}")

		# Phone numbers
		for phone in c.get("business_phones") or []:
			if phone:
				lines.append(f"TEL;TYPE=WORK,VOICE:{phone}")
		if c.get("mobile_phone"):
			lines.append(f"TEL;TYPE=CELL,VOICE:{c['mobile_phone']}")

		# Business address
		addr = c.get("business_address")
		if addr:
			street = _vcf_escape(addr.get("street") or "")
			city = _vcf_escape(addr.get("city") or "")
			state = _vcf_escape(addr.get("state") or "")
			postal = _vcf_escape(addr.get("postal_code") or "")
			country = _vcf_escape(addr.get("country_or_region") or "")
			lines.append(f"ADR;TYPE=WORK:;;{street};{city};{state};{postal};{country}")

		# Office location
		if c.get("office_location"):
			lines.append(f"X-OFFICE-LOCATION:{_vcf_escape(c['office_location'])}")

		# Notes
		if c.get("personal_notes"):
			lines.append(f"NOTE:{_vcf_escape(c['personal_notes'])}")

		lines.append("END:VCARD")
		lines.append("")

	return "\r\n".join(lines)


def _generate_csv(contacts: list[dict]) -> str:
	"""Generate CSV format for all contacts."""
	output = io.StringIO()

	fieldnames = [
		"Display Name",
		"Given Name",
		"Surname",
		"Email 1",
		"Email 2",
		"Email 3",
		"Business Phone 1",
		"Business Phone 2",
		"Mobile Phone",
		"Company Name",
		"Job Title",
		"Department",
		"Office Location",
		"Street",
		"City",
		"State",
		"Postal Code",
		"Country",
		"Notes",
		"Source ID",
	]

	writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
	writer.writeheader()

	for c in contacts:
		emails = c.get("email_addresses") or []
		phones = c.get("business_phones") or []
		addr = c.get("business_address") or {}

		row = {
			"Display Name": c.get("display_name") or "",
			"Given Name": c.get("given_name") or "",
			"Surname": c.get("surname") or "",
			"Email 1": emails[0]["address"] if len(emails) > 0 else "",
			"Email 2": emails[1]["address"] if len(emails) > 1 else "",
			"Email 3": emails[2]["address"] if len(emails) > 2 else "",
			"Business Phone 1": phones[0] if len(phones) > 0 else "",
			"Business Phone 2": phones[1] if len(phones) > 1 else "",
			"Mobile Phone": c.get("mobile_phone") or "",
			"Company Name": c.get("company_name") or "",
			"Job Title": c.get("job_title") or "",
			"Department": c.get("department") or "",
			"Office Location": c.get("office_location") or "",
			"Street": addr.get("street") or "",
			"City": addr.get("city") or "",
			"State": addr.get("state") or "",
			"Postal Code": addr.get("postal_code") or "",
			"Country": addr.get("country_or_region") or "",
			"Notes": c.get("personal_notes") or "",
			"Source ID": c.get("id") or "",
		}
		writer.writerow(row)

	return output.getvalue()


def _generate_json(contacts: list[dict], connector) -> str:
	"""Generate JSON format for all contacts with metadata."""
	export = {
		"metadata": {
			"connector": connector.title,
			"connector_type": connector.connector_type,
			"email_address": connector.email_address,
			"exported_at": frappe.utils.now_datetime().isoformat(),
			"contact_count": len(contacts),
			"format_version": "1.0",
		},
		"contacts": contacts,
	}
	return json.dumps(export, ensure_ascii=False, indent=2)


def _vcf_escape(value: str) -> str:
	"""Escape special characters for vCard format."""
	if not value:
		return ""
	value = value.replace("\\", "\\\\")
	value = value.replace(";", "\\;")
	value = value.replace(",", "\\,")
	value = value.replace("\n", "\\n")
	return value


def _safe_filename(name: str) -> str:
	"""Convert a name to a safe filename."""
	import re
	safe = re.sub(r"[^\w\s-]", "", name)
	safe = re.sub(r"\s+", "_", safe.strip())
	return safe or "backup"
