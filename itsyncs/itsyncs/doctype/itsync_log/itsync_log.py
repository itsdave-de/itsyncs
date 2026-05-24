import html

import frappe
from frappe.model.document import Document
from frappe.utils import escape_html, get_datetime


class ITSyncLog(Document):
	@frappe.whitelist()
	def get_html_report(self) -> str:
		"""Render a standalone HTML report for this sync log.

		Surfaces what was skipped (especially Conflict-status mappings) so end
		users can understand why a contact they edited didn't make it into the
		target — the contact's source SMTP is owned by another tenant object,
		or its name collides with an existing directory entry, etc.

		Returns the HTML as a string; the form's JS opens it as a Blob URL.
		"""
		return _build_report_html(self)


def _build_report_html(log: "ITSyncLog") -> str:
	conflicts = frappe.get_all(
		"ITSync Mapping",
		filters={"sync_pair": log.sync_pair, "status": "Conflict"},
		fields=["display_name", "source_email", "conflict_kind", "conflict_detail", "last_synced"],
		order_by="conflict_kind asc, display_name asc",
	)
	# Group by conflict_kind for readability
	by_kind: dict[str, list[dict]] = {}
	for c in conflicts:
		by_kind.setdefault(c.conflict_kind or "Unknown", []).append(c)

	# Normalize date fields: when called via frm.call(doc=frm.doc), Frappe
	# rehydrates the doc from JSON and date fields arrive as strings rather
	# than datetime objects. get_datetime accepts both.
	started = get_datetime(log.started_at) if log.started_at else None
	completed = get_datetime(log.completed_at) if log.completed_at else None

	duration = ""
	if started and completed:
		secs = (completed - started).total_seconds()
		if secs < 60:
			duration = f"{secs:.0f} s"
		else:
			duration = f"{int(secs) // 60} min {int(secs) % 60} s"

	status_color = {
		"Success": "#2ea868",
		"Partial": "#d99800",
		"Failed": "#c0392b",
		"Running": "#4a90e2",
	}.get(log.status or "", "#888")

	# Counts box
	counts_html = _render_counts(log)

	# Action lists: what was created / updated during this run
	# (queried from the mapping table; deleted entries are gone and only
	# countable via the details text — see _parse_deleted_from_details).
	actions_html = _build_action_lists(log)

	# Conflicts sections
	if by_kind:
		conflict_html = []
		conflict_html.append(
			f"<p>Insgesamt <strong>{len(conflicts)}</strong> Kontakte werden für diesen Sync "
			f"dauerhaft übersprungen — gruppiert nach Konflikt-Art:</p>"
		)
		for kind, rows in by_kind.items():
			conflict_html.append(f"<h3>{escape_html(kind)} <span class='count'>({len(rows)})</span></h3>")
			conflict_html.append(_explain_kind(kind))
			conflict_html.append("<table class='conflicts'>")
			conflict_html.append("<tr><th>Kontakt</th><th>E-Mail</th><th>Konflikt-Detail</th></tr>")
			for r in rows:
				conflict_html.append(
					"<tr>"
					f"<td>{escape_html(r.display_name or '')}</td>"
					f"<td>{escape_html(r.source_email or '')}</td>"
					f"<td><code>{escape_html(r.conflict_detail or '')}</code></td>"
					"</tr>"
				)
			conflict_html.append("</table>")
		conflict_section = "\n".join(conflict_html)
	else:
		conflict_section = (
			"<p class='muted'>Für dieses Pair gibt es derzeit keine dauerhaft "
			"übersprungenen Kontakte (Status=Conflict).</p>"
		)

	# Details preview (last lines of the log's text trace)
	details_text = (log.details or "").strip()
	if details_text:
		details_preview = "\n".join(details_text.splitlines()[-30:])
		details_html = f"<pre class='details'>{escape_html(details_preview)}</pre>"
	else:
		details_html = "<p class='muted'>Keine Detailzeilen vorhanden.</p>"

	# Assemble
	return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Sync-Report — {escape_html(log.name)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         max-width: 920px; margin: 2em auto; padding: 0 1.2em; color: #1f2630; line-height: 1.55; }}
  h1 {{ border-bottom: 3px solid #2c5aa0; padding-bottom: 0.3em; color: #2c5aa0; font-size: 1.6em; }}
  h2 {{ margin-top: 2em; padding-bottom: 0.3em; border-bottom: 1px solid #d8dee5; color: #2c5aa0; }}
  h3 {{ margin-top: 1.4em; color: #3a4858; font-size: 1.05em; }}
  h3 .count {{ color: #888; font-weight: normal; font-size: 0.9em; }}
  .meta {{ background: #f0f4fa; padding: 0.8em 1.2em; border-radius: 4px; margin: 1em 0; }}
  .meta dt {{ float: left; clear: left; min-width: 130px; color: #4a5260; font-weight: 600; }}
  .meta dd {{ margin: 0 0 0.4em 130px; }}
  .meta dd .status {{ display: inline-block; padding: 0.1em 0.6em; border-radius: 3px;
                       color: white; font-weight: 600; font-size: 0.9em; background: {status_color}; }}
  .counts {{ display: flex; gap: 1em; flex-wrap: wrap; margin: 1em 0 1.5em; }}
  .counts .item {{ flex: 1; min-width: 110px; padding: 0.7em 0.9em;
                    background: #f5f7fa; border-radius: 4px; text-align: center; }}
  .counts .item .n {{ font-size: 1.6em; font-weight: 700; color: #2c5aa0; display: block; }}
  .counts .item.errors .n {{ color: #c0392b; }}
  .counts .item .lbl {{ font-size: 0.85em; color: #6a7280; text-transform: uppercase; letter-spacing: 0.04em; }}
  table.conflicts {{ width: 100%; border-collapse: collapse; margin: 0.5em 0 1em; font-size: 0.95em; }}
  table.conflicts th, table.conflicts td {{ text-align: left; padding: 0.45em 0.7em; border-bottom: 1px solid #e5e9ee; vertical-align: top; }}
  table.conflicts th {{ background: #eef2f7; color: #2c5aa0; }}
  table.conflicts tr:nth-child(even) td {{ background: #fafbfc; }}
  table.conflicts code {{ font-size: 0.85em; background: transparent; padding: 0; }}
  .explain {{ background: #fff9ee; border-left: 4px solid #d99800; padding: 0.6em 1em;
              margin: 0.5em 0 0.8em; border-radius: 0 4px 4px 0; font-size: 0.95em; }}
  .kind-info {{ margin: 0.5em 0 1em; }}
  .kind-info .kind-title {{ margin: 0 0 0.6em; font-size: 1em; color: #6a4900;
                            background: #fff5d6; padding: 0.4em 0.8em; border-radius: 4px;
                            border-left: 3px solid #d99800; }}
  .kind-info .kind-sections {{ display: grid; grid-template-columns: 1fr 1fr 1fr;
                                gap: 0.7em; margin-bottom: 0.7em; }}
  .kind-info .kind-block {{ padding: 0.6em 0.8em; border-radius: 4px;
                             font-size: 0.9em; background: #f8fafb; }}
  .kind-info .kind-block h4 {{ margin: 0 0 0.4em; font-size: 0.78em;
                                color: #6a7280; text-transform: uppercase;
                                letter-spacing: 0.05em; font-weight: 700; }}
  .kind-info .kind-block p, .kind-info .kind-block ol {{ margin: 0; line-height: 1.45; }}
  .kind-info .kind-block ol {{ padding-left: 1.2em; }}
  .kind-info .kind-block ol li {{ margin-bottom: 0.35em; }}
  .kind-info .kind-block.what    {{ background: #f0f6fd; border-left: 3px solid #4a90e2; }}
  .kind-info .kind-block.impact  {{ background: #fff8ea; border-left: 3px solid #d99800; }}
  .kind-info .kind-block.actions {{ background: #f0fbf4; border-left: 3px solid #2ea868; }}
  .intro-box {{ background: #f0f6fd; border-left: 4px solid #4a90e2; padding: 0.9em 1.2em;
                margin: 1em 0 1.5em; border-radius: 0 4px 4px 0; font-size: 0.95em; }}
  .intro-box ul {{ margin: 0.5em 0 0; padding-left: 1.3em; }}
  .tips-box {{ background: #f3f6f3; border: 1px solid #c8d6c8; padding: 1em 1.3em;
                margin: 2em 0 1em; border-radius: 6px; font-size: 0.94em; }}
  .tips-box h3 {{ margin-top: 0; color: #2c5a3a; }}
  .tips-box ul {{ margin: 0; padding-left: 1.3em; }}
  .tips-box li {{ margin-bottom: 0.5em; }}
  @media (max-width: 720px) {{
    .kind-info .kind-sections {{ grid-template-columns: 1fr; }}
  }}
  details.action-list {{ margin: 0.6em 0 1em; border: 1px solid #d8dee5;
                          border-radius: 4px; padding: 0.6em 0.9em; background: #fafbfc; }}
  details.action-list[open] {{ background: #fff; }}
  details.action-list summary {{ cursor: pointer; padding: 0.3em 0; font-size: 1em; }}
  details.action-list summary strong {{ color: #2c5aa0; }}
  details.action-list .action-count {{ color: #888; font-weight: normal; font-size: 0.9em; }}
  details.action-list.created summary strong {{ color: #1b6e3d; }}
  details.action-list.updated summary strong {{ color: #8a5500; }}
  details.action-list.deleted summary strong {{ color: #8a2d2d; }}
  table.action-table {{ width: 100%; border-collapse: collapse; margin: 0.5em 0; font-size: 0.92em; }}
  table.action-table th, table.action-table td {{ text-align: left; padding: 0.35em 0.7em;
                                                    border-bottom: 1px solid #eef0f3; }}
  table.action-table th {{ color: #6a7280; font-weight: 600; }}
  table.action-table tr:nth-child(even) td {{ background: #fafbfc; }}
  pre.details {{ background: #f5f7fa; padding: 0.8em 1em; border-radius: 4px;
                  max-height: 360px; overflow-y: auto; font-size: 0.85em;
                  font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
  .muted {{ color: #6a7280; font-size: 0.95em; }}
  footer {{ margin-top: 3em; padding-top: 1em; border-top: 1px solid #e5e9ee;
            color: #6a7280; font-size: 0.85em; }}
  @media print {{ body {{ font-size: 11pt; max-width: none; }}
                  .counts {{ page-break-inside: avoid; }}
                  table.conflicts {{ page-break-inside: avoid; }} }}
</style>
</head>
<body>

<h1>Sync-Report</h1>

<div class="intro-box">
<strong>Wozu dieser Bericht?</strong><br>
Er erklärt, was der letzte Sync-Lauf für diesen Pair gemacht hat — und vor allem,
<strong>welche Kontakte bewusst übersprungen wurden</strong>. Das ist wichtig zu wissen,
falls Sie einen Kontakt geändert haben und sich wundern, warum die Änderung in Outlook
oder anderen Anwendungen nicht erscheint. Suchen Sie unten den Kontakt in der
„Übersprungen"-Liste — daneben steht der Grund <em>und</em> was Sie ggf. tun können.
</div>

<div class="meta">
<dl>
  <dt>Pair:</dt><dd>{escape_html(log.sync_pair or '')}</dd>
  <dt>Typ:</dt><dd>{escape_html(log.sync_type or '')}</dd>
  <dt>Status:</dt><dd><span class="status">{escape_html(log.status or '')}</span></dd>
  <dt>Gestartet:</dt><dd>{escape_html(str(log.started_at or '—'))}</dd>
  <dt>Abgeschlossen:</dt><dd>{escape_html(str(log.completed_at or '—'))}</dd>
  <dt>Dauer:</dt><dd>{escape_html(duration or '—')}</dd>
  <dt>Log-ID:</dt><dd><code>{escape_html(log.name)}</code></dd>
</dl>
</div>

<h2>Zusammenfassung dieses Laufs</h2>
{counts_html}

{actions_html}

<h2>Dauerhaft übersprungene Kontakte (Konflikte)</h2>
<p class="muted">
  Diese Kontakte werden bei jedem Sync bewusst übersprungen, weil ein
  <em>strukturelles</em> Hindernis das Anlegen im Microsoft-365-Verzeichnis
  verhindert. Das ist <strong>kein Sync-Bug</strong> — und auch nichts, was
  durch häufigeres Syncen oder Wiederholungen verschwindet. Für jede
  Konflikt-Art ist unten erklärt, was passiert ist, was es bedeutet, und
  was — falls überhaupt — jemand tun kann.
</p>
{conflict_section}

<div class="tips-box">
<h3>Tipps aus der Praxis</h3>
<ul>
  <li><strong>Wenn eine Änderung an einem Kontakt nicht in Outlook erscheint:</strong>
      zuerst hier nach dem Kontakt suchen. Wenn er als Konflikt gelistet ist, sind
      die Änderungen an die GAL <em>nicht</em> übertragen worden — der Grund steht daneben.</li>
  <li><strong>Conflict-Mappings sind „eingefroren":</strong> einmal markiert, werden sie
      bei jedem Lauf übersprungen — auch wenn sich die Konflikt-Ursache aufgelöst hat.
      Damit der Kontakt neu versucht wird, das Conflict-Mapping in
      <code>ITSync Mapping</code> manuell löschen.</li>
  <li><strong>Die Quell-Adresse ist oft die einfachere Korrektur:</strong> wenn ein
      Konflikt mit einem internen Mitarbeiter passiert, liegt das meistens daran, dass
      die Person im externen Adressbuch ohnehin nicht hingehört — sie steht über ihr
      eigenes Konto schon in der GAL.</li>
  <li><strong>Wenn die SMTP-Adresse vermutlich falsch ist:</strong> es gab Fälle, in denen
      eine Adresse im Quell-Adressbuch eindeutig vergeben war (z. B. <code>r.brandau@</code>),
      aber tatsächlich einer anderen Person gehört. Konflikt-Detail nennt das tatsächliche
      Objekt — wenn das nicht zur Quell-Person passt, ist die Quell-Adresse falsch eingepflegt.</li>
  <li><strong>Microsoft-Replikation kann verzögern:</strong> nach Änderungen am Tenant
      (Konto-Löschung, Adressbuch-Bereinigung) ein paar Minuten warten, bevor das
      Conflict-Mapping gelöscht wird. Sonst kann der Re-Try noch in den alten Konflikt laufen.</li>
  <li><strong>Bei wiederholten transienten Fehlern</strong> (Status „Failed" / „Partial"
      mit hohem <em>errors</em>-Count): meist Microsoft-throttling — der nächste Lauf
      bügelt das aus. Bleibt der Zustand über mehrere Läufe, IT-Support kontaktieren.</li>
</ul>
</div>

<h2>Detail-Protokoll (letzte 30 Zeilen)</h2>
{details_html}

<footer>
  Erstellt: {escape_html(str(frappe.utils.now_datetime()))} · Quelle: ITSync Log <code>{escape_html(log.name)}</code>
</footer>

</body>
</html>"""


_MAX_LIST_ROWS = 200
_DELETED_RE = None  # lazy-initialised regex (built on first use)


def _build_action_lists(log) -> str:
	"""Build HTML sections for what this run created, updated and deleted.

	created/updated come from the Mapping table (reliable for the most recent
	run; older logs may show partial data if mappings were touched again later).
	deleted is parsed from the log's details text — opaque source_id only,
	since the mapping is gone by the time we'd want to read display_name.
	"""
	if not log.started_at:
		return ""

	window_end = log.completed_at or frappe.utils.now_datetime()

	created = frappe.db.sql(
		"""
		SELECT display_name, source_email, creation
		FROM `tabITSync Mapping`
		WHERE sync_pair = %s
		  AND status = "Synced"
		  AND creation BETWEEN %s AND %s
		ORDER BY creation ASC
		LIMIT %s
		""",
		(log.sync_pair, log.started_at, window_end, _MAX_LIST_ROWS + 1),
		as_dict=True,
	)
	updated = frappe.db.sql(
		"""
		SELECT display_name, source_email, last_synced
		FROM `tabITSync Mapping`
		WHERE sync_pair = %s
		  AND status = "Synced"
		  AND last_synced BETWEEN %s AND %s
		  AND creation < %s
		ORDER BY last_synced ASC
		LIMIT %s
		""",
		(log.sync_pair, log.started_at, window_end, log.started_at, _MAX_LIST_ROWS + 1),
		as_dict=True,
	)

	# Deleted: parse opaque source_ids from the details text (ring-buffered to
	# 200 lines, so the list can be incomplete for very large runs).
	global _DELETED_RE
	if _DELETED_RE is None:
		import re
		_DELETED_RE = re.compile(r"deleted mapping for source_id (\S+)")
	deleted_ids = _DELETED_RE.findall(log.details or "") if log.details else []

	parts = []
	if created or updated or deleted_ids:
		parts.append("<h2>Was hat dieser Lauf geändert?</h2>")

	if created:
		parts.append(_render_action_list(
			"Neu angelegt",
			"created",
			"Diese Kontakte sind beim ersten Mal im Ziel erschienen. Wer sie in Outlook gesucht hat, findet sie ab sofort.",
			created,
		))

	if updated:
		parts.append(_render_action_list(
			"Aktualisiert",
			"updated",
			"An diesen Kontakten haben sich Felder geändert (z. B. Telefonnummer, Firma, E-Mail) — die Änderungen sind im Ziel angekommen.",
			updated,
		))

	if deleted_ids:
		count = len(deleted_ids)
		note = (
			"Bei jedem Lauf können Kontakte gelöscht werden, wenn die Quelle sie nicht "
			"mehr enthält und das Pair auf <code>on_delete=Delete</code> steht. "
			"Die konkreten Namen sind hier nicht mehr verfügbar — sehen Sie ggf. im "
			"Detail-Protokoll am Ende dieses Berichts nach den entsprechenden Zeilen."
		)
		parts.append(
			'<details class="action-list deleted" open>'
			f'<summary><strong>Gelöscht</strong> <span class="action-count">({count})</span></summary>'
			f'<p class="muted">{note}</p>'
			'</details>'
		)

	return "\n".join(parts)


def _render_action_list(label: str, css_class: str, intro: str, rows: list[dict]) -> str:
	truncated = len(rows) > _MAX_LIST_ROWS
	visible = rows[:_MAX_LIST_ROWS]
	open_attr = " open" if len(rows) <= 25 else ""
	html = [
		f'<details class="action-list {css_class}"{open_attr}>',
		f'<summary><strong>{escape_html(label)}</strong> '
		f'<span class="action-count">({len(rows)})</span></summary>',
		f'<p class="muted">{escape_html(intro)}</p>',
		'<table class="action-table">',
		'<tr><th>Kontakt</th><th>E-Mail</th></tr>',
	]
	for r in visible:
		html.append(
			"<tr>"
			f"<td>{escape_html(r.get('display_name') or '')}</td>"
			f"<td>{escape_html(r.get('source_email') or '')}</td>"
			"</tr>"
		)
	html.append("</table>")
	if truncated:
		html.append(
			f'<p class="muted">… {len(rows) - _MAX_LIST_ROWS} weitere ausgeblendet '
			f'(nur die ersten {_MAX_LIST_ROWS} werden angezeigt).</p>'
		)
	html.append("</details>")
	return "\n".join(html)


def _render_counts(log) -> str:
	items = [
		("created", "Erstellt", "create"),
		("updated", "Aktualisiert", "update"),
		("deleted", "Gelöscht", "delete"),
		("skipped", "Übersprungen", "skip"),
		("errors", "Fehler", "errors"),
	]
	parts = ['<div class="counts">']
	for field_suffix, label, css_class in items:
		val = getattr(log, f"{field_suffix}_count", 0) or 0
		parts.append(
			f'<div class="item {css_class}">'
			f'<span class="n">{int(val)}</span>'
			f'<span class="lbl">{escape_html(label)}</span>'
			f'</div>'
		)
	parts.append("</div>")
	return "\n".join(parts)


_KIND_INFO = {
	"ProxyAddressExists": {
		"title": "E-Mail-Adresse ist bereits im Tenant vergeben",
		"what": (
			"Die E-Mail-Adresse dieses Kontakts gehört bereits einem anderen Objekt im "
			"Microsoft 365-Tenant — meistens einem internen Postfach (Mitarbeiter), einem "
			"Shared Mailbox (z. B. <code>archiv@…</code>) oder einer Person, die bereits "
			"als externer Gast eingeladen wurde."
		),
		"impact": (
			"<strong>Das ist kein Fehler.</strong> Der Kontakt ist über diesen anderen Weg "
			"in der GAL bereits präsent — Sie finden ihn in der Outlook-Adresssuche ganz "
			"normal, nur nicht als separat gepflegten externen Kontakt. Änderungen, die Sie "
			"jetzt am Quell-Kontakt vornehmen (Telefon, Firma etc.), wirken sich daher "
			"<strong>nicht</strong> auf das, was in Outlook erscheint, aus."
		),
		"actions": [
			"In den meisten Fällen: nichts tun. Die Person erreichen Sie über ihr eigenes Konto bzw. die Gast-Einladung.",
			"Falls die E-Mail im Quell-Adressbuch falsch ist (Tippfehler — z. B. wenn Konflikt-Detail auf ein anderes Objekt zeigt als erwartet): im Adressbuch korrigieren, danach das Conflict-Mapping löschen.",
			"Wenn der interne Mitarbeiter das Unternehmen verlässt und der Konflikt sich auflöst: Conflict-Mapping löschen — beim nächsten Sync wird der externe Eintrag neu angelegt.",
		],
	},
	"AmbiguousIdentity": {
		"title": "Name kollidiert mehrdeutig im Verzeichnis",
		"what": (
			"Beim ersten Anlegen kam der Name dieses Kontakts mehrdeutig im Verzeichnis "
			"vor (z. B. zwei verschiedene „Markus Schwarz“ von unterschiedlichen Firmen). "
			"Exchange konnte das nicht eindeutig auflösen."
		),
		"impact": (
			"Seit Mai 2026 generiert die Sync-Engine eindeutige Identifier automatisch aus "
			"der SMTP-Adresse — solche Konflikte entstehen <strong>neu nicht mehr</strong>."
		),
		"actions": [
			"Alte Conflict-Mappings dieser Art können risikofrei gelöscht werden — beim nächsten Sync wird der Kontakt mit dem neuen Verfahren erfolgreich angelegt.",
		],
	},
	"InvalidEmailAddress": {
		"title": "Die hinterlegte E-Mail-Adresse ist syntaktisch ungültig",
		"what": (
			"Die Adresse enthält Zeichen, die in einer SMTP-Adresse nichts zu suchen haben — "
			"typische Kandidaten: <code>?</code> oder <code>&gt;</code> innerhalb der Adresse, "
			"führende/folgende Leerzeichen, mehrfache <code>@</code>. Meist ein Copy-Paste-Schaden "
			"beim Pflegen des Kontakts."
		),
		"impact": (
			"Microsoft Exchange lehnt das Anlegen ab — der Kontakt erscheint <strong>nicht</strong> "
			"in der GAL. Alles, was am Quell-Kontakt gepflegt wird, bleibt unsichtbar."
		),
		"actions": [
			"Öffnen Sie den Kontakt im geteilten Adressbuch (firmenkontakte@…) und bereinigen Sie die E-Mail-Adresse (Sonderzeichen entfernen).",
			"Anschließend das Conflict-Mapping löschen — beim nächsten Sync wird der Kontakt sauber angelegt.",
		],
	},
	"SoftDeletedRecipient": {
		"title": "Die SMTP-Adresse ist von einem gelöschten Konto reserviert",
		"what": (
			"Im Microsoft 365-Tenant existiert ein „soft-deleted“ Konto (z. B. ehemaliger "
			"Mitarbeiter), dessen E-Mail-Adresse noch reserviert ist. Solange diese Reservierung "
			"besteht, kann <em>keine</em> andere Person — auch nicht als externer MailContact — "
			"diese Adresse beanspruchen."
		),
		"impact": (
			"Der Kontakt erscheint nicht in der GAL, bis ein Exchange-Administrator den "
			"reservierten Eintrag freigibt."
		),
		"actions": [
			"<strong>Exchange-Administrator</strong>: Im Admin Center oder per PowerShell prüfen: <code>Get-User -SoftDeletedUser | ?{ $_.EmailAddresses -like \"*&lt;localpart&gt;*\" }</code>",
			"Soft-Delete-Eintrag dauerhaft entfernen (<code>Remove-MsolUser -RemoveFromRecycleBin</code>) oder die Proxy-Adresse freigeben.",
			"Anschließend das Conflict-Mapping in ITSync Mapping löschen — beim nächsten Sync wird neu versucht.",
		],
	},
}


def _render_kind_info(kind: str) -> str:
	info = _KIND_INFO.get(kind)
	if not info:
		return ""
	actions_html = "".join(f"<li>{a}</li>" for a in info["actions"])
	return (
		'<div class="kind-info">'
		f'<p class="kind-title">{escape_html(info["title"])}</p>'
		'<div class="kind-sections">'
		f'<div class="kind-block what"><h4>Was ist passiert?</h4><p>{info["what"]}</p></div>'
		f'<div class="kind-block impact"><h4>Was bedeutet das?</h4><p>{info["impact"]}</p></div>'
		f'<div class="kind-block actions"><h4>Was kann ich tun?</h4><ol>{actions_html}</ol></div>'
		'</div>'
		'</div>'
	)


# Renamed for backwards-compatibility: still callable as _explain_kind in tests.
_explain_kind = _render_kind_info
