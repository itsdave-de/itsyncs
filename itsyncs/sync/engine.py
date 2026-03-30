import json

import frappe

from itsyncs.graph.client import get_graph_client
from itsyncs.graph.contacts import (
	create_contact,
	delete_contact,
	fetch_all_contacts,
	fetch_contact_delta,
	update_contact,
)
from itsyncs.graph.exchange import (
	create_mail_contact,
	delete_mail_contact,
	fetch_all_mail_contacts,
	update_mail_contact,
)
from itsyncs.graph.gal import fetch_all_gal_entries, fetch_gal_delta
from itsyncs.sync.matching import compute_field_hash, find_match, get_primary_email


def generate_preview(pair_name: str):
	"""Generate a sync preview for the given pair. Runs as a background job."""
	pair = frappe.get_doc("ITSync Pair", pair_name)
	source_conn = frappe.get_doc("ITSync Connector", pair.source)
	target_conn = frappe.get_doc("ITSync Connector", pair.target)

	source_tenant = frappe.get_doc("ITSync Tenant", source_conn.tenant)
	target_tenant = frappe.get_doc("ITSync Tenant", target_conn.tenant)

	source_client = get_graph_client(source_tenant)
	target_client = get_graph_client(target_tenant)

	# Fetch source contacts
	source_contacts = _fetch_source_contacts(source_client, source_conn)
	target_contacts = _fetch_target_contacts(target_client, target_conn, target_tenant)

	# Match contacts
	matched = 0
	to_create = 0

	for sc in source_contacts:
		match = find_match(sc, target_contacts, pair.matching_strategy, pair.fuzzy_threshold or 0.85)
		if match:
			matched += 1
		else:
			to_create += 1

	pair.db_set("preview_source_count", len(source_contacts))
	pair.db_set("preview_target_count", len(target_contacts))
	pair.db_set("preview_to_create", to_create)
	pair.db_set("preview_matched", matched)
	pair.db_set("preview_generated_at", frappe.utils.now_datetime())

	frappe.publish_realtime(
		"itsync_preview_complete",
		{"pair": pair_name, "source": len(source_contacts), "target": len(target_contacts), "to_create": to_create, "matched": matched},
		doctype="ITSync Pair",
		docname=pair_name,
	)


def run_sync(pair_name: str, sync_type: str = "Incremental"):
	"""Run a sync for the given pair. Runs as a background job."""
	pair = frappe.get_doc("ITSync Pair", pair_name)
	source_conn = frappe.get_doc("ITSync Connector", pair.source)
	target_conn = frappe.get_doc("ITSync Connector", pair.target)

	# Create log entry
	log = frappe.new_doc("ITSync Log")
	log.sync_pair = pair_name
	log.sync_type = sync_type
	log.started_at = frappe.utils.now_datetime()
	log.status = "Running"
	log.insert(ignore_permissions=True)
	frappe.db.commit()

	counts = {"created": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}
	error_details = []

	try:
		source_tenant = frappe.get_doc("ITSync Tenant", source_conn.tenant)
		target_tenant = frappe.get_doc("ITSync Tenant", target_conn.tenant)
		source_client = get_graph_client(source_tenant)
		target_client = get_graph_client(target_tenant)

		if sync_type == "Initial":
			_run_initial_sync(
				pair, source_conn, target_conn, source_client, target_client,
				target_tenant, counts, error_details
			)
			pair.db_set("initial_sync_complete", 1)
			pair.db_set("status", "Idle")
		else:
			_run_incremental_sync(
				pair, source_conn, target_conn, source_client, target_client,
				target_tenant, counts, error_details
			)
			pair.db_set("status", "Idle")

		log_status = "Success" if counts["errors"] == 0 else "Partial"
	except Exception as e:
		log_status = "Failed"
		error_details.append({"error": str(e), "type": "fatal"})
		pair.db_set("status", "Error")
		frappe.log_error(f"ITSync Error for {pair_name}", str(e))
	finally:
		log.db_set("status", log_status)
		log.db_set("completed_at", frappe.utils.now_datetime())
		log.db_set("created_count", counts["created"])
		log.db_set("updated_count", counts["updated"])
		log.db_set("deleted_count", counts["deleted"])
		log.db_set("skipped_count", counts["skipped"])
		log.db_set("error_count", counts["errors"])
		if error_details:
			log.db_set("details", json.dumps(error_details, ensure_ascii=False, indent=2))

		pair.db_set("last_run", frappe.utils.now_datetime())
		pair.db_set("last_run_log", log.name)
		frappe.db.commit()

		frappe.publish_realtime(
			"itsync_sync_complete",
			{"pair": pair_name, "status": log_status, "log": log.name, **counts},
			doctype="ITSync Pair",
			docname=pair_name,
		)


