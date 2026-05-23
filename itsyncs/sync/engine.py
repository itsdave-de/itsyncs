import json
import time
import traceback as tb_module

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
	PermanentExchangeError,
	create_mail_contact,
	create_mail_contact_basic,
	delete_mail_contact,
	fetch_all_mail_contacts,
	set_mail_contact_rich_fields,
	update_mail_contact,
)
from itsyncs.graph.gal import fetch_all_gal_entries, fetch_gal_delta
from itsyncs.sync.matching import compute_field_hash, find_match, get_primary_email


DETAILS_MAX_LINES = 200
PROGRESS_EVERY = 50

# Delayed-batch for GAL initial sync: create N mail contacts in a tight loop,
# then wait for Exchange replication, then apply rich Set-Contact properties
# to the same N. Benchmarks (19/19 first-try-ok vs. 3–5 retries per contact
# in the serial pattern) validate this structure over a per-contact loop.
GAL_BATCH_CHUNK_SIZE = 50
GAL_BATCH_WAIT_SECONDS = 15


def _is_gal_eligible(contact):
	"""Check if a contact has the minimum data needed for a GAL MailContact.

	Returns (True, None) if eligible, or (False, reason_string) if not.
	Contacts without a valid SMTP email address cannot be created as Exchange
	MailContacts — they should be skipped rather than sent to the API (where
	they'd waste ~3 s on a guaranteed failure).
	"""
	email = get_primary_email(contact)
	if not email:
		return False, "no email address"
	if "@" not in email:
		return False, f"invalid email (not SMTP): {email}"
	return True, None


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


def _append_log_lines(log_name, lines):
	"""Append lines to ITSync Log.details, ring-buffered to last N lines."""
	if not lines:
		return
	existing = frappe.db.get_value("ITSync Log", log_name, "details") or ""
	combined = (existing.splitlines() if existing else []) + list(lines)
	if len(combined) > DETAILS_MAX_LINES:
		combined = [f"... (truncated to last {DETAILS_MAX_LINES} lines)"] + combined[-(DETAILS_MAX_LINES - 1):]
	frappe.db.set_value("ITSync Log", log_name, "details", "\n".join(combined), update_modified=False)


def _write_progress(log_name, current, total, phase):
	"""Persist progress fields on the log and publish a realtime event."""
	frappe.db.set_value(
		"ITSync Log",
		log_name,
		{"progress_current": current, "progress_total": total, "progress_phase": phase},
		update_modified=False,
	)


def _timestamp():
	return frappe.utils.now_datetime().strftime("%H:%M:%S")


def generate_preview(pair_name: str):
	"""Generate a sync preview for the given pair. Runs as a background job."""
	pair = frappe.get_doc("ITSync Pair", pair_name)
	source_conn = frappe.get_doc("ITSync Connector", pair.source)
	target_conn = frappe.get_doc("ITSync Connector", pair.target)

	source_client = _get_client_for_connector(source_conn)
	target_client = _get_client_for_connector(target_conn)
	target_tenant = _get_tenant_for_connector(target_conn)

	source_contacts = _fetch_source_contacts(source_client, source_conn)
	target_contacts = _fetch_target_contacts(target_client, target_conn, target_tenant)

	matched = 0
	to_create = 0

	for sc in source_contacts:
		match = find_match(sc, target_contacts, pair.matching_strategy, pair.fuzzy_threshold or 0.85)
		if match:
			matched += 1
		else:
			to_create += 1

	# update_modified=False: the Preview only populates internal counters,
	# bumping the doc's modified timestamp would invalidate any open form in
	# the UI (Frappe's optimistic concurrency check), causing "Document has
	# been modified after you have opened it" errors when the user interacts
	# with the form next.
	frappe.db.set_value("ITSync Pair", pair_name, {
		"preview_source_count": len(source_contacts),
		"preview_target_count": len(target_contacts),
		"preview_to_create": to_create,
		"preview_matched": matched,
		"preview_generated_at": frappe.utils.now_datetime(),
	}, update_modified=False)


