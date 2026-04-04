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


def _get_client_for_connector(connector):
	"""Create the appropriate API client for a connector, or None for Sage SQL."""
	if connector.connector_type == "Sage SQL":
		return None
	tenant = frappe.get_doc("ITSync Tenant", connector.tenant)
	return get_graph_client(tenant)


def _get_tenant_for_connector(connector):
	"""Get the tenant doc for a connector, or None for Sage SQL."""
	if connector.connector_type == "Sage SQL":
		return None
	return frappe.get_doc("ITSync Tenant", connector.tenant)


def generate_preview(pair_name: str):
	"""Generate a sync preview for the given pair. Runs as a background job."""
	pair = frappe.get_doc("ITSync Pair", pair_name)
	source_conn = frappe.get_doc("ITSync Connector", pair.source)
	target_conn = frappe.get_doc("ITSync Connector", pair.target)

	source_client = _get_client_for_connector(source_conn)
	target_client = _get_client_for_connector(target_conn)
	target_tenant = _get_tenant_for_connector(target_conn)

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

	frappe.db.set_value("ITSync Pair", pair_name, {
		"preview_source_count": len(source_contacts),
		"preview_target_count": len(target_contacts),
		"preview_to_create": to_create,
		"preview_matched": matched,
		"preview_generated_at": frappe.utils.now_datetime(),
	})


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
		source_client = _get_client_for_connector(source_conn)
		target_client = _get_client_for_connector(target_conn)
		target_tenant = _get_tenant_for_connector(target_conn)

		if sync_type == "Initial":
			_run_initial_sync(
				pair, source_conn, target_conn, source_client, target_client,
				target_tenant, counts, error_details
			)
			pair_update = {"initial_sync_complete": 1, "status": "Idle"}
		else:
			_run_incremental_sync(
				pair, source_conn, target_conn, source_client, target_client,
				target_tenant, counts, error_details
			)
			pair_update = {"status": "Idle"}

		log_status = "Success" if counts["errors"] == 0 else "Partial"
	except Exception as e:
		log_status = "Failed"
		error_details.append({"error": str(e), "type": "fatal"})
		pair_update = {"status": "Error"}
		frappe.log_error(f"ITSync Error for {pair_name}", str(e))
	finally:
		# Batch-update log to avoid multiple notify_update calls
		log_values = {
			"status": log_status,
			"completed_at": frappe.utils.now_datetime(),
			"created_count": counts["created"],
			"updated_count": counts["updated"],
			"deleted_count": counts["deleted"],
			"skipped_count": counts["skipped"],
			"error_count": counts["errors"],
		}
		if error_details:
			log_values["details"] = json.dumps(error_details, ensure_ascii=False, indent=2)
		frappe.db.set_value("ITSync Log", log.name, log_values, update_modified=False)

		# Batch-update pair
		pair_update["last_run"] = frappe.utils.now_datetime()
		pair_update["last_run_log"] = log.name
		frappe.db.set_value("ITSync Pair", pair_name, pair_update, update_modified=False)
		frappe.db.commit()

		frappe.publish_realtime(
			"itsync_sync_complete",
			{"pair": pair_name, "status": log_status, "log": log.name, **counts},
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

	# Store delta tokens for future incremental syncs
	_store_delta_tokens(source_conn, source_client)

	frappe.db.commit()


def _run_incremental_sync(pair, source_conn, target_conn, source_client, target_client, target_tenant, counts, error_details):
	"""Incremental sync using delta queries."""
	if source_conn.connector_type == "Sage SQL":
		from itsyncs.sage.contacts import fetch_sage_delta

		last_rv = int(source_conn.delta_token or "0")
		changed, deleted_ids, new_rv = fetch_sage_delta(source_conn, last_rv)
		if new_rv > last_rv:
			source_conn.db_set("delta_token", str(new_rv))

	elif source_conn.connector_type == "GAL":
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
	if source_conn.connector_type == "Sage SQL":
		from itsyncs.sage.contacts import fetch_all_sage_contacts

		return fetch_all_sage_contacts(source_conn)
	elif source_conn.connector_type in ("Mailbox", "Shared Mailbox"):
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
	"""Initialize delta tokens after a full sync."""
	if source_conn.connector_type == "Sage SQL":
		from itsyncs.sage.client import get_max_rowversion

		max_rv = get_max_rowversion(source_conn)
		source_conn.db_set("delta_token", str(max_rv))
	elif source_conn.connector_type in ("Mailbox", "Shared Mailbox"):
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
