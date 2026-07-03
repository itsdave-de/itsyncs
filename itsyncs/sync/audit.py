"""Ziel-Bestandsabgleich (Konsistenz-Audit).

Vergleicht pro aktiviertem Pair die ITSync Mappings mit dem tatsächlichen
Quell- und Ziel-Bestand und findet Drift, den der normale Delta-Sync nicht
sehen kann:

  - Verwaiste Mappings: Ziel-Kontakt existiert nicht mehr (extern gelöscht
	oder umbenannt — die GAL-Identity ist namensbasiert und ändert sich beim
	Umbenennen). Heilung: Mapping entfernen, der nächste Reconcile legt den
	Kontakt neu an.
  - Verpasste Löschungen: Quell-Kontakt weg, aber Mapping+Ziel leben noch
	(Graph-Delta ist best-effort und kann Löschungen verschlucken).
	Heilung: Ziel löschen — respektiert das Lösch-Limit aus den Settings.
  - Doppel-Zuordnungen und unverwaltete Ziel-Einträge: nur Meldung, nie
	automatischer Eingriff.

Jeder Lauf schreibt pro Pair einen ITSync Log (Sync Type "Audit") und eine
Einzeiler-Zusammenfassung in die ITSync Settings.
"""

from collections import Counter

import frappe

from itsyncs.sync.engine import (
	_append_log_lines,
	_delete_mapping,
	_fetch_source_contacts,
	_fetch_target_contacts,
	_get_client_for_connector,
	_get_tenant_for_connector,
	_target_delete,
	_timestamp,
)

MAX_EXAMPLES = 6


def run_audit(triggered_by: str = "Scheduler") -> dict:
	settings = frappe.get_single("ITSync Settings")
	autoheal = bool(settings.get("audit_autoheal"))
	pair_names = frappe.get_all(
		"ITSync Pair", filters={"enabled": 1, "initial_sync_complete": 1}, pluck="name",
	)

	results = []
	for name in pair_names:
		try:
			results.append(_audit_pair(name, autoheal))
		except Exception as e:
			frappe.log_error(f"ITSync Audit failed for {name}", frappe.get_traceback())
			results.append({"pair": name, "findings": -1, "healed": 0, "error": str(e)[:120]})
		frappe.db.commit()

	total = sum(r["findings"] for r in results if r["findings"] > 0)
	healed = sum(r["healed"] for r in results)
	failed = [r["pair"] for r in results if r["findings"] < 0]

	if failed:
		result = f"{len(results)} Pairs, FEHLER bei: {', '.join(failed)}"
	elif total == 0:
		result = f"{len(results)} Pairs geprüft — keine Abweichungen"
	else:
		rest = total - healed
		result = f"{len(results)} Pairs, {total} Abweichung(en), {healed} bereinigt" + (
			f", {rest} offen (siehe Audit-Logs)" if rest else ""
		)

	settings.db_set("audit_last_run", frappe.utils.now_datetime(), update_modified=False)
	settings.db_set("audit_last_result", f"{triggered_by}: {result}", update_modified=False)
	frappe.db.commit()
	return {"pairs": len(results), "findings": total, "healed": healed, "result": result}