def run_sync(pair_name: str, sync_type: str = "Incremental", log_name: str | None = None):
	"""Run a sync for the given pair. Runs as a background job.

	If log_name is provided (preferred), the caller has already created the log entry
	and the same log_name has been passed to the on_failure callback, so a work-horse
	kill can still update it. If not provided, a new log is created here.
	"""
	pair = frappe.get_doc("ITSync Pair", pair_name)
	source_conn = frappe.get_doc("ITSync Connector", pair.source)
	target_conn = frappe.get_doc("ITSync Connector", pair.target)

	if log_name:
		log = frappe.get_doc("ITSync Log", log_name)
	else:
		log = frappe.new_doc("ITSync Log")
		log.sync_pair = pair_name
		log.sync_type = sync_type
		log.started_at = frappe.utils.now_datetime()
		log.status = "Running"
		log.insert(ignore_permissions=True)

	frappe.db.set_value("ITSync Pair", pair_name, "last_run_log", log.name, update_modified=False)
	_write_progress(log.name, 0, 0, "Starting")
	_append_log_lines(log.name, [f"[{_timestamp()}] Starting {sync_type} sync"])
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
				target_tenant, counts, error_details, log.name,
			)
			pair_update = {"initial_sync_complete": 1, "status": "Idle"}
		else:
			_run_incremental_sync(
				pair, source_conn, target_conn, source_client, target_client,
				target_tenant, counts, error_details, log.name,
			)
			pair_update = {"status": "Idle"}

		log_status = "Success" if counts["errors"] == 0 else "Partial"
	except Exception as e:
		log_status = "Failed"
		error_details.append({"error": str(e), "type": "fatal", "traceback": tb_module.format_exc()})
		pair_update = {"status": "Error"}
		_append_log_lines(log.name, [f"[{_timestamp()}] FATAL: {e}"])
		frappe.log_error(f"ITSync Error for {pair_name}", str(e))
	finally:
		log_values = {
			"status": log_status,
			"completed_at": frappe.utils.now_datetime(),
			"created_count": counts["created"],
			"updated_count": counts["updated"],
			"deleted_count": counts["deleted"],
			"skipped_count": counts["skipped"],
			"error_count": counts["errors"],
			"progress_phase": f"Done ({log_status})",
		}
		if error_details:
			current_details = frappe.db.get_value("ITSync Log", log.name, "details") or ""
			error_block = "\n\n--- ERRORS ---\n" + json.dumps(error_details, ensure_ascii=False, indent=2)
			log_values["details"] = (current_details + error_block)[-60000:]
		frappe.db.set_value("ITSync Log", log.name, log_values, update_modified=False)

		pair_update["last_run"] = frappe.utils.now_datetime()
		pair_update["last_run_log"] = log.name
		pair_update["current_job_id"] = ""
		frappe.db.set_value("ITSync Pair", pair_name, pair_update, update_modified=False)

		source_mapping_count = frappe.db.count("ITSync Mapping", {"sync_pair": pair_name, "status": "Synced"})
		frappe.db.set_value("ITSync Connector", source_conn.name, "contact_count", source_mapping_count, update_modified=False)
		frappe.db.set_value("ITSync Connector", target_conn.name, "contact_count", source_mapping_count, update_modified=False)

		frappe.db.commit()

		frappe.publish_realtime(
			"itsync_sync_complete",
			{"pair": pair_name, "status": log_status, "log": log.name, **counts},
		)


