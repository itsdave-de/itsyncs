frappe.ui.form.on("ITSync Mobile Device", {
	refresh(frm) {
		if (frm.is_new()) return;

		const token = frm.doc.enroll_token;
		if (token) {
			frm.add_web_link(`/dav-enroll/${token}`, __("Open Enrollment Page"));
		}

		if (frm.doc.email) {
			frm.add_custom_button(__("Send Email Invite"), () => {
				frm.call("send_email_invite").then(() => frm.reload_doc());
			}, __("Invite"));
		}

		if (frm.doc.mobile_number) {
			frm.add_custom_button(__("Send SMS Invite"), () => {
				frm.call("send_sms_invite").then(() => frm.reload_doc());
			}, __("Invite"));
		}

		frm.add_custom_button(__("Enrollment-Link neu erzeugen"), () => {
			frappe.confirm(__("Neuen Einrichtungs-Link erzeugen? Der bisherige Link wird ungültig."), () => {
				frm.call("reissue_enrollment_link").then(() => frm.reload_doc());
			});
		}, __("Invite"));

		if (frm.doc.status !== "Revoked") {
			frm.add_custom_button(__("Revoke Access"), () => {
				frappe.confirm(__("Revoke CardDAV access for this device?"), () => {
					frm.set_value("status", "Revoked");
					frm.save();
				});
			});
		}

		const exp = frm.doc.enroll_expires_at;
		if (exp) {
			const expired = frappe.datetime.now_datetime() > exp;
			frm.dashboard.add_indicator(
				expired
					? __("Einrichtungs-Link abgelaufen")
					: __("Link gültig bis {0}", [frappe.datetime.str_to_user(exp)]),
				expired ? "red" : "green"
			);
		}
	},
});
