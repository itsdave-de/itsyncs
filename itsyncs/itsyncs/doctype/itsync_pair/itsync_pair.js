frappe.ui.form.on("ITSync Pair", {
	refresh(frm) {
		_itsync_render_live_panel(frm, null);
		_itsync_fetch_live_status(frm);
		_itsync_start_polling(frm);

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

		if (frm.doc.on_delete === "Delete") {
			frm.fields_dict.on_delete.set_description(
				__("When a contact is deleted from the source, it will also be <b>removed from the target</b>.")
			);
		} else {
			frm.fields_dict.on_delete.set_description(
				__("When a contact is deleted from the source, it will be <b>kept in the target</b> (mapping marked as orphaned).")
			);
		}
	},

	onload(frm) {
		frm._itsync_poll_timer = null;
	},

	on_hide(frm) {
		_itsync_stop_polling(frm);
	},
});

function _itsync_start_polling(frm) {
	_itsync_stop_polling(frm);
	frm._itsync_poll_timer = setInterval(() => {
		if (!cur_frm || cur_frm !== frm || frm.is_new()) {
			_itsync_stop_polling(frm);
			return;
		}
		_itsync_fetch_live_status(frm);
	}, 5000);
}

function _itsync_stop_polling(frm) {
	if (frm._itsync_poll_timer) {
		clearInterval(frm._itsync_poll_timer);
		frm._itsync_poll_timer = null;
	}
}

function _itsync_fetch_live_status(frm) {
	if (frm.is_new()) return;
	frm.call({
		method: "get_live_status",
		doc: frm.doc,
		callback: (r) => {
			if (!r.message) return;
			_itsync_render_live_panel(frm, r.message);
			_itsync_rebuild_buttons(frm, r.message);
			if (r.message.pair_status !== frm.doc.status) {
				frm.reload_doc();
			}
		},
	});
}

function _itsync_rebuild_buttons(frm, live) {
	frm.clear_custom_buttons();

	const is_running = live.pair_status === "Running" && !live.inconsistent;
	const is_inconsistent = live.inconsistent === true;

	if (!frm.is_new() && !frm.doc.initial_sync_complete && !is_running) {
		frm.add_custom_button(__("Generate Preview"), () => {
			// Pause the 5s live-status poll during the preview; the server
			// call takes 5-30 s and we don't want concurrent get_live_status
			// reloads racing with the preview's db writes.
			_itsync_stop_polling(frm);
			frm.call("generate_preview").always(() => {
				frm.reload_doc();
				_itsync_start_polling(frm);
			});
		}, __("Actions"));
	}

	if (frm.doc.preview_generated_at && !frm.doc.initial_sync_complete && !is_running) {
		frm.add_custom_button(__("Run Initial Sync"), () => {
			frappe.confirm(
				__("This will sync {0} contacts from source to target. Continue?", [frm.doc.preview_to_create]),
				() => {
					frm.call("run_initial_sync").then(() => _itsync_fetch_live_status(frm));
				}
			);
		}, __("Actions"));
	}

	if (frm.doc.initial_sync_complete && !is_running) {
		frm.add_custom_button(__("Sync Now"), () => {
			frm.call("run_manual_sync").then(() => _itsync_fetch_live_status(frm));
		}).addClass("btn-primary-dark");
	}

	// Diagnostics group: ground-truth inspection + smoke tests + cleanup
	if (!frm.is_new() && frm.doc.target) {
		frm.add_custom_button(__("Show Target Contacts"), () => {
			_itsync_show_target_contacts_dialog(frm);
		}, __("Diagnostics"));
		frm.add_custom_button(__("Preflight Check"), () => {
			_itsync_run_preflight(frm);
		}, __("Diagnostics"));
		frm.add_custom_button(__("Run API Smoke Test"), () => {
			_itsync_show_smoke_test_dialog(frm);
		}, __("Diagnostics"));
		frm.add_custom_button(__("Clean Up Test Contacts"), () => {
			_itsync_cleanup_test_contacts(frm);
		}, __("Diagnostics"));
	}

	if (is_inconsistent) {
		frm.add_custom_button(__("Force-clean Stale State"), () => {
			frappe.confirm(
				__("Mark this pair as Error and its log as Failed? Use this if the job is clearly dead but the status is still Running."),
				() => {
					frm.call("force_clean_stale").then(() => {
						frm.reload_doc();
						frappe.show_alert({message: __("Stale state cleaned"), indicator: "green"});
					});
				}
			);
		}, __("Actions")).addClass("btn-warning");
	}
}