def mark_sync_failed(job, connection, type_, value, traceback):
	"""RQ on_failure callback. Runs in the worker process (possibly the parent on SIGKILL).

	Must initialize Frappe itself because the work-horse that usually does that may be dead.
	This is best-effort: the scheduler watchdog is the reliable backstop.
	"""
	import os

	site = None
	pair_name = None
	log_name = None
	try:
		outer = job.kwargs or {}
		site = outer.get("site")
		inner = outer.get("kwargs") or {}
		pair_name = inner.get("pair_name")
		log_name = inner.get("log_name")
	except Exception:
		return

	if not (site and pair_name):
		return

	owns_init = False
	try:
		if not getattr(frappe.local, "conf", None):
			frappe.init(site=site, force=True)
			frappe.connect()
			owns_init = True

		exc_string = ""
		try:
			exc_string = "".join(tb_module.format_exception(type_, value, traceback))
		except Exception:
			exc_string = f"{type_.__name__ if type_ else 'Error'}: {value}"

		line = f"[{_timestamp()}] JOB FAILED: {type_.__name__ if type_ else 'Killed'}: {value or 'work-horse terminated'}"

		if log_name and frappe.db.exists("ITSync Log", log_name):
			current = frappe.db.get_value("ITSync Log", log_name, "details") or ""
			new_details = (current + "\n" + line + "\n\n--- TRACEBACK ---\n" + exc_string)[-60000:]
			frappe.db.set_value(
				"ITSync Log",
				log_name,
				{
					"status": "Failed",
					"completed_at": frappe.utils.now_datetime(),
					"details": new_details,
					"progress_phase": "Failed (job killed)",
				},
				update_modified=False,
			)

		if frappe.db.exists("ITSync Pair", pair_name):
			frappe.db.set_value(
				"ITSync Pair",
				pair_name,
				{
					"status": "Error",
					"current_job_id": "",
					"last_run": frappe.utils.now_datetime(),
					"last_run_log": log_name or frappe.db.get_value("ITSync Pair", pair_name, "last_run_log"),
				},
				update_modified=False,
			)

		frappe.db.commit()

		frappe.publish_realtime(
			"itsync_sync_complete",
			{"pair": pair_name, "status": "Failed", "log": log_name, "reason": "on_failure"},
		)
	except Exception:
		# Silent: watchdog will pick it up
		try:
			if frappe.db:
				frappe.db.rollback()
		except Exception:
			pass
	finally:
		if owns_init:
			try:
				frappe.destroy()
			except Exception:
				pass


