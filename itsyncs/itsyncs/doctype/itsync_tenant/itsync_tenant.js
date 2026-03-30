frappe.ui.form.on("ITSync Tenant", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__("Test Connection"), () => {
				frm.call("test_connection").then(() => {
					frm.reload_doc();
				});
			});
		}
	},
});
