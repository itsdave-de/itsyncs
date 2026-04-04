frappe.ui.form.on("ITSync Connector", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__("Test Connection"), () => {
				frm.call("test_connection").then(() => {
					frm.reload_doc();
				});
			});
		}

		// Info banner for GAL connectors
		if (frm.doc.connector_type === "GAL") {
			frm.set_intro(
				__("GAL connectors can be used as source (read) and target (write). "
				+ "Writing to the GAL requires the Azure App to have: "
				+ "<b>Exchange.ManageAsApp</b> permission and the <b>Exchange Administrator</b> directory role. "
				+ "The Enterprise App <b>Office 365 Exchange Online</b> must be registered in the tenant."),
				"blue"
			);
		}

		// Info banner for Sage SQL connectors
		if (frm.doc.connector_type === "Sage SQL") {
			frm.set_intro(
				__("Sage SQL connectors are <b>read-only sources</b>. "
				+ "Contacts are fetched from the Sage Office Line database and synced to an Exchange target. "
				+ "Change detection uses SQL Server rowversion for efficient incremental syncs."),
				"blue"
			);
		}

		// Info for Mailbox connectors
		if ((frm.doc.connector_type === "Mailbox" || frm.doc.connector_type === "Shared Mailbox") && !frm.doc.contact_folder) {
			frm.set_intro(
				__("Using the default contacts folder. Use <b>Settings → Select Folder</b> to sync from/to a specific subfolder."),
				"blue"
			);
		}

		// Show folder selection button for Mailbox types
		if (
			!frm.is_new() &&
			(frm.doc.connector_type === "Mailbox" || frm.doc.connector_type === "Shared Mailbox") &&
			frm.doc.email_address &&
			frm.doc.connection_status === "Connected"
		) {
			frm.add_custom_button(__("Select Folder"), () => {
				frm.call("fetch_folders").then((r) => {
					if (!r.message || r.message.length === 0) {
						frappe.msgprint(__("No contact folders found (only the default folder exists)."));
						return;
					}

					let options = [
						{ label: __("Default (Contacts)"), value: "" },
					];
					for (let folder of r.message) {
						options.push({
							label: folder.name,
							value: folder.id,
						});
					}

					let current = frm.doc.contact_folder || "";

					let d = new frappe.ui.Dialog({
						title: __("Select Contact Folder"),
						fields: [
							{
								fieldtype: "Select",
								fieldname: "folder",
								label: __("Contact Folder"),
								options: options.map((o) => o.value),
								default: current,
							},
							{
								fieldtype: "HTML",
								fieldname: "folder_list",
								options: _build_folder_html(r.message, current),
							},
						],
						primary_action_label: __("Select"),
						primary_action(values) {
							let selected = values.folder;
							let folder_name = "";
							if (selected) {
								let match = options.find((o) => o.value === selected);
								folder_name = match ? match.label : "";
							}
							frm.set_value("contact_folder", selected);
							frm.set_value("contact_folder_name", folder_name);
							frm.dirty();
							frm.save();
							d.hide();
						},
					});

					// Replace the Select with a nicer radio list
					let folder_field = d.fields_dict.folder;
					folder_field.$wrapper.hide();

					let $list = d.fields_dict.folder_list.$wrapper;
					$list.on("click", ".folder-option", function () {
						$list.find(".folder-option").removeClass("selected");
						$(this).addClass("selected");
						folder_field.set_value($(this).data("value"));
					});

					d.show();
				});
			}, __("Settings"));
		}

		// Show clear folder button if a folder is selected
		if (frm.doc.contact_folder) {
			frm.add_custom_button(__("Reset to Default Folder"), () => {
				frm.set_value("contact_folder", "");
				frm.set_value("contact_folder_name", "");
				frm.dirty();
				frm.save();
			}, __("Settings"));
		}
	},
});

function _build_folder_html(folders, current_id) {
	let rows = `<div class="folder-option" data-value=""
		style="padding: 8px 12px; cursor: pointer; border-radius: 4px; margin-bottom: 4px;
		${!current_id ? "background: var(--bg-blue); font-weight: bold;" : ""}">
		📁 Default (Contacts)
	</div>`;

	for (let f of folders) {
		let is_selected = f.id === current_id;
		rows += `<div class="folder-option" data-value="${f.id}"
			style="padding: 8px 12px; cursor: pointer; border-radius: 4px; margin-bottom: 4px;
			${is_selected ? "background: var(--bg-blue); font-weight: bold;" : ""}">
			📂 ${frappe.utils.escape_html(f.name)}
		</div>`;
	}

	return `<style>.folder-option:hover { background: var(--bg-light-gray); }</style>
		<div style="max-height: 300px; overflow-y: auto;">${rows}</div>`;
}