function _itsync_render_live_panel(frm, live) {
	let $panel = frm.$wrapper.find(".itsync-live-panel");
	if (!$panel.length) {
		$panel = $('<div class="itsync-live-panel" style="margin: 10px 0;"></div>');
		const mount = (frm.layout && frm.layout.wrapper)
			|| (frm.dashboard && frm.dashboard.parent)
			|| frm.$wrapper;
		$(mount).prepend($panel);
	}

	if (!live) {
		$panel.empty();
		return;
	}

	const running = live.pair_status === "Running" && !live.inconsistent;
	const inconsistent = live.inconsistent === true;

	let html = '';

	if (inconsistent) {
		html += `
			<div class="alert alert-warning" style="margin-bottom: 0;">
				<strong>${__("Inconsistent state detected")}</strong><br>
				${__("Pair is marked as Running, but")}: <code>${frappe.utils.escape_html(live.inconsistency_reason || "unknown")}</code><br>
				<small>${__("The scheduler watchdog will clean this up within 5 minutes, or use the Force-clean button above.")}</small>
			</div>
		`;
	} else if (running) {
		const phase = frappe.utils.escape_html(live.progress_phase || __("Running…"));
		const current = live.progress_current || 0;
		const total = live.progress_total || 0;
		const pct = total > 0 ? Math.floor((current / total) * 100) : 0;
		const heartbeat = live.heartbeat_age_seconds;
		const hbStr = heartbeat !== null && heartbeat !== undefined
			? __("last heartbeat {0}s ago", [heartbeat])
			: __("waiting for worker…");
		const hbClass = heartbeat !== null && heartbeat > 60 ? "text-warning" : "text-muted";
		const rqStatus = live.rq_status ? String(live.rq_status).replace("JobStatus.", "") : "—";

		const logLink = live.last_run_log
			? `<a href="/app/itsync-log/${encodeURIComponent(live.last_run_log)}">${__("Open live log")}</a>`
			: "";

		const progressBar = total > 0
			? `<div class="progress" style="height: 8px; margin: 6px 0;">
				<div class="progress-bar progress-bar-striped progress-bar-animated"
					 role="progressbar" style="width: ${pct}%;"></div>
			   </div>
			   <small>${current}/${total} (${pct}%)</small>`
			: '<div class="text-muted"><i class="fa fa-spinner fa-spin"></i> ' + __("Working…") + '</div>';

		html += `
			<div class="alert alert-info" style="margin-bottom: 0;">
				<div><strong>${__("Sync running")}</strong> &middot; ${phase}</div>
				${progressBar}
				<div style="margin-top: 4px;">
					<small class="${hbClass}">${__("RQ status")}: <code>${rqStatus}</code> &middot; ${hbStr}</small>
					&nbsp;&middot;&nbsp; ${logLink}
				</div>
			</div>
		`;
	} else if (live.log_status && live.pair_status !== "Running") {
		const stat = live.log_status;
		const cls = stat === "Success" ? "alert-success"
			: stat === "Partial" ? "alert-warning"
			: stat === "Failed" ? "alert-danger"
			: "alert-secondary";
		const logLink = live.last_run_log
			? `<a href="/app/itsync-log/${encodeURIComponent(live.last_run_log)}">${__("Open log")}</a>`
			: "";
		const counts = __("{0} created, {1} updated, {2} deleted, {3} skipped, {4} errors",
			[live.created_count || 0, live.updated_count || 0, live.deleted_count || 0,
			 live.skipped_count || 0, live.error_count || 0]);
		html += `
			<div class="alert ${cls}" style="margin-bottom: 0;">
				<strong>${__("Last run")}: ${stat}</strong> &middot; ${counts}
				&nbsp;&middot;&nbsp; ${logLink}
			</div>
		`;
	}

	$panel.html(html);
}

function _itsync_show_target_contacts_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Target Contacts: {0}", [frm.doc.target]),
		size: "extra-large",
		fields: [
			{ fieldname: "info", fieldtype: "HTML" },
			{ fieldname: "list_html", fieldtype: "HTML" },
		],
		primary_action_label: __("Refresh"),
		primary_action: () => _itsync_load_target_contacts(frm, d),
	});
	d.show();
	_itsync_load_target_contacts(frm, d);
}

