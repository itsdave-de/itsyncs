import frappe


SCHEDULE_MINUTES = {
	"Every 15 Minutes": 15,
	"Every 30 Minutes": 30,
	"Hourly": 60,
	"Every 2 Hours": 120,
	"Every 6 Hours": 360,
	"Daily": 1440,
}

HEARTBEAT_GRACE_SECONDS = 120


def run_due_syncs():
	"""Scheduler entry point. Runs watchdog cleanup, then enqueues due syncs."""
	_watchdog_cleanup()
	_enqueue_due_syncs()


def daily_sms_balance_check():
	"""Scheduler entry: if enabled, refresh + store the seven.io balance."""
	if not frappe.db.get_single_value("ITSync Settings", "sms_balance_daily_check"):
		return
	from itsyncs.carddav.notify import check_and_store_balance

	check_and_store_balance()


def radicale_watchdog():
	"""Reconcile the Radicale sidecar to the desired state + refresh diagnostics.

	Self-healing: if enabled and not running (crash / host reboot) it is
	restarted; if disabled and running it is stopped.
	"""
	from itsyncs.carddav import service

	enabled = frappe.db.get_single_value("ITSync Settings", "radicale_enabled")
	running = service.is_running()
	if enabled and not running:
		service.start()
	elif not enabled and running:
		service.stop()

	# Persist status + certificate/URL diagnostics for the Settings page / DB.
	from itsyncs.carddav import diagnostics

	diagnostics.get_diagnostics()


def _watchdog_cleanup():
	"""Reconcile 'Running' pairs against the actual RQ state.

	Covers the case where the work-horse was killed (SIGKILL) and neither the
	finally-block nor the on_failure callback ran: the pair stays on 'Running'
	forever. We compare Pair.current_job_id to RQ; if the job is gone, failed,
	stopped, or its RQ heartbeat is stale, we mark the pair + log as failed.
	"""
	from frappe.utils.background_jobs import get_job, get_job_status
	from rq.job import JobStatus

	running = frappe.get_all(
		"ITSync Pair",
		filters={"status": "Running"},
		fields=["name", "current_job_id", "last_run_log", "modified"],
	)

	if not running:
		return

	now = frappe.utils.now_datetime()

	for pair in running:
		reason = None

		if not pair.current_job_id:
			reason = "No current_job_id tracked — stale state"
		else:
			try:
				rq_status = get_job_status(pair.current_job_id)
				job = get_job(pair.current_job_id)
			except Exception as e:
				reason = f"Could not query RQ job status: {e}"
				rq_status = None
				job = None

			if rq_status is None:
				reason = f"RQ job '{pair.current_job_id}' is gone (expired or never enqueued)"
			elif rq_status in (JobStatus.FAILED, JobStatus.STOPPED, JobStatus.CANCELED):
				reason = f"RQ job ended with status '{rq_status}' without finalization"
			elif rq_status == JobStatus.STARTED and job is not None:
				heartbeat = getattr(job, "last_heartbeat", None)
				if heartbeat:
					try:
						from frappe.utils import get_datetime

						hb = get_datetime(heartbeat)
						age = (now - hb).total_seconds()
						if age > HEARTBEAT_GRACE_SECONDS:
							reason = f"RQ heartbeat stale ({int(age)}s since last ping)"
					except Exception:
						pass

		if reason:
			_mark_pair_dead(pair.name, pair.last_run_log, reason)

	frappe.db.commit()


def _mark_pair_dead(pair_name, log_name, reason):
	"""Mark a stale Running pair as Error, and its log as Failed."""
	now = frappe.utils.now_datetime()
	line = f"[{now.strftime('%H:%M:%S')}] WATCHDOG: {reason}"

	if log_name and frappe.db.exists("ITSync Log", log_name):
		log_status = frappe.db.get_value("ITSync Log", log_name, "status")
		if log_status == "Running":
			current = frappe.db.get_value("ITSync Log", log_name, "details") or ""
			new_details = (current + "\n" + line)[-60000:]
			frappe.db.set_value(
				"ITSync Log",
				log_name,
				{
					"status": "Failed",
					"completed_at": now,
					"details": new_details,
					"progress_phase": "Failed (watchdog)",
				},
				update_modified=False,
			)

	frappe.db.set_value(
		"ITSync Pair",
		pair_name,
		{"status": "Error", "current_job_id": "", "last_run": now},
		update_modified=False,
	)

	frappe.publish_realtime(
		"itsync_sync_complete",
		{"pair": pair_name, "status": "Failed", "log": log_name, "reason": "watchdog"},
	)


