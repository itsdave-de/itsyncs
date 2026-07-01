frappe.ui.form.on("ITSync Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test-SMS senden"), () => {
			frappe.prompt(
				[
					{
						fieldname: "to",
						label: __("Mobilnummer"),
						fieldtype: "Data",
						reqd: 1,
						description: __("z. B. 0151… oder +49151…"),
					},
					{
						fieldname: "message",
						label: __("Text (optional)"),
						fieldtype: "Small Text",
					},
				],
				(values) => {
					frm.call({
						method: "send_test_sms",
						doc: frm.doc,
						args: { to: values.to, message: values.message },
						freeze: true,
						freeze_message: __("SMS wird versendet …"),
					}).then((r) => {
						if (r.message && r.message.log) {
							frappe.set_route("Form", "ITSync SMS Log", r.message.log);
						}
					});
				},
				__("Test-SMS senden"),
				__("Senden")
			);
		}, __("Aktionen"));

		frm.add_custom_button(__("Guthaben abrufen"), () => {
			frm.call({
				method: "get_sms_balance",
				doc: frm.doc,
				freeze: true,
				freeze_message: __("Guthaben wird abgefragt …"),
			}).always(() => frm.reload_doc());
		}, __("Aktionen"));

		frm.add_custom_button(__("SMS-Protokoll"), () => {
			frappe.set_route("List", "ITSync SMS Log");
		}, __("Aktionen"));
	},
});
