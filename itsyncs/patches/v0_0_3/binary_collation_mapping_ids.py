import frappe


def execute():
	"""Switch ITSync Mapping.source_id / target_id to a binary collation.

	Microsoft Graph contact IDs are case-sensitive base64 tokens. The default
	utf8mb4 *_ci collation makes SQL comparisons case-insensitive, so two
	contacts whose IDs differ only in letter case collapse onto a single
	mapping row — the affected contact never gets its own mapping and is
	re-created on every sync (mailbox targets accumulate duplicates, GAL
	targets throw New-MailContact 409 conflicts).

	The engine code additionally compares IDs with an explicit BINARY cast
	(see _find_mapping in itsyncs/sync/engine.py), so correctness no longer
	depends on this column collation. The ALTER keeps the schema honest and
	any ad-hoc or list-view queries consistent.
	"""
	if frappe.db.db_type != "mariadb":
		return

	for column in ("source_id", "target_id"):
		frappe.db.sql_ddl(
			f"ALTER TABLE `tabITSync Mapping` "
			f"MODIFY `{column}` TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
		)

	print("itsyncs: ITSync Mapping.source_id/target_id set to utf8mb4_bin")