def _run_initial_sync(pair, source_conn, target_conn, source_client, target_client, target_tenant, counts, error_details, log_name):
	"""Full initial sync: fetch source + target, match, create missing.

	For GAL targets uses a delayed-batch pattern: for each chunk of
	GAL_BATCH_CHUNK_SIZE contacts we (A) create all via New-MailContact,
	(W) wait GAL_BATCH_WAIT_SECONDS for Exchange replication, (B) apply rich
	properties via Set-Contact on each. The old per-contact loop called
	Set-Contact right after New-MailContact and hit NotFound retries for up
	to 75 s per contact; the chunk pattern drops that to ~4.5 s/contact.

	For Mailbox targets (Graph /contacts) the old per-contact pattern is
	fine because create_contact() accepts all fields in a single call.
	"""
	_write_progress(log_name, 0, 0, "Fetching source contacts")
	frappe.db.commit()
	source_contacts = _fetch_source_contacts(source_client, source_conn)

	_write_progress(log_name, 0, 0, "Fetching target contacts")
	frappe.db.commit()
	target_contacts = _fetch_target_contacts(target_client, target_conn, target_tenant)

	total = len(source_contacts)
	_append_log_lines(log_name, [
		f"[{_timestamp()}] Fetched {total} source contacts, {len(target_contacts)} target contacts",
	])
	frappe.db.commit()

	# Step 1: split into matches vs. to_create (fast, no network)
	_write_progress(log_name, 0, total, "Matching source against target")
	frappe.db.commit()
	matches = []     # [(source_contact, target_id), …]
	to_create = []   # [source_contact, …]
	match_errors = 0
	for sc in source_contacts:
		try:
			m = find_match(sc, target_contacts, pair.matching_strategy, pair.fuzzy_threshold or 0.85)
		except Exception as e:
			counts["errors"] += 1
			match_errors += 1
			error_details.append({
				"contact": sc.get("display_name"),
				"email": get_primary_email(sc),
				"stage": "match",
				"error": str(e),
			})
			continue
		if m:
			matches.append((sc, m["id"]))
		else:
			to_create.append(sc)

	# Pre-filter: skip contacts that can't become GAL entries (no email, non-SMTP)
	target_is_gal = target_conn.connector_type == "GAL"
	ineligible_count = 0
	if target_is_gal and to_create:
		eligible = []
		for sc in to_create:
			ok, reason = _is_gal_eligible(sc)
			if ok:
				eligible.append(sc)
			else:
				ineligible_count += 1
				counts["skipped"] += 1
		to_create = eligible

	_append_log_lines(log_name, [
		f"[{_timestamp()}] Split: {len(matches)} already matched, {len(to_create)} to create"
		+ (f", {ineligible_count} skipped (no valid email)" if ineligible_count else "")
		+ (f", {match_errors} match errors" if match_errors else ""),
	])
	frappe.db.commit()

	# Step 2: record mappings for all matches (DB only, very fast)
	_write_progress(log_name, 0, total, f"Recording {len(matches)} existing matches")
	frappe.db.commit()
	pending_lines = []
	for i, (sc, tid) in enumerate(matches, start=1):
		display = sc.get("display_name") or get_primary_email(sc) or "?"
		try:
			_create_mapping(pair.name, sc, tid)
			counts["skipped"] += 1
			pending_lines.append(f"[{_timestamp()}] skipped (already matched): {display}")
		except Exception as e:
			counts["errors"] += 1
			error_details.append({
				"contact": sc.get("display_name"),
				"email": get_primary_email(sc),
				"stage": "mapping",
				"error": str(e),
			})
			pending_lines.append(f"[{_timestamp()}] ERROR mapping {display}: {e}")
		if i % PROGRESS_EVERY == 0:
			_append_log_lines(log_name, pending_lines)
			pending_lines = []
			_write_progress(log_name, i, total, f"Recording existing matches ({i}/{len(matches)})")
			frappe.db.commit()
	if pending_lines:
		_append_log_lines(log_name, pending_lines)
	processed = len(matches)
	frappe.db.commit()

	# Step 3: create missing contacts
	if target_is_gal:
		_run_initial_gal_batched(
			pair, target_tenant, to_create, counts, error_details, log_name,
			total, processed,
		)
	else:
		target_folder = target_conn.contact_folder or None
		pending_lines = []
		for sc in to_create:
			processed += 1
			display = sc.get("display_name") or get_primary_email(sc) or "?"
			try:
				new_id = create_contact(target_tenant, target_conn.email_address, sc, target_folder)
				_create_mapping(pair.name, sc, new_id)
				counts["created"] += 1
				pending_lines.append(f"[{_timestamp()}] created: {display}")
			except Exception as e:
				counts["errors"] += 1
				error_details.append({
					"contact": sc.get("display_name"),
					"email": get_primary_email(sc),
					"error": str(e),
				})
				pending_lines.append(f"[{_timestamp()}] ERROR for {display}: {e}")

			if processed % PROGRESS_EVERY == 0:
				_append_log_lines(log_name, pending_lines)
				pending_lines = []
				_write_progress(
					log_name, processed, total,
					f"Creating contacts ({processed}/{total}) — {counts['created']} created, {counts['errors']} errors",
				)
				frappe.publish_realtime("itsync_sync_progress",
					{"pair": pair.name, "log": log_name, "current": processed, "total": total, **counts})
				frappe.db.commit()

		if pending_lines:
			_append_log_lines(log_name, pending_lines)

	_write_progress(log_name, total, total, "Storing delta tokens")
	frappe.db.commit()

	_store_delta_tokens(source_conn, source_client)
	frappe.db.commit()


