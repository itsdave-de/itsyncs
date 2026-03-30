frappe.ui.form.on("ITSync Backup", {
	refresh(frm) {
		if (!frm.is_new() && frm.doc.status !== "Running") {
			frm.add_custom_button(__("Run Backup"), () => {
				frm.call("run_backup").then(() => {
					frm.reload_doc();
				});
			}).addClass("btn-primary-dark");
		}

		if (frm.doc.status === "Running") {
			frm.dashboard.set_headline(__("Backup is running..."));
		}

		if (frm.doc.backup_file && frm.doc.status === "Success") {
			frm.add_custom_button(__("Download"), () => {
				window.open(frm.doc.backup_file);
			}).addClass("btn-primary");
		}

		frappe.realtime.on("itsync_backup_complete", (data) => {
			if (data.backup === frm.doc.name) {
				frm.reload_doc();
				let indicator = data.status === "Success" ? "green" : "red";
				frappe.show_alert({
					message: __("Backup {0}: {1} contacts exported", [data.status, data.contact_count]),
					indicator: indicator,
				});
			}
		});
	},
});
