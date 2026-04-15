import frappe


def execute():
	"""One-time cleanup: mark all 'Running' Pair/Log entries older than 1 hour as stale.

	Context: prior to the on_failure + watchdog rework, a work-horse SIGKILL left
	pair.status='Running' and log.status='Running' forever. This patch sweeps the
	existing state so the new code starts from a clean slate.
	"""
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1)

	stale_logs = frappe.get_all(
		"ITSync Log",
		filters={"status": "Running", "started_at": ["<", cutoff]},
		fields=["name"],
	)
	for log in stale_logs:
		current = frappe.db.get_value("ITSync Log", log.name, "details") or ""
		note = f"\n[migrate] Marked as Failed by patch: status was 'Running' for over an hour without finalization."
		frappe.db.set_value(
			"ITSync Log",
			log.name,
			{
				"status": "Failed",
				"completed_at": frappe.utils.now_datetime(),
				"details": (current + note)[-60000:],
				"progress_phase": "Failed (baseline cleanup)",
			},
			update_modified=False,
		)

	stale_pairs = frappe.get_all(
		"ITSync Pair",
		filters={"status": "Running", "modified": ["<", cutoff]},
		fields=["name"],
	)
	for pair in stale_pairs:
		frappe.db.set_value(
			"ITSync Pair",
			pair.name,
			{"status": "Error", "current_job_id": ""},
			update_modified=False,
		)

	if stale_logs or stale_pairs:
		print(f"itsyncs: reconciled {len(stale_logs)} stale log(s) and {len(stale_pairs)} stale pair(s)")

	frappe.db.commit()
