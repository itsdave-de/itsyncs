frappe.ui.form.on("ITSync Pair", {
	refresh(frm) {
		// Preview button - available when status is Ready and no initial sync yet
		if (!frm.is_new() && !frm.doc.initial_sync_complete) {
			frm.add_custom_button(__("Generate Preview"), () => {
				frm.call("generate_preview");
			}, __("Actions"));
		}

		// Initial Sync button - available when preview exists and no initial sync yet
		if (frm.doc.preview_generated_at && !frm.doc.initial_sync_complete && frm.doc.status !== "Running") {
			frm.add_custom_button(__("Run Initial Sync"), () => {
				frappe.confirm(
					__("This will sync {0} contacts from source to target. Continue?", [frm.doc.preview_to_create]),
					() => {
						frm.call("run_initial_sync");
					}
				);
			}, __("Actions"));
		}

		// Manual Sync button - available after initial sync
		if (frm.doc.initial_sync_complete && frm.doc.status !== "Running") {
			frm.add_custom_button(__("Sync Now"), () => {
				frm.call("run_manual_sync");
			}).addClass("btn-primary-dark");
		}

		// Status indicator
		if (frm.doc.status === "Running") {
			frm.dashboard.set_headline(__("Sync is currently running..."));
		}

		// Info banners based on target type
		if (frm.doc.target) {
			frappe.db.get_value("ITSync Connector", frm.doc.target, "connector_type").then((r) => {
				if (r.message && r.message.connector_type === "GAL") {
					frm.set_intro(
						__("Target is a GAL. Contacts will be written as <b>Mail Contacts</b> via Exchange Online. "
						+ "These appear in the Global Address List for all users in the tenant. "
						+ "Note: New contacts may take a few minutes to appear due to Exchange replication."),
						"blue"
					);
				}
			});
		}

		// Explain on_delete behavior
		if (frm.doc.on_delete === "Delete") {
			frm.fields_dict.on_delete.set_description(
				__("When a contact is deleted from the source, it will also be <b>removed from the target</b>.")
			);
		} else {
			frm.fields_dict.on_delete.set_description(
				__("When a contact is deleted from the source, it will be <b>kept in the target</b> (mapping marked as orphaned).")
			);
		}

		// Realtime updates — unbind first, then use dedup flag to ignore duplicates
		frappe.realtime.off("itsync_preview_complete");
		frappe.realtime.on("itsync_preview_complete", (data) => {
			if (data.pair !== frm.doc.name) return;
			if (frm._preview_notified) return;
			frm._preview_notified = true;
			setTimeout(() => { frm._preview_notified = false; }, 5000);
			frm.reload_doc();
			frappe.show_alert({
				message: __("Preview complete: {0} to create, {1} already matched", [data.to_create, data.matched]),
				indicator: "green",
			});
		});

		frappe.realtime.off("itsync_sync_complete");
		frappe.realtime.on("itsync_sync_complete", (data) => {
			if (data.pair !== frm.doc.name) return;
			if (frm._sync_notified) return;
			frm._sync_notified = true;
			setTimeout(() => { frm._sync_notified = false; }, 5000);
			frm.reload_doc();
			let indicator = data.status === "Success" ? "green" : data.status === "Partial" ? "orange" : "red";
			frappe.show_alert({
				message: __("Sync {0}: {1} created, {2} updated, {3} deleted, {4} errors",
					[data.status, data.created, data.updated, data.deleted, data.errors]),
				indicator: indicator,
			});
		});
	},
});