def _run_initial_sync(pair, source_conn, target_conn, source_client, target_client, target_tenant, counts, error_details):
	"""Full initial sync: fetch all from source, match against target, create missing."""
	source_contacts = _fetch_source_contacts(source_client, source_conn)
	target_contacts = _fetch_target_contacts(target_client, target_conn, target_tenant)

	target_is_gal = target_conn.connector_type == "GAL"
	target_folder = target_conn.contact_folder or None

	for sc in source_contacts:
		try:
			match = find_match(sc, target_contacts, pair.matching_strategy, pair.fuzzy_threshold or 0.85)

			if match:
				# Already exists in target - just create mapping
				_create_mapping(pair.name, sc, match["id"])
				counts["skipped"] += 1
			else:
				if target_is_gal:
					new_id = create_mail_contact(target_tenant, sc)
				else:
					new_id = create_contact(target_tenant, target_conn.email_address, sc, target_folder)
				_create_mapping(pair.name, sc, new_id)
				counts["created"] += 1

		except Exception as e:
			counts["errors"] += 1
			error_details.append({
				"contact": sc.get("display_name"),
				"email": get_primary_email(sc),
				"error": str(e),
			})

		# Commit periodically to avoid long transactions
		if (counts["created"] + counts["skipped"] + counts["errors"]) % 50 == 0:
			frappe.db.commit()

	# Store delta tokens for future incremental syncs (fresh client needed)
	fresh_source_client = get_graph_client(frappe.get_doc("ITSync Tenant", source_conn.tenant))
	_store_delta_tokens(source_conn, fresh_source_client)

	frappe.db.commit()


