import frappe
from frappe.model.document import Document


class ITSyncPair(Document):
	def validate(self):
		self._validate_target_writable()
		self._validate_sage_source_only()
		self._validate_different_connectors()
		self._validate_enabled()
		self._update_status()

	def _validate_target_writable(self):
		target = frappe.get_doc("ITSync Connector", self.target)
		if not target.is_writable:
			frappe.throw("Target connector must be writable.")

	def _validate_sage_source_only(self):
		target = frappe.get_doc("ITSync Connector", self.target)
		if target.connector_type == "Sage SQL":
			frappe.throw("Sage SQL connectors can only be used as a sync source, not as a target.")

	def _validate_different_connectors(self):
		if self.source == self.target:
			frappe.throw("Source and Target must be different connectors.")

	def _validate_enabled(self):
		if self.enabled and not self.initial_sync_complete:
			frappe.throw("Auto sync can only be enabled after the initial sync has been completed.")

	def _update_status(self):
		if not self.initial_sync_complete and self.status not in ("Running",):
			source = frappe.get_doc("ITSync Connector", self.source)
			target = frappe.get_doc("ITSync Connector", self.target)
			if source.connection_status == "Connected" and target.connection_status == "Connected":
				self.status = "Ready"
			else:
				self.status = "Draft"

	@frappe.whitelist()
	def generate_preview(self):
		from itsyncs.sync.engine import generate_preview

		generate_preview(self.name)
		self.reload()
		frappe.msgprint(
			f"Preview complete: {self.preview_to_create} to create, {self.preview_matched} already matched.",
			alert=True,
			indicator="green",
		)

	@frappe.whitelist()
	def run_initial_sync(self):
		if self.initial_sync_complete:
			frappe.throw("Initial sync has already been completed.")

		if not self.preview_generated_at:
			frappe.throw("Please generate a preview first before running the initial sync.")

		if self.status == "Running":
			frappe.throw("A sync is already running for this pair.")

		self._start_sync(sync_type="Initial", queue="long", job_id_prefix="itsync_initial")
		frappe.msgprint("Initial sync started. Check the sync log for progress.", alert=True)

	@frappe.whitelist()
	def run_manual_sync(self):
		if not self.initial_sync_complete:
			frappe.throw("Initial sync must be completed before running manual syncs.")

		if self.status == "Running":
			frappe.throw("A sync is already running for this pair.")

		self._start_sync(sync_type="Manual", queue="default", job_id_prefix="itsync_manual")
		frappe.msgprint("Manual sync started.", alert=True)

	def _start_sync(self, sync_type, queue, job_id_prefix):
		"""Create log + pair state + enqueue job with on_failure callback."""
		job_id = f"{job_id_prefix}_{self.name}"

		log = frappe.new_doc("ITSync Log")
		log.sync_pair = self.name
		log.sync_type = sync_type
		log.started_at = frappe.utils.now_datetime()
		log.status = "Running"
		log.progress_phase = "Queued"
		log.insert(ignore_permissions=True)

		self.db_set("current_job_id", job_id, update_modified=False)
		self.db_set("last_run_log", log.name, update_modified=False)
		self.db_set("status", "Running", update_modified=False)
		frappe.db.commit()

		frappe.enqueue(
			"itsyncs.sync.engine.run_sync",
			pair_name=self.name,
			sync_type=sync_type,
			log_name=log.name,
			queue=queue,
			timeout=self.job_timeout or (3600 if sync_type == "Initial" else 1800),
			deduplicate=True,
			job_id=job_id,
			on_failure="itsyncs.sync.engine.mark_sync_failed",
		)

	@frappe.whitelist()
	def get_live_status(self):
		"""Return the current runtime state for the pair: DB status + RQ job status +
		heartbeat + progress from the current log. Used by the form JS for polling."""
		from frappe.utils.background_jobs import get_job, get_job_status
		from rq.job import JobStatus

		self.reload()

		result = {
			"pair_status": self.status,
			"current_job_id": self.current_job_id or "",
			"last_run_log": self.last_run_log,
			"last_run": self.last_run,
			"rq_status": None,
			"heartbeat_age_seconds": None,
			"progress_current": 0,
			"progress_total": 0,
			"progress_phase": "",
			"log_status": None,
			"inconsistent": False,
		}

		log_info = None
		if self.last_run_log and frappe.db.exists("ITSync Log", self.last_run_log):
			log_info = frappe.db.get_value(
				"ITSync Log",
				self.last_run_log,
				["status", "progress_current", "progress_total", "progress_phase",
				 "started_at", "completed_at", "created_count", "updated_count",
				 "deleted_count", "skipped_count", "error_count"],
				as_dict=True,
			)
			if log_info:
				result.update({
					"log_status": log_info.status,
					"progress_current": log_info.progress_current or 0,
					"progress_total": log_info.progress_total or 0,
					"progress_phase": log_info.progress_phase or "",
					"started_at": log_info.started_at,
					"completed_at": log_info.completed_at,
					"created_count": log_info.created_count or 0,
					"updated_count": log_info.updated_count or 0,
					"deleted_count": log_info.deleted_count or 0,
					"skipped_count": log_info.skipped_count or 0,
					"error_count": log_info.error_count or 0,
				})

		if self.current_job_id:
			try:
				rq_status = get_job_status(self.current_job_id)
				result["rq_status"] = str(rq_status) if rq_status else None
				if rq_status == JobStatus.STARTED:
					job = get_job(self.current_job_id)
					if job and getattr(job, "last_heartbeat", None):
						from frappe.utils import get_datetime

						hb = get_datetime(job.last_heartbeat)
						age = (frappe.utils.now_datetime() - hb).total_seconds()
						result["heartbeat_age_seconds"] = int(age)
			except Exception as e:
				result["rq_error"] = str(e)

		# Inconsistency detection: pair says Running but RQ disagrees
		if self.status == "Running":
			if not self.current_job_id:
				result["inconsistent"] = True
				result["inconsistency_reason"] = "No job ID tracked"
			elif result["rq_status"] is None:
				result["inconsistent"] = True
				result["inconsistency_reason"] = "Job not found in queue"
			elif result["rq_status"] in (str(JobStatus.FAILED), str(JobStatus.STOPPED), str(JobStatus.CANCELED)):
				result["inconsistent"] = True
				result["inconsistency_reason"] = f"Job ended ({result['rq_status']}) but pair still marked Running"
			elif result["heartbeat_age_seconds"] and result["heartbeat_age_seconds"] > 120:
				result["inconsistent"] = True
				result["inconsistency_reason"] = f"No heartbeat for {result['heartbeat_age_seconds']}s"

		return result

	@frappe.whitelist()
	def list_target_contacts(self, limit=200, offset=0):
		"""Fetch the current state of the target connector via Graph/Exchange.
		Used by the UI 'Show Target Contacts' button as a ground-truth sanity check."""
		from itsyncs.sync.engine import (
			_fetch_target_contacts, _get_client_for_connector, _get_tenant_for_connector,
		)
		from itsyncs.sync.matching import get_primary_email

		target_conn = frappe.get_doc("ITSync Connector", self.target)
		client = _get_client_for_connector(target_conn)
		tenant = _get_tenant_for_connector(target_conn)

		all_contacts = _fetch_target_contacts(client, target_conn, tenant)
		all_contacts = sorted(all_contacts, key=lambda c: (c.get("display_name") or "").lower())

		offset = int(offset or 0)
		limit = int(limit or 200)
		page = all_contacts[offset:offset + limit]

		rows = []
		for c in page:
			phones = c.get("business_phones") or []
			rows.append({
				"id": c.get("id"),
				"display_name": c.get("display_name") or "",
				"email": get_primary_email(c) or "",
				"company": c.get("company_name") or "",
				"job_title": c.get("job_title") or "",
				"phone": c.get("mobile_phone") or (phones[0] if phones else ""),
			})

		return {
			"total": len(all_contacts),
			"offset": offset,
			"limit": limit,
			"connector_name": target_conn.name,
			"connector_type": target_conn.connector_type,
			"contacts": rows,
		}

	@frappe.whitelist()
	def run_api_smoke_test(self, count=3, mode="serial", batch_wait=15):
		"""Create, update, and delete N test contacts against the target connector.

		mode='serial' (default): for each contact, create immediately followed by
		the rich Set-Contact — mirrors what the real sync engine currently does.

		mode='batch': phase A = all creates, then wait batch_wait seconds for
		Exchange replication, then phase B = all rich Set-Contacts on first
		attempt (no retry schedule). This is the mode we're evaluating for the
		initial-sync rewrite.

		Returns a per-operation log with timing. Deletion runs in a finally-block
		so test contacts are cleaned up even if create/update phase raises.
		"""
		if mode == "batch":
			return self._smoke_test_batch(int(count or 3), int(batch_wait or 15))
		return self._smoke_test_serial(int(count or 3))

	def _smoke_test_serial(self, count):
		import time
		import uuid

		from itsyncs.sync.engine import _get_client_for_connector, _get_tenant_for_connector
		from itsyncs.graph.exchange import (
			create_mail_contact, update_mail_contact, delete_mail_contact,
		)
		from itsyncs.graph.contacts import (
			create_contact, update_contact, delete_contact,
		)

		target_conn = frappe.get_doc("ITSync Connector", self.target)
		client = _get_client_for_connector(target_conn)
		tenant = _get_tenant_for_connector(target_conn)

		count = max(1, min(int(count or 3), 7))
		run_id = uuid.uuid4().hex[:8]
		is_gal = target_conn.connector_type == "GAL"

		events = []
		created_ids = []

		def record(phase, idx, ok, started, detail=""):
			events.append({
				"phase": phase,
				"idx": idx,
				"ok": ok,
				"duration_ms": round((time.time() - started) * 1000),
				"detail": detail,
			})

		try:
			# CREATE
			for i in range(1, count + 1):
				email = f"smoketest-{run_id}-{i}@itsync-test.example.com"
				contact = {
					"display_name": f"[SMOKETEST {run_id}] Contact {i}",
					"given_name": "SmokeTest",
					"surname": f"No{i}",
					"email_addresses": [{"address": email, "name": f"SmokeTest No{i}"}],
					"business_phones": ["+49 123 456789"],
					"company_name": "itsyncs Smoke Test",
					"job_title": "Test Contact",
				}
				t0 = time.time()
				try:
					if is_gal:
						new_id = create_mail_contact(tenant, contact)
					else:
						new_id = create_contact(tenant, target_conn.email_address, contact, target_conn.contact_folder or None)
					created_ids.append(new_id)
					record("create", i, True, t0, f"{email} → {new_id}")
				except Exception as e:
					record("create", i, False, t0, f"{type(e).__name__}: {str(e)[:200]}")

			# UPDATE (only the ones we successfully created)
			for i, cid in enumerate(created_ids, start=1):
				contact = {
					"display_name": f"[SMOKETEST {run_id}] Contact {i} (updated)",
					"job_title": "Updated Test Contact",
					"email_addresses": [{"address": f"smoketest-{run_id}-{i}@itsync-test.example.com"}],
				}
				t0 = time.time()
				try:
					if is_gal:
						update_mail_contact(tenant, cid, contact)
					else:
						update_contact(tenant, target_conn.email_address, cid, contact)
					record("update", i, True, t0, cid)
				except Exception as e:
					record("update", i, False, t0, f"{type(e).__name__}: {str(e)[:200]}")
		finally:
			# DELETE — always run, even if create/update raised
			for i, cid in enumerate(created_ids, start=1):
				t0 = time.time()
				try:
					if is_gal:
						delete_mail_contact(tenant, cid)
					else:
						delete_contact(tenant, target_conn.email_address, cid)
					record("delete", i, True, t0, cid)
				except Exception as e:
					record("delete", i, False, t0, f"{type(e).__name__}: {str(e)[:200]}")

		summary = {
			"create": {"ok": 0, "fail": 0, "total_ms": 0},
			"update": {"ok": 0, "fail": 0, "total_ms": 0},
			"delete": {"ok": 0, "fail": 0, "total_ms": 0},
		}
		for e in events:
			b = summary[e["phase"]]
			b["ok" if e["ok"] else "fail"] += 1
			b["total_ms"] += e["duration_ms"]
		for phase, b in summary.items():
			done = b["ok"] + b["fail"]
			b["avg_ms"] = round(b["total_ms"] / done) if done else 0

		return {
			"run_id": run_id,
			"connector_name": target_conn.name,
			"connector_type": target_conn.connector_type,
			"count_requested": count,
			"events": events,
			"summary": summary,
			"mode": "serial",
		}

	def _smoke_test_batch(self, count, batch_wait):
		"""Delayed-batch smoke test: phase A creates all contacts, phase W waits
		for Exchange replication, phase B runs Set-Contact in one pass without
		retry. Measures what _run_initial_sync would look like if restructured."""
		import time
		import uuid

		from itsyncs.sync.engine import _get_tenant_for_connector
		from itsyncs.graph.exchange import (
			_invoke_command, delete_mail_contact,
		)

		target_conn = frappe.get_doc("ITSync Connector", self.target)
		tenant = _get_tenant_for_connector(target_conn)

		if target_conn.connector_type != "GAL":
			frappe.throw("Batch smoke test is only implemented for GAL targets.")
		if not tenant:
			frappe.throw("Target has no tenant.")

		count = max(1, min(int(count or 5), 7))
		batch_wait = max(0, min(int(batch_wait or 15), 60))
		run_id = uuid.uuid4().hex[:8]

		events = []
		created = []  # list of (idx, email, alias)

		def record(phase, idx, ok, started, detail=""):
			events.append({
				"phase": phase,
				"idx": idx,
				"ok": ok,
				"duration_ms": round((time.time() - started) * 1000),
				"detail": detail,
			})

		try:
			# ----- Phase A: create all -----
			for i in range(1, count + 1):
				email = f"batchsmoke-{run_id}-{i}@itsync-test.example.com"
				name = f"[BATCHSMOKE {run_id}] Contact {i}"
				t0 = time.time()
				try:
					r = _invoke_command(tenant, "New-MailContact", {
						"Name": name, "ExternalEmailAddress": email, "DisplayName": name,
						"FirstName": "Batch", "LastName": f"No{i}",
					})
					alias = r[0].get("Alias") if r else None
					if alias:
						created.append((i, email, alias))
						record("create", i, True, t0, f"{email} → {alias}")
					else:
						record("create", i, False, t0, "no alias returned")
				except Exception as e:
					record("create", i, False, t0, f"{type(e).__name__}: {str(e)[:150]}")

			# ----- Phase W: wait for replication -----
			t0 = time.time()
			time.sleep(batch_wait)
			record("wait", 0, True, t0, f"{batch_wait}s")

			# ----- Phase B: set all rich properties, single attempt each -----
			for i, email, alias in created:
				rich = {
					"Identity": email,
					"Company": "Batch Smoke Test",
					"Title": "Engineer",
					"Phone": "+49 123 456789",
					"Department": "Demo",
				}
				t0 = time.time()
				try:
					_invoke_command(tenant, "Set-Contact", rich)
					record("set", i, True, t0, email)
				except Exception as e:
					record("set", i, False, t0, f"{type(e).__name__}: {str(e)[:150]}")
		finally:
			# ----- Cleanup: delete every created contact -----
			for i, email, alias in created:
				t0 = time.time()
				try:
					delete_mail_contact(tenant, alias)
					record("delete", i, True, t0, alias)
				except Exception as e:
					record("delete", i, False, t0, f"{type(e).__name__}: {str(e)[:150]}")

		summary = {
			"create": {"ok": 0, "fail": 0, "total_ms": 0},
			"wait":   {"ok": 0, "fail": 0, "total_ms": 0},
			"set":    {"ok": 0, "fail": 0, "total_ms": 0},
			"delete": {"ok": 0, "fail": 0, "total_ms": 0},
		}
		for e in events:
			b = summary[e["phase"]]
			b["ok" if e["ok"] else "fail"] += 1
			b["total_ms"] += e["duration_ms"]
		for phase, b in summary.items():
			done = b["ok"] + b["fail"]
			b["avg_ms"] = round(b["total_ms"] / done) if done else 0

		return {
			"run_id": run_id,
			"connector_name": target_conn.name,
			"connector_type": target_conn.connector_type,
			"count_requested": count,
			"batch_wait": batch_wait,
			"events": events,
			"summary": summary,
			"mode": "batch",
		}

	@frappe.whitelist()
	def cleanup_test_contacts(self):
		"""Delete any leftover test contacts from the target GAL.

		Matches anything that either:
		  - has an email address on the reserved test domain 'itsync-test.example.com', or
		  - has a display name starting with one of our known test prefixes
		    ([SMOKETEST, [BATCHSMOKE, [BENCH, [CMC-, [POC-).

		Safe to call from the UI — it only touches contacts that could not have
		been created by a real sync.
		"""
		from itsyncs.sync.engine import _get_client_for_connector, _get_tenant_for_connector
		from itsyncs.graph.exchange import fetch_all_mail_contacts, delete_mail_contact

		target_conn = frappe.get_doc("ITSync Connector", self.target)
		tenant = _get_tenant_for_connector(target_conn)

		if target_conn.connector_type != "GAL":
			frappe.throw("Test-contact cleanup is only implemented for GAL targets.")
		if not tenant:
			frappe.throw("Target has no tenant.")

		TEST_EMAIL_DOMAIN = "@itsync-test.example.com"
		TEST_NAME_PREFIXES = ("[SMOKETEST", "[BATCHSMOKE", "[BENCH", "[CMC-", "[POC-")

		all_contacts = fetch_all_mail_contacts(tenant)
		to_delete = []
		for c in all_contacts:
			display_name = (c.get("display_name") or "")
			emails = c.get("email_addresses") or []
			has_test_email = any(TEST_EMAIL_DOMAIN in (e.get("address") or "") for e in emails)
			has_test_name = any(display_name.startswith(p) for p in TEST_NAME_PREFIXES)
			if has_test_email or has_test_name:
				to_delete.append({
					"id": c.get("id"),
					"display_name": display_name,
					"email": (emails[0].get("address") if emails else ""),
				})

		deleted = []
		failed = []
		for item in to_delete:
			try:
				delete_mail_contact(tenant, item["id"])
				deleted.append(item)
			except Exception as e:
				item["error"] = str(e)[:200]
				failed.append(item)

		return {
			"total_scanned": len(all_contacts),
			"total_matched": len(to_delete),
			"deleted": deleted,
			"failed": failed,
		}

	@frappe.whitelist()
	def force_clean_stale(self):
		"""Manually mark a stuck 'Running' pair as Error and its log as Failed.
		Intended for the UI 'Force-clean' button when the watchdog hasn't run yet."""
		from itsyncs.tasks import _mark_pair_dead

		if self.status != "Running":
			frappe.throw("Pair is not in Running state — nothing to clean.")

		_mark_pair_dead(self.name, self.last_run_log, "Manually force-cleaned by user")
		frappe.db.commit()
		self.reload()
		return {"cleaned": True, "new_status": self.status}
