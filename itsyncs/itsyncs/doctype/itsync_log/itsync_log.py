import html

import frappe
from frappe.model.document import Document
from frappe.utils import escape_html


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

	duration = ""
	if log.started_at and log.completed_at:
		secs = (log.completed_at - log.started_at).total_seconds()
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

<h2>Dauerhaft übersprungene Kontakte (Konflikte)</h2>
<p class="muted">
  Diese Kontakte werden bei jedem Sync absichtlich übersprungen — sie sind
  im Microsoft 365-Verzeichnis nicht als externe Kontakte anlegbar, weil
  die E-Mail-Adresse bereits einem anderen Recipient gehört oder der Name
  mehrdeutig ist. Änderungen an diesen Quell-Kontakten (Telefon, Firma etc.)
  werden <strong>nicht</strong> in die GAL übertragen, solange der Konflikt besteht.
</p>
{conflict_section}

<h2>Detail-Protokoll (letzte 30 Zeilen)</h2>
{details_html}

<footer>
  Erstellt: {escape_html(str(frappe.utils.now_datetime()))} · Quelle: ITSync Log <code>{escape_html(log.name)}</code>
</footer>

</body>
</html>"""


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


_KIND_EXPLAIN = {
	"ProxyAddressExists": (
		"Die E-Mail-Adresse gehört bereits einem anderen Verzeichnis-Objekt "
		"(meist ein internes Postfach, ein Shared Mailbox oder ein eingeladener Gast). "
		"Ein zusätzlicher MailContact mit derselben SMTP geht in Exchange nicht."
	),
	"AmbiguousIdentity": (
		"Der angezeigte Name kommt im Verzeichnis mehrfach vor — Exchange kann den "
		"neuen MailContact nicht eindeutig anlegen. Eine eindeutige interne "
		"Identity wird bei neuen Kontakten ab jetzt automatisch konstruiert; "
		"bestehende mit dieser Markierung können nach einem Re-Sync verschwinden."
	),
}


def _explain_kind(kind: str) -> str:
	text = _KIND_EXPLAIN.get(kind)
	if not text:
		return ""
	return f'<div class="explain">{escape_html(text)}</div>'