def _audit_pair(pair_name: str, autoheal: bool) -> dict:
	pair = frappe.get_doc("ITSync Pair", pair_name)
	source_conn = frappe.get_doc("ITSync Connector", pair.source)
	target_conn = frappe.get_doc("ITSync Connector", pair.target)

	log = frappe.new_doc("ITSync Log")
	log.sync_pair = pair_name
	log.sync_type = "Audit"
	log.started_at = frappe.utils.now_datetime()
	log.status = "Running"
	log.insert(ignore_permissions=True)
	frappe.db.commit()

	source_client = _get_client_for_connector(source_conn)
	target_client = _get_client_for_connector(target_conn)
	target_tenant = _get_tenant_for_connector(target_conn)

	source_ids = {c["id"] for c in _fetch_source_contacts(source_client, source_conn) if c.get("id")}
	target_ids = {c["id"] for c in _fetch_target_contacts(target_client, target_conn, target_tenant) if c.get("id")}

	mappings = frappe.get_all(
		"ITSync Mapping",
		filters={"sync_pair": pair_name},
		fields=["name", "source_id", "target_id", "status", "display_name"],
	)
	synced = [m for m in mappings if m.status == "Synced"]
	conflicts = sum(1 for m in mappings if m.status == "Conflict")

	orphans = [m for m in synced if m.target_id and m.target_id not in target_ids]
	source_gone = [m for m in synced if m.source_id not in source_ids and m not in orphans]
	dup_counter = Counter(m.target_id for m in synced if m.target_id)
	duplicates = {t: n for t, n in dup_counter.items() if n > 1}
	mapped_targets = {m.target_id for m in synced if m.target_id}
	unmanaged = len(target_ids - mapped_targets)
	mapped_sources = {m.source_id for m in mappings}
	unmapped_source = len(source_ids - mapped_sources)

	lines = [
		f"[{_timestamp()}] Audit {pair_name}: Quelle {len(source_ids)} | Ziel {len(target_ids)} "
		f"| Mappings {len(synced)} synced, {conflicts} conflict",
	]
	counts = {"deleted": 0, "errors": 0}
	healed = 0

	if orphans:
		names = ", ".join(m.display_name or m.source_id for m in orphans[:MAX_EXAMPLES])
		more = f" (+{len(orphans) - MAX_EXAMPLES} weitere)" if len(orphans) > MAX_EXAMPLES else ""
		if autoheal:
			for m in orphans:
				_delete_mapping(m.name)
			healed += len(orphans)
			lines.append(
				f"[{_timestamp()}] Verwaiste Mappings (Ziel fehlt): {len(orphans)} — entfernt, "
				f"Neuanlage beim nächsten Sync: {names}{more}"
			)
		else:
			lines.append(f"[{_timestamp()}] Verwaiste Mappings (Ziel fehlt): {len(orphans)}: {names}{more}")

	if source_gone:
		names = ", ".join(m.display_name or m.source_id for m in source_gone[:MAX_EXAMPLES])
		more = f" (+{len(source_gone) - MAX_EXAMPLES} weitere)" if len(source_gone) > MAX_EXAMPLES else ""
		threshold = _delete_threshold()
		if autoheal and pair.on_delete == "Delete" and (threshold <= 0 or len(source_gone) <= threshold):
			ok = 0
			for m in source_gone:
				try:
					_target_delete(target_conn, target_tenant, m.target_id)
					_delete_mapping(m.name)
					ok += 1
				except Exception as e:
					counts["errors"] += 1
					lines.append(f"[{_timestamp()}] FEHLER beim Nachziehen der Löschung {m.display_name}: {str(e)[:100]}")
			counts["deleted"] = ok
			healed += ok
			lines.append(f"[{_timestamp()}] Verpasste Löschungen (Quelle fehlt): {len(source_gone)} — {ok} im Ziel nachgezogen: {names}{more}")
		elif autoheal and pair.on_delete == "Delete":
			lines.append(
				f"[{_timestamp()}] Verpasste Löschungen: {len(source_gone)} > Lösch-Limit {threshold} — "
				f"NICHT bereinigt, bitte prüfen: {names}{more}"
			)
		else:
			lines.append(f"[{_timestamp()}] Verpasste Löschungen (Quelle fehlt): {len(source_gone)}: {names}{more}")

	if duplicates:
		ex = "; ".join(f"{t} ×{n}" for t, n in list(duplicates.items())[:MAX_EXAMPLES])
		lines.append(f"[{_timestamp()}] Doppel-Zuordnung auf gleiches Ziel: {len(duplicates)} (Info): {ex}")

	if unmanaged:
		lines.append(f"[{_timestamp()}] Ziel-Einträge ohne Mapping: {unmanaged} (Info, kein Eingriff)")
	if unmapped_source:
		lines.append(f"[{_timestamp()}] Quell-Kontakte ohne Mapping: {unmapped_source} (übernimmt der nächste Sync-Lauf)")

	findings = len(orphans) + len(source_gone) + len(duplicates)
	if findings == 0:
		lines.append(f"[{_timestamp()}] OK — keine Abweichungen")

	_append_log_lines(log.name, lines)
	frappe.db.set_value("ITSync Log", log.name, {
		"status": "Success" if findings == 0 or (findings == healed and not counts["errors"]) else "Partial",
		"completed_at": frappe.utils.now_datetime(),
		"deleted_count": counts["deleted"],
		"error_count": counts["errors"],
		"progress_phase": f"Audit: {findings} Abweichung(en), {healed} bereinigt",
	}, update_modified=False)

	return {"pair": pair_name, "findings": findings, "healed": healed}


def _delete_threshold() -> int:
	raw = frappe.db.get_single_value("ITSync Settings", "delete_threshold_per_run")
	return 25 if raw in (None, "") else frappe.utils.cint(raw)