def _run_initial_gal_batched(pair, tenant, to_create, counts, error_details, log_name, total, processed_start):
	"""Delayed-batch create+set for GAL targets.

	For each chunk:
	  A. New-MailContact for every entry in the chunk (tight loop)
	  W. sleep GAL_BATCH_WAIT_SECONDS so Exchange replicates
	  B. Set-Contact (rich fields) on each created entry

	Failures in B leave the contact in the GAL with mapping but missing rich
	fields — the next incremental sync will pick those up.
	"""
	if not to_create:
		return

	processed = processed_start
	chunks = [to_create[i:i + GAL_BATCH_CHUNK_SIZE] for i in range(0, len(to_create), GAL_BATCH_CHUNK_SIZE)]

	for chunk_idx, chunk in enumerate(chunks, start=1):
		chunk_label = f"chunk {chunk_idx}/{len(chunks)}"

		# ----- Phase A: create -----
		_write_progress(log_name, processed, total,
			f"GAL Phase A — creating {chunk_label} ({len(chunk)} contacts)")
		frappe.db.commit()
		created_in_chunk = []   # [(source_contact, alias), …]
		pending_lines = []
		for sc in chunk:
			processed += 1
			display = sc.get("display_name") or get_primary_email(sc) or "?"
			try:
				alias = create_mail_contact_basic(tenant, sc)
				_create_mapping(pair.name, sc, alias)
				counts["created"] += 1
				created_in_chunk.append((sc, alias))
				pending_lines.append(f"[{_timestamp()}] created: {display}")
			except PermanentExchangeError as e:
				# Permanent conflict — record it as a Conflict mapping so we
				# stop retrying on every subsequent run. Counts as skipped
				# (no error) since this is data state, not a sync failure.
				_create_conflict_mapping(pair.name, sc, e.kind, e.detail)
				counts["skipped"] += 1
				pending_lines.append(
					f"[{_timestamp()}] skipped (conflict — {e.kind}): {display} — {e.detail}"
				)
			except Exception as e:
				counts["errors"] += 1
				error_details.append({
					"contact": sc.get("display_name"),
					"email": get_primary_email(sc),
					"stage": "create",
					"error": str(e),
				})
				pending_lines.append(f"[{_timestamp()}] ERROR creating {display}: {e}")
		_append_log_lines(log_name, pending_lines)
		frappe.db.commit()

		if not created_in_chunk:
			continue  # nothing to set — whole chunk failed

		# ----- Phase W: wait for Exchange replication -----
		_write_progress(log_name, processed, total,
			f"GAL Phase W — waiting {GAL_BATCH_WAIT_SECONDS} s for replication ({chunk_label})")
		frappe.db.commit()
		time.sleep(GAL_BATCH_WAIT_SECONDS)

		# ----- Phase B: apply rich fields -----
		_write_progress(log_name, processed, total,
			f"GAL Phase B — setting rich fields {chunk_label} ({len(created_in_chunk)} contacts)")
		frappe.db.commit()
		pending_lines = []
		rich_ok = 0
		rich_deferred = 0
		for sc, alias in created_in_chunk:
			display = sc.get("display_name") or get_primary_email(sc) or "?"
			email = get_primary_email(sc)
			try:
				# Use email as Identity — more reliable than Alias for special chars
				set_mail_contact_rich_fields(tenant, email or alias, sc)
				rich_ok += 1
			except Exception as e:
				# Non-fatal: mapping exists, contact is in the GAL, rich fields
				# will be applied on the next incremental sync when the field_hash
				# mismatch is detected. Log for visibility but don't count as error.
				rich_deferred += 1
				pending_lines.append(f"[{_timestamp()}] rich fields deferred for {display}: {str(e)[:150]}")
		_append_log_lines(log_name, pending_lines + [
			f"[{_timestamp()}] {chunk_label} done: {rich_ok} rich fields set, {rich_deferred} deferred",
		])
		frappe.publish_realtime("itsync_sync_progress",
			{"pair": pair.name, "log": log_name, "current": processed, "total": total, **counts})
		frappe.db.commit()