function _itsync_load_target_contacts(frm, dialog) {
	const $info = dialog.get_field("info").$wrapper;
	const $list = dialog.get_field("list_html").$wrapper;
	$info.html('<div class="text-muted" style="padding: 10px;">'
		+ '<i class="fa fa-spinner fa-spin"></i> '
		+ __("Fetching from Exchange — may take 10–30 s for large tenants…")
		+ '</div>');
	$list.empty();

	const startedAt = Date.now();
	frm.call({
		method: "list_target_contacts",
		doc: frm.doc,
		args: { limit: 500 },
	}).then((r) => {
		if (!r.message) return;
		const data = r.message;
		const tookMs = Date.now() - startedAt;
		$info.html(`
			<div class="alert alert-info" style="margin-bottom: 10px;">
				<div><strong>${frappe.utils.escape_html(data.connector_name)}</strong>
					<span class="text-muted">(${frappe.utils.escape_html(data.connector_type)})</span>
				</div>
				<div>${__("Total contacts in target")}: <strong>${data.total}</strong>
					&middot; ${__("Showing first")} ${data.contacts.length}
					&middot; <small class="text-muted">${__("fetched in {0} s", [(tookMs / 1000).toFixed(1)])}</small>
				</div>
			</div>
		`);
		if (!data.contacts.length) {
			$list.html(`<div class="text-muted" style="padding: 10px;">${__("No contacts found in target.")}</div>`);
			return;
		}
		const headers = ["#", __("Display Name"), __("Email"), __("Company"), __("Job Title"), __("Phone")];
		const rows = data.contacts.map((c, i) => `
			<tr>
				<td class="text-muted" style="width: 40px;">${i + 1}</td>
				<td>${frappe.utils.escape_html(c.display_name)}</td>
				<td><code>${frappe.utils.escape_html(c.email)}</code></td>
				<td>${frappe.utils.escape_html(c.company)}</td>
				<td>${frappe.utils.escape_html(c.job_title)}</td>
				<td>${frappe.utils.escape_html(c.phone)}</td>
			</tr>
		`).join("");
		$list.html(`
			<div style="max-height: 500px; overflow-y: auto; border: 1px solid #e2e6e9; border-radius: 4px;">
				<table class="table table-sm table-striped" style="margin: 0;">
					<thead style="position: sticky; top: 0; background: #f5f7fa;">
						<tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
			</div>
		`);
	}).catch((e) => {
		$info.html(`<div class="alert alert-danger">${__("Error loading contacts")}: ${frappe.utils.escape_html(String(e && e.message || e))}</div>`);
	});
}

function _itsync_cleanup_test_contacts(frm) {
	frappe.confirm(
		__("Scan the target GAL and delete every contact whose email domain is <code>itsync-test.example.com</code> or whose display name starts with <code>[SMOKETEST</code>, <code>[BATCHSMOKE</code>, <code>[BENCH</code>, <code>[CMC-</code> or <code>[POC-</code>. Proceed?"),
		() => {
			// Defer the actual work one tick so the confirm dialog gets a
			// chance to close itself before we open the progress dialog.
			setTimeout(() => _itsync_run_cleanup(frm), 0);
		}
	);
}

