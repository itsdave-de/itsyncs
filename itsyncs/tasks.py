import frappe


SCHEDULE_MINUTES = {
	"Every 15 Minutes": 15,
	"Every 30 Minutes": 30,
	"Hourly": 60,
	"Every 2 Hours": 120,
	"Every 6 Hours": 360,
	"Daily": 1440,
}


def run_due_syncs():
	"""Called by the scheduler. Checks all enabled pairs and enqueues due syncs."""
	pairs = frappe.get_all(
		"ITSync Pair",
		filters={"enabled": 1, "initial_sync_complete": 1, "status": ["!=", "Running"]},
		fields=["name", "schedule", "last_run"],
	)

	now = frappe.utils.now_datetime()

	for pair in pairs:
		interval_minutes = SCHEDULE_MINUTES.get(pair.schedule, 30)

		if pair.last_run:
			diff = (now - pair.last_run).total_seconds() / 60
			if diff < interval_minutes:
				continue

		frappe.db.set_value("ITSync Pair", pair.name, "status", "Running")
		frappe.enqueue(
			"itsyncs.sync.engine.run_sync",
			pair_name=pair.name,
			sync_type="Incremental",
			queue="default",
			timeout=1800,
			deduplicate=True,
			job_id=f"itsync_scheduled_{pair.name}",
		)

	frappe.db.commit()