def _reconcile_unmapped(pair, source_conn, target_conn, source_client, target_client, target_tenant, counts, error_details, log_name):
	"""Find source contacts without a mapping and create them in the target.

	Runs at the start of every incremental sync. Catches:
	  - Transient failures from the initial sync (Exchange 500s, timeouts)
	  - Contacts added to the source between syncs
	  - Contacts whose data was corrected (e.g. email added) since last run

	Cost when everything is clean: one source-fetch (needed anyway for delta) +
	one DB query on ITSync Mapping ≈ 0–2 s. Only triggers Graph API calls when
	there are actual unmapped contacts with valid emails.
	"""
	_write_progress(log_name, 0, 0, "Reconcile: checking for unmapped contacts")
	frappe.db.commit()

	source_contacts = _fetch_source_contacts(source_client, source_conn)
	mapped_ids = set(frappe.get_all(
		"ITSync Mapping",
		filters={"sync_pair": pair.name},
		fields=["source_id"],
		pluck="source_id",
	))

	unmapped = [c for c in source_contacts if c.get("id") not in mapped_ids]

	if not unmapped:
		_append_log_lines(log_name, [
			f"[{_timestamp()}] Reconcile: all {len(source_contacts)} source contacts mapped — nothing to do",
		])
		frappe.db.commit()
		return

	target_is_gal = target_conn.connector_type == "GAL"

	# Pre-filter for GAL eligibility
	skipped_ineligible = 0
	if target_is_gal:
		eligible = []
		for c in unmapped:
			ok, reason = _is_gal_eligible(c)
			if ok:
				eligible.append(c)
			else:
				skipped_ineligible += 1
				counts["skipped"] += 1
		unmapped = eligible

	if not unmapped:
		_append_log_lines(log_name, [
			f"[{_timestamp()}] Reconcile: no eligible unmapped contacts"
			+ (f" ({skipped_ineligible} skipped, no valid email)" if skipped_ineligible else ""),
		])
		frappe.db.commit()
		return

	# Match unmapped contacts against the target before attempting to create.
	# This catches contacts that ARE in the GAL but lost their mapping (e.g.
	# after a duplicate-mapping cleanup) — they need a mapping, not a create.
	_write_progress(log_name, 0, 0, "Reconcile: matching against target")
	frappe.db.commit()
	target_client = _get_client_for_connector(target_conn)
	target_contacts = _fetch_target_contacts(target_client, target_conn, target_tenant)

	already_in_target = []
	to_create = []
	for c in unmapped:
		try:
			m = find_match(c, target_contacts, pair.matching_strategy, pair.fuzzy_threshold or 0.85)
		except Exception:
			m = None
		if m:
			already_in_target.append((c, m["id"]))
		else:
			to_create.append(c)

	_append_log_lines(log_name, [
		f"[{_timestamp()}] Reconcile: {len(unmapped)} eligible unmapped"
		+ (f", {skipped_ineligible} skipped (no valid email)" if skipped_ineligible else "")
		+ f" → {len(already_in_target)} matched in target (mapping only)"
		+ f", {len(to_create)} to create",
	])
	frappe.db.commit()

	# Record mappings for contacts already in the target (no API call needed)
	for c, tid in already_in_target:
		_create_mapping(pair.name, c, tid)
		counts["skipped"] += 1
	if already_in_target:
		_append_log_lines(log_name, [
			f"[{_timestamp()}] Reconcile: {len(already_in_target)} mappings restored (contacts already in target)",
		])
		frappe.db.commit()

	# Create contacts that truly don't exist in the target yet
	if to_create:
		if target_is_gal:
			_run_initial_gal_batched(
				pair, target_tenant, to_create,
				counts, error_details, log_name,
				total=len(to_create), processed_start=0,
			)
		else:
			target_folder = target_conn.contact_folder or None
			for c in to_create:
				display = c.get("display_name") or get_primary_email(c) or "?"
				try:
					new_id = create_contact(target_tenant, target_conn.email_address, c, target_folder)
					_create_mapping(pair.name, c, new_id)
					counts["created"] += 1
				except Exception as e:
					counts["errors"] += 1
					error_details.append({
						"contact": c.get("display_name"),
						"email": get_primary_email(c),
						"stage": "reconcile",
						"error": str(e),
					})

	_append_log_lines(log_name, [
		f"[{_timestamp()}] Reconcile done: {len(already_in_target)} restored, {counts['created']} created, {counts['errors']} errors",
	])
	frappe.db.commit()