function _itsync_run_cleanup(frm) {
	_itsync_stop_polling(frm);

	const dlg = new frappe.ui.Dialog({
		title: __("Cleanup test contacts"),
		size: "large",
		fields: [{fieldname: "html", fieldtype: "HTML"}],
	});
	const $html = dlg.get_field("html").$wrapper;
	$html.html(`<div class="text-muted" style="padding: 10px;">
		<i class="fa fa-spinner fa-spin"></i>
		${__("Scanning target GAL and deleting test contacts… may take 10–30 s.")}
	</div>`);
	dlg.show();

	frm.call({
		method: "cleanup_test_contacts",
		doc: frm.doc,
	}).then((r) => {
		if (!r.message) return;
		const d = r.message;
		const summary = __("Scanned {0} · matched {1} · deleted {2} · failed {3}",
			[d.total_scanned, d.total_matched, d.deleted.length, d.failed.length]);
		const indicator = d.failed.length === 0 ? "green" : "orange";

		if (d.total_matched === 0) {
			$html.html(`<div class="alert alert-info" style="margin: 10px 0;">${__("No test contacts found — target is clean.")}</div>`);
			return;
		}

		const rows = [...d.deleted.map(x => ({ok: true, ...x})), ...d.failed.map(x => ({ok: false, ...x}))]
			.map((x, i) => `
				<tr>
					<td class="text-muted" style="width: 40px;">${i + 1}</td>
					<td style="width: 30px;" class="${x.ok ? 'text-success' : 'text-danger'}">${x.ok ? '✓' : '✗'}</td>
					<td>${frappe.utils.escape_html(x.display_name || '')}</td>
					<td><code>${frappe.utils.escape_html(x.email || '')}</code></td>
					<td><small class="text-muted">${frappe.utils.escape_html(x.error || '')}</small></td>
				</tr>
			`).join("");

		$html.html(`
			<div class="alert alert-${indicator === 'green' ? 'success' : 'warning'}" style="margin-bottom: 10px;">${summary}</div>
			<div style="max-height: 400px; overflow-y: auto; border: 1px solid #e2e6e9; border-radius: 4px;">
				<table class="table table-sm table-striped" style="margin: 0; font-size: 12px;">
					<thead style="position: sticky; top: 0; background: #f5f7fa;">
						<tr><th>#</th><th>OK</th><th>${__("Display Name")}</th><th>${__("Email")}</th><th>${__("Error")}</th></tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
			</div>
		`);
	}).catch((e) => {
		$html.html(`<div class="alert alert-danger" style="margin: 10px 0;">${__("Cleanup failed")}: ${frappe.utils.escape_html(String(e && e.message || e))}</div>`);
	}).always(() => {
		_itsync_start_polling(frm);
	});
}

function _itsync_run_preflight(frm) {
	_itsync_stop_polling(frm);

	const dlg = new frappe.ui.Dialog({
		title: __("Preflight Check: {0}", [frm.doc.title]),
		size: "extra-large",
		fields: [
			{ fieldname: "status", fieldtype: "HTML" },
			{ fieldname: "results", fieldtype: "HTML" },
		],
		primary_action_label: __("Download HTML Report"),
		primary_action: () => {
			if (!dlg._report_html) return;
			const blob = new Blob([dlg._report_html], { type: "text/html;charset=utf-8" });
			const url = URL.createObjectURL(blob);
			const a = document.createElement("a");
			a.href = url;
			a.download = `preflight-${frm.doc.name}-${frappe.datetime.nowdate()}.html`;
			document.body.appendChild(a);
			a.click();
			document.body.removeChild(a);
			URL.revokeObjectURL(url);
		},
	});
	dlg.disable_primary_action();
	dlg.get_field("status").$wrapper.html(`
		<div class="text-muted" style="padding: 10px;">
			<i class="fa fa-spinner fa-spin"></i>
			${__("Fetching source + target contacts and analyzing data quality — may take 10–30 s…")}
		</div>
	`);
	dlg.show();

	frm.call({
		method: "run_preflight_check",
		doc: frm.doc,
	}).then((r) => {
		if (!r.message) return;
		const data = r.message;
		const s = data.summary;
		dlg._report_html = data.html_report;
		dlg.enable_primary_action();

		// Render summary cards
		const cards = Object.entries(s).map(([key, cat]) => {
			const cls = key === "ok" ? "success" : key === "already_in_target" ? "info" : cat.count > 0 ? "danger" : "secondary";
			return `<div style="display: inline-block; margin: 4px; padding: 8px 14px; border-radius: 6px; text-align: center; min-width: 100px; background: var(--bg-${cls === 'secondary' ? 'light-gray' : cls}, #f8f9fa); border: 1px solid #e2e6e9;">
				<div style="font-size: 22px; font-weight: bold;">${cat.count}</div>
				<div style="font-size: 11px; color: #666;">${cat.icon} ${frappe.utils.escape_html(cat.label)}</div>
			</div>`;
		}).join("");

		dlg.get_field("status").$wrapper.html(`
			<div style="margin-bottom: 10px;">
				<strong>${__("Source")}: ${data.source_count}</strong> contacts &middot;
				<strong>${__("Target")}: ${data.target_count}</strong> contacts
			</div>
			<div style="display: flex; flex-wrap: wrap; gap: 6px;">${cards}</div>
		`);

		// Render issue tables
		const issueKeys = Object.entries(s).filter(([k, v]) => k !== "ok" && k !== "already_in_target" && v.count > 0);
		if (issueKeys.length === 0) {
			dlg.get_field("results").$wrapper.html(`
				<div class="alert alert-success" style="margin-top: 10px;">${__("No issues found — all contacts are ready to sync.")}</div>
			`);
		} else {
			// Embed the HTML report in an iframe for preview
			dlg.get_field("results").$wrapper.html(`
				<div style="margin-top: 10px; border: 1px solid #e2e6e9; border-radius: 4px; overflow: hidden;">
					<iframe srcdoc="${frappe.utils.escape_html(data.html_report)}"
						style="width: 100%; height: 500px; border: none;"></iframe>
				</div>
				<div class="text-muted" style="margin-top: 6px; font-size: 12px;">
					${__("Full report preview. Click <b>Download HTML Report</b> to save for offline use or email to the customer.")}
				</div>
			`);
		}
	}).catch((e) => {
		dlg.get_field("status").$wrapper.html(`
			<div class="alert alert-danger">${__("Preflight check failed")}: ${frappe.utils.escape_html(String(e && e.message || e))}</div>
		`);
	}).always(() => {
		_itsync_start_polling(frm);
	});
}

