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
		frappe.enqueue(
			"itsyncs.sync.engine.generate_preview",
			pair_name=self.name,
			queue="default",
			timeout=600,
			deduplicate=True,
			job_id=f"itsync_preview_{self.name}",
		)
		frappe.msgprint("Preview generation started. This may take a moment...", alert=True)

	@frappe.whitelist()
	def run_initial_sync(self):
		if self.initial_sync_complete:
			frappe.throw("Initial sync has already been completed.")

		if not self.preview_generated_at:
			frappe.throw("Please generate a preview first before running the initial sync.")

		self.db_set("status", "Running")
		frappe.enqueue(
			"itsyncs.sync.engine.run_sync",
			pair_name=self.name,
			sync_type="Initial",
			queue="long",
			timeout=3600,
			deduplicate=True,
			job_id=f"itsync_initial_{self.name}",
		)
		frappe.msgprint("Initial sync started. Check the sync log for progress.", alert=True)

	@frappe.whitelist()
	def run_manual_sync(self):
		if not self.initial_sync_complete:
			frappe.throw("Initial sync must be completed before running manual syncs.")

		if self.status == "Running":
			frappe.throw("A sync is already running for this pair.")

		self.db_set("status", "Running")
		frappe.enqueue(
			"itsyncs.sync.engine.run_sync",
			pair_name=self.name,
			sync_type="Manual",
			queue="default",
			timeout=1800,
			deduplicate=True,
			job_id=f"itsync_manual_{self.name}",
		)
		frappe.msgprint("Manual sync started.", alert=True)