def _run_incremental_sync(pair, source_conn, target_conn, source_client, target_client, target_tenant, counts, error_details, log_name):
	"""Incremental sync using delta queries.

	Starts with a reconciliation step that catches up any source contacts that
	are not yet mapped (failed during initial sync, added between syncs, or
	whose data was corrected since the last run). Then processes the normal
	delta (changed + deleted contacts).
	"""
	# Step 0: Reconcile unmapped source contacts
	_reconcile_unmapped(pair, source_conn, target_conn, source_client,
						target_client, target_tenant, counts, error_details, log_name)

	# Step 1: Delta sync
	_write_progress(log_name, 0, 0, "Fetching delta from source")
	frappe.db.commit()

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

	total = len(changed) + len(deleted_ids)
	_append_log_lines(log_name, [
		f"[{_timestamp()}] Delta: {len(changed)} changed, {len(deleted_ids)} deleted",
	])
	_write_progress(log_name, 0, total, "Applying changes")
	frappe.db.commit()

	pending_lines = []
	processed = 0

	for contact in changed:
		processed += 1
		display = contact.get("display_name") or get_primary_email(contact) or "?"
		try:
			mapping = _find_mapping(
				pair.name, contact["id"], ["name", "target_id", "field_hash", "status"],
			)

			new_hash = compute_field_hash(contact)

			if mapping:
				if mapping.status == "Conflict":
					# Known permanent conflict — don't attempt update.
					# Refresh the hash so we don't re-process unchanged conflicts forever.
					if mapping.field_hash != new_hash:
						frappe.db.set_value("ITSync Mapping", mapping.name, {
							"field_hash": new_hash,
							"last_synced": frappe.utils.now_datetime(),
							"display_name": contact.get("display_name"),
							"source_email": get_primary_email(contact),
						}, update_modified=False)
					counts["skipped"] += 1
					pending_lines.append(
						f"[{_timestamp()}] skipped (existing conflict): {display}"
					)
				elif mapping.field_hash != new_hash:
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
					pending_lines.append(f"[{_timestamp()}] updated: {display}")
				else:
					counts["skipped"] += 1
			else:
				try:
					if target_is_gal:
						new_id = create_mail_contact(target_tenant, contact)
					else:
						new_id = create_contact(target_tenant, target_conn.email_address, contact, target_folder)
					_create_mapping(pair.name, contact, new_id)
					counts["created"] += 1
					pending_lines.append(f"[{_timestamp()}] created: {display}")
				except PermanentExchangeError as e:
					_create_conflict_mapping(pair.name, contact, e.kind, e.detail)
					counts["skipped"] += 1
					pending_lines.append(
						f"[{_timestamp()}] skipped (conflict — {e.kind}): {display} — {e.detail}"
					)

		except Exception as e:
			counts["errors"] += 1
			error_details.append({
				"contact": contact.get("display_name"),
				"email": get_primary_email(contact),
				"error": str(e),
			})
			pending_lines.append(f"[{_timestamp()}] ERROR for {display}: {e}")

		if processed % PROGRESS_EVERY == 0:
			_append_log_lines(log_name, pending_lines)
			pending_lines = []
			_write_progress(log_name, processed, total, f"Applying changes ({processed}/{total})")
			frappe.publish_realtime(
				"itsync_sync_progress",
				{"pair": pair.name, "log": log_name, "current": processed, "total": total, **counts},
			)
			frappe.db.commit()

	if pending_lines:
		_append_log_lines(log_name, pending_lines)
		pending_lines = []

	if pair.on_delete == "Delete":
		for source_id in deleted_ids:
			processed += 1
			try:
				mapping = _find_mapping(pair.name, source_id, ["name", "target_id", "status"])
				if not mapping:
					continue
				if mapping.status == "Conflict":
					# Target was never under our control — just drop the mapping row.
					frappe.delete_doc("ITSync Mapping", mapping.name, ignore_permissions=True)
					counts["skipped"] += 1
					pending_lines.append(
						f"[{_timestamp()}] removed conflict mapping for source_id {source_id} (source deleted)"
					)
				elif mapping.target_id:
					if target_is_gal:
						delete_mail_contact(target_tenant, mapping.target_id)
					else:
						delete_contact(target_tenant, target_conn.email_address, mapping.target_id)
					frappe.delete_doc("ITSync Mapping", mapping.name, ignore_permissions=True)
					counts["deleted"] += 1
					pending_lines.append(f"[{_timestamp()}] deleted mapping for source_id {source_id}")
			except Exception as e:
				counts["errors"] += 1
				error_details.append({"source_id": source_id, "error": str(e), "action": "delete"})
				pending_lines.append(f"[{_timestamp()}] ERROR delete {source_id}: {e}")

			if processed % PROGRESS_EVERY == 0:
				_append_log_lines(log_name, pending_lines)
				pending_lines = []
				_write_progress(log_name, processed, total, f"Deleting ({processed}/{total})")
				frappe.db.commit()
	else:
		for source_id in deleted_ids:
			processed += 1
			mapping_row = _find_mapping(pair.name, source_id, ["name", "status"])
			if mapping_row:
				if mapping_row.status == "Conflict":
					# Conflict mapping for a now-deleted source — just drop it.
					frappe.delete_doc("ITSync Mapping", mapping_row["name"], ignore_permissions=True)
				else:
					frappe.db.set_value(
						"ITSync Mapping", mapping_row["name"], "status", "Orphaned",
						update_modified=False,
					)
				counts["skipped"] += 1

	if pending_lines:
		_append_log_lines(log_name, pending_lines)

	_write_progress(log_name, total, total, "Finalizing")
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