function _itsync_show_smoke_test_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("API Smoke Test: Target = {0}", [frm.doc.target]),
		size: "large",
		fields: [
			{
				fieldname: "intro",
				fieldtype: "HTML",
				options: `
					<div class="alert alert-info" style="margin-bottom: 10px;">
						${__("Creates N test contacts, updates them, then deletes them — each step timed individually. Test contacts use the prefix <code>[SMOKETEST]</code> / <code>[BATCHSMOKE]</code> and email domain <code>itsync-test.example.com</code>.")}
					</div>
					<div class="alert alert-secondary" style="margin-bottom: 10px; font-size: 12px;">
						${__("Capped at <b>7 contacts</b> per run — the synchronous HTTP call must finish within the gunicorn 120 s timeout. For larger benchmark runs, use the CLI:")}
						<code style="display: block; margin-top: 4px;">bench --site &lt;your-site&gt; execute itsyncs.bench.run</code>
					</div>
				`,
			},
			{
				fieldname: "mode",
				fieldtype: "Select",
				label: __("Mode"),
				options: "serial\nbatch",
				default: "serial",
				description: __("<b>serial</b> = current engine pattern (per contact: create + immediate Set-Contact + update + delete). <b>batch</b> = delayed-batch: phase A all creates → wait → phase B all rich Set-Contacts → delete. Batch mirrors the proposed initial-sync rewrite."),
			},
			{
				fieldname: "count",
				fieldtype: "Int",
				label: __("Number of test contacts"),
				default: 3,
				description: __("1–7. Each contact is create + update + delete (3 Graph calls in serial, slightly fewer in batch)."),
			},
			{
				fieldname: "batch_wait",
				fieldtype: "Int",
				label: __("Batch wait (s)"),
				default: 15,
				depends_on: "eval:doc.mode==='batch'",
				description: __("Seconds between phase A (creates) and phase B (set-contact). 10–20 s is a good range."),
			},
			{ fieldname: "status", fieldtype: "HTML" },
			{ fieldname: "results", fieldtype: "HTML" },
		],
		primary_action_label: __("Run Test"),
		primary_action: (values) => _itsync_run_smoke_test(frm, d, values),
	});
	d.show();
}

