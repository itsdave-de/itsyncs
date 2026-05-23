frappe.ui.form.on("ITSync Log", {
	refresh(frm) {
		_itsync_log_render_details(frm);
		_itsync_log_render_indicator(frm);
		_itsync_log_toggle_polling(frm);
		_itsync_log_add_report_button(frm);
	},

	status(frm) {
		_itsync_log_toggle_polling(frm);
	},

	onload(frm) {
		frm._itsync_log_poll_timer = null;
	},

	on_hide(frm) {
		_itsync_log_stop_polling(frm);
	},
});

function _itsync_log_add_report_button(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status === "Running") return;
	frm.add_custom_button(__("Bericht öffnen"), () => {
		frm.call({
			method: "get_html_report",
			doc: frm.doc,
			freeze: true,
			freeze_message: __("Bericht wird erstellt …"),
		}).then((r) => {
			if (!r.message) return;
			const blob = new Blob([r.message], { type: "text/html;charset=utf-8" });
			const url = URL.createObjectURL(blob);
			const win = window.open(url, "_blank");
			// Release the object URL once the new window has had a chance to load
			setTimeout(() => URL.revokeObjectURL(url), 60000);
			if (!win) {
				frappe.msgprint(__("Popup blockiert — bitte Popups für dieses Site erlauben."));
			}
		});
	});
}

function _itsync_log_toggle_polling(frm) {
	_itsync_log_stop_polling(frm);
	if (frm.doc.status === "Running") {
		frm._itsync_log_poll_timer = setInterval(() => {
			if (!cur_frm || cur_frm !== frm) {
				_itsync_log_stop_polling(frm);
				return;
			}
			frm.reload_doc();
		}, 5000);
	}
}

function _itsync_log_stop_polling(frm) {
	if (frm._itsync_log_poll_timer) {
		clearInterval(frm._itsync_log_poll_timer);
		frm._itsync_log_poll_timer = null;
	}
}

function _itsync_log_render_details(frm) {
	if (!frm.fields_dict.details || !frm.fields_dict.details.$wrapper) return;

	const $wrapper = frm.fields_dict.details.$wrapper;
	// Replace the textarea with a preformatted view while keeping the underlying field intact
	let $pre = $wrapper.find(".itsync-log-tail");
	if (!$pre.length) {
		$pre = $(`<pre class="itsync-log-tail" style="max-height: 400px; overflow-y: auto; background: #f8f9fa; padding: 10px; border-radius: 4px; font-size: 12px; line-height: 1.4;"></pre>`);
		$wrapper.find(".control-input-wrapper").hide();
		$wrapper.append($pre);
	}

	const text = frm.doc.details || __("(no details yet)");
	$pre.text(text);

	// Auto-scroll to bottom when running
	if (frm.doc.status === "Running") {
		$pre.scrollTop($pre[0].scrollHeight);
	}
}

function _itsync_log_render_indicator(frm) {
	const status = frm.doc.status;
	const map = {
		"Running": "blue",
		"Success": "green",
		"Partial": "orange",
		"Failed": "red",
	};
	const color = map[status] || "gray";
	frm.dashboard.clear_headline();
	if (status === "Running") {
		const phase = frm.doc.progress_phase || __("Running");
		let progressText = "";
		if (frm.doc.progress_total) {
			const pct = Math.floor((frm.doc.progress_current || 0) / frm.doc.progress_total * 100);
			progressText = ` — ${frm.doc.progress_current || 0}/${frm.doc.progress_total} (${pct}%)`;
		}
		frm.dashboard.set_headline(
			`<span class="indicator ${color}">${__("Running")}: ${frappe.utils.escape_html(phase)}${progressText}</span>`
		);
	}
}
