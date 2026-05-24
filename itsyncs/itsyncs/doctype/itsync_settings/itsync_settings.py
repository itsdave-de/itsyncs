import os

import frappe
from frappe.model.document import Document


# Packaged HTML docs that the Settings page can open in a new tab via a
# whitelisted method. Restricted to a small allow-list to avoid arbitrary
# file reads from the docs folder.
_PACKAGED_DOCS = {
	"kunden-bericht": {
		"filename": "kunden-bericht.html",
		"label": "Kunden-Bericht",
	},
	"massnahmenplan": {
		"filename": "konflikte-massnahmenplan.html",
		"label": "Maßnahmenplan (intern)",
	},
}


class ITSyncSettings(Document):
	@frappe.whitelist()
	def get_packaged_doc(self, name: str) -> str:
		"""Return the raw HTML of a packaged doc by safe name.

		Used by the form JS to open standalone HTML docs in a new tab via a
		Blob URL. Only System Managers; only the documents listed in
		_PACKAGED_DOCS may be returned.
		"""
		frappe.only_for(["System Manager"])

		entry = _PACKAGED_DOCS.get(name)
		if not entry:
			frappe.throw(frappe._("Unbekanntes Dokument: {0}").format(name))

		app_path = frappe.get_app_path("itsyncs")
		docs_root = os.path.normpath(os.path.join(app_path, "..", "docs"))
		path = os.path.normpath(os.path.join(docs_root, entry["filename"]))

		# Defensive: keep us inside docs/
		if not path.startswith(docs_root + os.sep):
			frappe.throw("Ungültiger Pfad")

		try:
			with open(path, "r", encoding="utf-8") as fh:
				return fh.read()
		except FileNotFoundError:
			frappe.throw(frappe._("Dokument nicht gefunden: {0}").format(entry["filename"]))
