frappe.ui.form.on("ITSync Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test-SMS senden"), () => {
			frappe.prompt(
				[
					{ fieldname: "to", label: __("Mobilnummer"), fieldtype: "Data", reqd: 1, description: __("z. B. 0151… oder +49151…") },
					{ fieldname: "message", label: __("Text (optional)"), fieldtype: "Small Text" },
				],
				(values) => {
					frm.call({
						method: "send_test_sms", doc: frm.doc, args: { to: values.to, message: values.message },
						freeze: true, freeze_message: __("SMS wird versendet …"),
					}).then((r) => {
						if (r.message && r.message.log) frappe.set_route("Form", "ITSync SMS Log", r.message.log);
					});
				},
				__("Test-SMS senden"), __("Senden")
			);
		}, __("SMS"));

		frm.add_custom_button(__("Guthaben abrufen"), () => {
			frm.call({ method: "get_sms_balance", doc: frm.doc, freeze: true, freeze_message: __("Guthaben wird abgefragt …") })
				.always(() => frm.reload_doc());
		}, __("SMS"));

		frm.add_custom_button(__("SMS-Protokoll"), () => frappe.set_route("List", "ITSync SMS Log"), __("SMS"));

		// --- CardDAV service controls ---
		frm.add_custom_button(__("Radicale starten"), () => {
			frm.call({ method: "start_radicale", doc: frm.doc, freeze: true }).always(() => { frm.reload_doc(); render_carddav_panel(frm); });
		}, __("CardDAV"));
		frm.add_custom_button(__("Radicale stoppen"), () => {
			frm.call({ method: "stop_radicale", doc: frm.doc, freeze: true }).always(() => { frm.reload_doc(); render_carddav_panel(frm); });
		}, __("CardDAV"));
		frm.add_custom_button(__("Diagnose aktualisieren"), () => render_carddav_panel(frm, true), __("CardDAV"));

		render_carddav_panel(frm);
	},
});

function render_carddav_panel(frm, freeze) {
	const wrap = frm.get_field("carddav_diagnostics_html");
	if (!wrap) return;
	wrap.$wrapper.html(`<div class="text-muted">${__("Diagnose wird geladen …")}</div>`);
	frm.call({ method: "get_carddav_diagnostics", doc: frm.doc, freeze: !!freeze, freeze_message: __("Diagnose läuft …") })
		.then((r) => { if (r.message) wrap.$wrapper.html(build_carddav_html(r.message)); })
		.catch(() => wrap.$wrapper.html(`<div class="text-danger">${__("Diagnose fehlgeschlagen")}</div>`));
}

function signing_cell(s) {
	const esc = frappe.utils.escape_html;
	if (!s || !s.configured) return `<span class="text-muted">${__("nicht konfiguriert (Profile unsigniert)")}</span>`;
	if (!s.active) return `<span class="indicator-pill red">${esc(s.error || __("inaktiv"))}</span>`;
	return `<span class="indicator-pill green">${__("aktiv")}</span> ${esc(s.subject || "")} · ${s.days_remaining} ${__("Tage")}`;
}

function build_carddav_html(d) {
	const badge = (ok, y, n) => `<span class="indicator-pill ${ok ? "green" : "red"}">${ok ? y : n}</span>`;
	const rad = d.radicale || {};
	const pub = d.public || {};
	const esc = frappe.utils.escape_html;

	let certRow = "";
	if (d.public_url) {
		if (pub.reachable && pub.not_after) {
			const days = pub.days_remaining;
			const cls = days < 14 ? "red" : days < 30 ? "orange" : "green";
			certRow = `
				<tr><td>${__("Zertifikat gültig bis")}</td><td><span class="indicator-pill ${cls}">${esc(pub.not_after.slice(0, 10))} (${days} ${__("Tage")})</span></td></tr>
				<tr><td>${__("Aussteller")}</td><td>${esc(pub.issuer || "")}</td></tr>
				<tr><td>${__("SAN")}</td><td>${esc((pub.san || []).join(", "))}</td></tr>
				<tr><td>${__("Öffentlich vertraut")}</td><td>${badge(pub.trusted, __("ja"), __("nein (self-signed?)"))}</td></tr>`;
		} else {
			certRow = `<tr><td>${__("Öffentliche URL")}</td><td>${badge(false, "", esc(pub.error || __("nicht erreichbar")))}</td></tr>`;
		}
	}

	const collRows = (d.collections || []).map((c) =>
		`<tr><td>${esc(c.connector)}</td><td>${esc(c.collection || "")}</td><td>${c.vcards}</td></tr>`).join("")
		|| `<tr><td colspan="3" class="text-muted">${__("keine CardDAV-Connectoren")}</td></tr>`;

	return `
	<div style="font-size:13px">
	  <table class="table table-bordered" style="margin-bottom:12px">
		<tr><td style="width:220px">${__("Radicale-Dienst")}</td><td>${badge(rad.running, __("läuft"), __("gestoppt"))} &nbsp; ${rad.enabled ? __("aktiviert") : __("deaktiviert")} · Port ${rad.port}</td></tr>
		<tr><td>${__("CardDAV antwortet")}</td><td>${badge(rad.serving && rad.serving.carddav, __("ja (DAV addressbook)"), (rad.serving && rad.serving.error) || __("nein"))}</td></tr>
		<tr><td>${__("Öffentliche URL")}</td><td>${d.public_url ? `<a href="${esc(d.public_url)}" target="_blank">${esc(d.public_url)}</a>` : `<span class="text-muted">${__("nicht gesetzt (carddav_base_url)")}</span>`}</td></tr>
		${certRow}
		<tr><td>${__("Geräte")}</td><td>${d.devices.active} ${__("aktiv")} · ${d.devices.pending} ${__("ausstehend")} · ${d.devices.revoked} ${__("widerrufen")}</td></tr>
		<tr><td>${__("Profil-Signierung")}</td><td>${signing_cell(d.profile_signing)}</td></tr>
	  </table>
	  <b>${__("Adressbücher (CardDAV)")}</b>
	  <table class="table table-bordered" style="margin-top:6px">
		<thead><tr><th>${__("Connector")}</th><th>${__("Collection")}</th><th>${__("vCards")}</th></tr></thead>
		<tbody>${collRows}</tbody>
	  </table>
	  <div class="text-muted">${__("Geprüft")}: ${frappe.datetime.str_to_user(d.checked_at)}</div>
	</div>`;
}