def _enqueue_due_syncs():
	"""Enqueue scheduled syncs for pairs whose schedule window has elapsed.

	Every due pair is enqueued — RQ is the pipeline that drains the queue at
	worker capacity (FIFO, so nothing starves; the worker count is the natural
	overload limit). We deliberately do NOT serialize per source: a sync writes
	no shared per-source state (the delta token lives on the pair, and contact
	data is never written back to the source), so concurrent runs from the same
	source are safe. The only serialization needed is per *pair* — preventing a
	pair from running twice at once — and that is already guaranteed by the
	status!=Running filter, the Running flag set before enqueue, and
	deduplicate=True with a fixed job_id.
	"""
	pairs = frappe.get_all(
		"ITSync Pair",
		filters={"enabled": 1, "initial_sync_complete": 1, "status": ["!=", "Running"]},
		fields=["name", "schedule", "last_run", "job_timeout"],
	)

	now = frappe.utils.now_datetime()

	for pair in pairs:
		interval_minutes = SCHEDULE_MINUTES.get(pair.schedule, 30)

		if pair.last_run:
			diff = (now - pair.last_run).total_seconds() / 60
			if diff < interval_minutes:
				continue

		job_id = f"itsync_scheduled_{pair.name}"

		log = frappe.new_doc("ITSync Log")
		log.sync_pair = pair.name
		log.sync_type = "Incremental"
		log.started_at = now
		log.status = "Running"
		log.progress_phase = "Queued"
		log.insert(ignore_permissions=True)

		frappe.db.set_value(
			"ITSync Pair",
			pair.name,
			{"status": "Running", "current_job_id": job_id, "last_run_log": log.name},
			update_modified=False,
		)
		frappe.db.commit()

		frappe.enqueue(
			"itsyncs.sync.engine.run_sync",
			pair_name=pair.name,
			sync_type="Incremental",
			log_name=log.name,
			queue="default",
			timeout=pair.job_timeout or 1800,
			deduplicate=True,
			job_id=job_id,
			on_failure="itsyncs.sync.engine.mark_sync_failed",
		)

	frappe.db.commit()


def cleanup_old_logs():
	"""Delete ITSync Log entries older than the configured max age.

	Runs daily via scheduler_events. Behaviour is controlled by the
	'ITSync Settings' single doctype:

	  - cleanup_logs_enabled (default 0): if 0, this function is a no-op.
	  - cleanup_logs_max_age_days (default 30): logs whose started_at is
	    older than (now - this many days) are removed.

	Running logs (status='Running') are never deleted, even if they exceed
	the age threshold — the watchdog in _watchdog_cleanup is responsible
	for surfacing those before they could be removed here.
	"""
	settings = frappe.get_single("ITSync Settings")
	if not settings.cleanup_logs_enabled:
		return

	days = int(settings.cleanup_logs_max_age_days or 30)
	if days <= 0:
		return

	cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), -days)
	old_logs = frappe.get_all(
		"ITSync Log",
		filters={
			"started_at": ["<", cutoff],
			"status": ["!=", "Running"],
		},
		pluck="name",
	)
	if not old_logs:
		return

	# Bulk delete via DB to avoid per-doc overhead (ITSync Log has no
	# on_trash hook, so a direct delete is safe and ~100× faster than
	# frappe.delete_doc in a loop for large batches).
	frappe.db.delete("ITSync Log", {"name": ["in", old_logs]})
	frappe.db.commit()

	frappe.logger("itsyncs").info(
		f"cleanup_old_logs: removed {len(old_logs)} log entries older than {days} days"
	)