def _run_incremental_sync(pair, source_conn, target_conn, source_client, target_client, target_tenant, counts, error_details):
	"""Incremental sync using delta queries."""
	if source_conn.connector_type == "GAL":
		delta_data = json.loads(source_conn.delta_token or "{}") if source_conn.delta_token else {}
		changed, deleted_ids, new_token_users, new_token_org = fetch_gal_delta(
			source_client,
			source_conn.gal_include,
			delta_data.get("users"),
			delta_data.get("org"),
		)
		new_tokens = {}
		if new_token_users:
			new_tokens["users"] = new_token_users
		if new_token_org:
			new_tokens["org"] = new_token_org
		if new_tokens:
			source_conn.db_set("delta_token", json.dumps(new_tokens))
	else:
		source_folder = source_conn.contact_folder or None
		changed, _, deleted_ids, new_delta_token = fetch_contact_delta(
			source_client,
			source_conn.email_address,
			source_conn.delta_token,
			folder_id=source_folder,
		)
		if new_delta_token:
			source_conn.db_set("delta_token", new_delta_token)

	target_is_gal = target_conn.connector_type == "GAL"
	target_folder = target_conn.contact_folder or None

	# Process changes
	for contact in changed:
		try:
			mapping = frappe.db.get_value(
				"ITSync Mapping",
				{"sync_pair": pair.name, "source_id": contact["id"]},
				["name", "target_id", "field_hash"],
				as_dict=True,
			)

			new_hash = compute_field_hash(contact)

			if mapping:
				if mapping.field_hash != new_hash:
					if target_is_gal:
						update_mail_contact(target_tenant, mapping.target_id, contact)
					else:
						update_contact(target_tenant, target_conn.email_address, mapping.target_id, contact)
					frappe.db.set_value("ITSync Mapping", mapping.name, {
						"field_hash": new_hash,
						"last_synced": frappe.utils.now_datetime(),
						"status": "Synced",
						"display_name": contact.get("display_name"),
						"source_email": get_primary_email(contact),
					})
					counts["updated"] += 1
				else:
					counts["skipped"] += 1
			else:
				if target_is_gal:
					new_id = create_mail_contact(target_tenant, contact)
				else:
					new_id = create_contact(target_tenant, target_conn.email_address, contact, target_folder)
				_create_mapping(pair.name, contact, new_id)
				counts["created"] += 1

		except Exception as e:
			counts["errors"] += 1
			error_details.append({
				"contact": contact.get("display_name"),
				"email": get_primary_email(contact),
				"error": str(e),
			})

	# Process deletions
	if pair.on_delete == "Delete":
		for source_id in deleted_ids:
			try:
				mapping = frappe.db.get_value(
					"ITSync Mapping",
					{"sync_pair": pair.name, "source_id": source_id},
					["name", "target_id"],
					as_dict=True,
				)
				if mapping and mapping.target_id:
					if target_is_gal:
						delete_mail_contact(target_tenant, mapping.target_id)
					else:
						delete_contact(target_tenant, target_conn.email_address, mapping.target_id)
					frappe.delete_doc("ITSync Mapping", mapping.name, ignore_permissions=True)
					counts["deleted"] += 1
			except Exception as e:
				counts["errors"] += 1
				error_details.append({"source_id": source_id, "error": str(e), "action": "delete"})
	else:
		for source_id in deleted_ids:
			mapping_name = frappe.db.get_value(
				"ITSync Mapping",
				{"sync_pair": pair.name, "source_id": source_id},
				"name",
			)
			if mapping_name:
				frappe.db.set_value("ITSync Mapping", mapping_name, "status", "Orphaned")
				counts["skipped"] += 1

	frappe.db.commit()


def _fetch_source_contacts(client, source_conn):
	"""Fetch contacts from a source connector."""
	if source_conn.connector_type in ("Mailbox", "Shared Mailbox"):
		return fetch_all_contacts(client, source_conn.email_address, source_conn.contact_folder or None)
	elif source_conn.connector_type == "GAL":
		return fetch_all_gal_entries(client, source_conn.gal_include)
	return []


def _fetch_target_contacts(client, target_conn, target_tenant):
	"""Fetch contacts from a target connector."""
	if target_conn.connector_type in ("Mailbox", "Shared Mailbox"):
		return fetch_all_contacts(client, target_conn.email_address, target_conn.contact_folder or None)
	elif target_conn.connector_type == "GAL":
		return fetch_all_mail_contacts(target_tenant)
	return []


def _create_mapping(pair_name: str, source_contact: dict, target_id: str):
	"""Create an ITSync Mapping record."""
	mapping = frappe.new_doc("ITSync Mapping")
	mapping.sync_pair = pair_name
	mapping.source_id = source_contact["id"]
	mapping.target_id = target_id
	mapping.source_email = get_primary_email(source_contact)
	mapping.display_name = source_contact.get("display_name")
	mapping.field_hash = compute_field_hash(source_contact)
	mapping.status = "Synced"
	mapping.last_synced = frappe.utils.now_datetime()
	mapping.insert(ignore_permissions=True)


def _store_delta_tokens(source_conn, source_client):
	"""Initialize delta tokens after a full sync by doing an empty delta query."""
	if source_conn.connector_type in ("Mailbox", "Shared Mailbox"):
		_, _, _, new_token = fetch_contact_delta(
			source_client, source_conn.email_address, folder_id=source_conn.contact_folder or None
		)
		if new_token:
			source_conn.db_set("delta_token", new_token)
	elif source_conn.connector_type == "GAL":
		_, _, token_users, token_org = fetch_gal_delta(source_client, source_conn.gal_include)
		tokens = {}
		if token_users:
			tokens["users"] = token_users
		if token_org:
			tokens["org"] = token_org
		if tokens:
			source_conn.db_set("delta_token", json.dumps(tokens))
