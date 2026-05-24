frappe.ui.form.on("ITSync Settings", {
	refresh(frm) {
		_itsync_settings_add_doc_buttons(frm);
	},
});

function _itsync_settings_add_doc_buttons(frm) {
	const open_blob = (html) => {
		const blob = new Blob([html], { type: "text/html;charset=utf-8" });
		const url = URL.createObjectURL(blob);
		const win = window.open(url, "_blank");
		setTimeout(() => URL.revokeObjectURL(url), 60000);
		if (!win) {
			frappe.msgprint(__("Popup blockiert — bitte Popups für diese Seite erlauben."));
		}
	};

	const button = (label, doc_name) => {
		frm.add_custom_button(__(label), () => {
			frm.call({
				method: "get_packaged_doc",
				doc: frm.doc,
				args: { name: doc_name },
				freeze: true,
				freeze_message: __("Dokument wird geöffnet …"),
			}).then((r) => {
				if (r.message) open_blob(r.message);
			});
		}, __("Dokumente"));
	};

	button("Kunden-Bericht", "kunden-bericht");
	button("Maßnahmenplan (intern)", "massnahmenplan");
}