def _find_mapping(pair_name, source_id, fields):
	"""Case-sensitive lookup of an ITSync Mapping by (sync_pair, source_id).

	Microsoft Graph contact IDs are case-sensitive base64 tokens, but the
	source_id column collation (utf8mb4_*_ci) compares text case-insensitively.
	A plain frappe.db.get_value filter therefore matches a different contact
	whose ID differs only in letter case, collapsing both onto one mapping row
	— the affected contact never gets its own mapping and is re-created on
	every sync. The BINARY cast forces a byte-exact comparison regardless of
	column collation. Returns a frappe._dict of the requested fields, or None.
	"""
	columns = ", ".join(f"`{f}`" for f in fields)
	rows = frappe.db.sql(
		f"SELECT {columns} FROM `tabITSync Mapping` "
		"WHERE sync_pair = %s AND source_id = BINARY %s LIMIT 1",
		(pair_name, source_id),
		as_dict=True,
	)
	return rows[0] if rows else None


def _create_mapping(pair_name: str, source_contact: dict, target_id: str):
	"""Create or update an ITSync Mapping record.

	Idempotent: if a mapping for (sync_pair, source_id) already exists, it
	is updated in place rather than creating a duplicate. This prevents the
	duplicate-mapping problem that occurred when a crashed initial sync left
	behind partial mappings and the next run re-created them.
	"""
	source_id = source_contact["id"]
	existing_row = _find_mapping(pair_name, source_id, ["name"])
	existing = existing_row["name"] if existing_row else None
	values = {
		"target_id": target_id,
		"source_email": get_primary_email(source_contact),
		"display_name": source_contact.get("display_name"),
		"field_hash": compute_field_hash(source_contact),
		"status": "Synced",
		"last_synced": frappe.utils.now_datetime(),
	}
	if existing:
		frappe.db.set_value("ITSync Mapping", existing, values, update_modified=False)
	else:
		mapping = frappe.new_doc("ITSync Mapping")
		mapping.sync_pair = pair_name
		mapping.source_id = source_id
		mapping.update(values)
		mapping.insert(ignore_permissions=True)


def _create_conflict_mapping(pair_name, source_contact, conflict_kind, conflict_detail):
	"""Record a permanent conflict as a Mapping row with status='Conflict'.

	Used when New-MailContact fails for a structural reason that will reproduce
	on every retry (ProxyAddressExists, AmbiguousIdentity, …). Storing a mapping
	with status='Conflict' prevents the engine from re-attempting on every sync;
	the contact's user-visible details are still tracked here so a sync-report
	can surface what was skipped and why. If the underlying conflict gets
	resolved out-of-band, an admin deletes the mapping to trigger a fresh
	create attempt on the next run.

	Idempotent like _create_mapping: existing rows are updated in place.
	"""
	source_id = source_contact["id"]
	existing_row = _find_mapping(pair_name, source_id, ["name"])
	values = {
		"target_id": "",  # nothing to manage in the target
		"source_email": get_primary_email(source_contact),
		"display_name": source_contact.get("display_name"),
		"field_hash": compute_field_hash(source_contact),
		"status": "Conflict",
		"conflict_kind": conflict_kind,
		"conflict_detail": conflict_detail,
		"last_synced": frappe.utils.now_datetime(),
	}
	if existing_row:
		frappe.db.set_value("ITSync Mapping", existing_row["name"], values, update_modified=False)
	else:
		mapping = frappe.new_doc("ITSync Mapping")
		mapping.sync_pair = pair_name
		mapping.source_id = source_id
		mapping.update(values)
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