function _itsync_run_smoke_test(frm, dialog, values) {
	const mode = values.mode || "serial";
	const count = Math.max(1, Math.min(parseInt(values.count || 3, 10), 7));
	const batchWait = Math.max(0, Math.min(parseInt(values.batch_wait || 15, 10), 60));
	const $status = dialog.get_field("status").$wrapper;
	const $results = dialog.get_field("results").$wrapper;

	// Pause the 5s live-status polling while the test runs. Otherwise concurrent
	// run_doc_method calls on the same Pair doc race for row locks and surface
	// as "Lock wait timeout" toasts from Frappe's realtime error feed.
	_itsync_stop_polling(frm);

	dialog.disable_primary_action();
	const etaSeconds = mode === "batch"
		? count * 4 + batchWait + count * 2
		: count * 3 * 5;
	$status.html(`<div class="text-muted" style="padding: 10px;">
		<i class="fa fa-spinner fa-spin"></i>
		${__("Running {0}-mode test with {1} contacts against {2}…", [mode, count, frm.doc.target])}
		${__("Expect roughly {0} s.", [etaSeconds])}
	</div>`);
	$results.empty();

	const startedAt = Date.now();
	frm.call({
		method: "run_api_smoke_test",
		doc: frm.doc,
		args: { count: count, mode: mode, batch_wait: batchWait },
		freeze: false,
	}).then((r) => {
		dialog.enable_primary_action();
		if (!r.message) return;
		const data = r.message;
		const tookMs = Date.now() - startedAt;

		const phaseBadge = (phase, s) => {
			const pct = (s.ok + s.fail) > 0 ? Math.round(s.ok / (s.ok + s.fail) * 100) : 0;
			const cls = s.fail === 0 ? "badge-success" : s.ok === 0 ? "badge-danger" : "badge-warning";
			return `
				<div style="display: inline-block; margin-right: 15px;">
					<strong>${phase}:</strong>
					<span class="badge ${cls}">${s.ok}/${s.ok + s.fail} ok</span>
					<small class="text-muted">avg ${s.avg_ms} ms</small>
				</div>
			`;
		};

		const phases = data.mode === "batch"
			? ["create", "wait", "set", "delete"]
			: ["create", "update", "delete"];
		const phaseLabels = {create: "Create", update: "Update", delete: "Delete", set: "Set-Contact", wait: "Wait"};
		$status.html(`
			<div class="alert alert-secondary" style="margin-bottom: 10px;">
				<div>${__("Run")} <code>${data.run_id}</code> — <b>${data.mode}</b> mode —
					${frappe.utils.escape_html(data.connector_name)}
					(${frappe.utils.escape_html(data.connector_type)}) — ${__("total")} ${(tookMs / 1000).toFixed(1)} s</div>
				<div style="margin-top: 6px;">
					${phases.map(p => phaseBadge(phaseLabels[p], data.summary[p])).join("")}
				</div>
			</div>
		`);

		const phaseColor = { create: "info", update: "primary", delete: "secondary" };
		const rows = data.events.map((e, i) => {
			const icon = e.ok ? '✓' : '✗';
			const cls = e.ok ? "text-success" : "text-danger";
			return `
				<tr>
					<td class="text-muted" style="width: 40px;">${i + 1}</td>
					<td><span class="badge badge-${phaseColor[e.phase] || 'secondary'}">${e.phase}</span></td>
					<td style="width: 40px;">${e.idx}</td>
					<td class="${cls}" style="width: 30px; font-weight: bold;">${icon}</td>
					<td style="width: 80px;">${e.duration_ms} ms</td>
					<td><small><code>${frappe.utils.escape_html(e.detail)}</code></small></td>
				</tr>
			`;
		}).join("");

		$results.html(`
			<div style="max-height: 400px; overflow-y: auto; border: 1px solid #e2e6e9; border-radius: 4px;">
				<table class="table table-sm" style="margin: 0; font-size: 12px;">
					<thead style="position: sticky; top: 0; background: #f5f7fa;">
						<tr>
							<th>#</th><th>${__("Phase")}</th><th>${__("N")}</th>
							<th>${__("OK")}</th><th>${__("ms")}</th><th>${__("Detail")}</th>
						</tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
			</div>
		`);
	}).catch((e) => {
		dialog.enable_primary_action();
		$status.html(`<div class="alert alert-danger">${__("Smoke test failed")}: ${frappe.utils.escape_html(String(e && e.message || e))}</div>`);
	}).always(() => {
		// Restart polling after the test completes (success or failure)
		_itsync_start_polling(frm);
	});
}

if (!frappe._itsync_realtime_bound) {
	frappe._itsync_realtime_bound = true;

	frappe.realtime.on("itsync_sync_complete", (data) => {
		if (!cur_frm || cur_frm.doctype !== "ITSync Pair" || data.pair !== cur_frm.doc.name) return;
		let indicator = data.status === "Success" ? "green"
			: data.status === "Partial" ? "orange" : "red";
		frappe.show_alert({
			message: __("Sync {0}", [data.status]) + (data.reason ? ` (${data.reason})` : ""),
			indicator: indicator,
		}, 10);
		_itsync_fetch_live_status(cur_frm);
	});

	frappe.realtime.on("itsync_sync_progress", (data) => {
		if (!cur_frm || cur_frm.doctype !== "ITSync Pair" || data.pair !== cur_frm.doc.name) return;
		_itsync_fetch_live_status(cur_frm);
	});
}
