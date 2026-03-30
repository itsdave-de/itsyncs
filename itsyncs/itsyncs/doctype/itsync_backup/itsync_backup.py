import frappe
from frappe.model.document import Document


class ITSyncBackup(Document):
	def validate(self):
		conn = frappe.get_doc("ITSync Connector", self.connector)
		if conn.connection_status != "Connected":
			frappe.throw("Connector must be connected before creating a backup.")

	@frappe.whitelist()
	def run_backup(self):
		if self.status == "Running":
			frappe.throw("Backup is already running.")

		self.db_set("status", "Running")
		self.db_set("started_at", frappe.utils.now_datetime())
		frappe.db.commit()

		frappe.enqueue(
			"itsyncs.sync.backup.run_backup",
			backup_name=self.name,
			queue="default",
			timeout=600,
		)
		frappe.msgprint("Backup started. You will be notified when it completes.", alert=True)
