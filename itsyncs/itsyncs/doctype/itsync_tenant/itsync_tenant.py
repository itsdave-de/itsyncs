import frappe
from frappe.model.document import Document


class ITSyncTenant(Document):
	def validate(self):
		self.test_connection()

	@frappe.whitelist()
	def test_connection(self):
		from itsyncs.graph.client import get_graph_client, run_async

		results = []

		# 1. Test Graph API
		try:
			client = get_graph_client(self)
			org_result = run_async(client.organization.get())

			if org_result and org_result.value:
				org_name = org_result.value[0].display_name
				results.append(("Graph API", "green", f"Connected — Organization: {org_name}"))
			else:
				results.append(("Graph API", "red", "Could not retrieve organization info"))
		except Exception as e:
			results.append(("Graph API", "red", f"Failed: {e!s}"))

		# 2. Test Graph API — list mailboxes (fresh client to avoid event loop issues)
		try:
			from itsyncs.graph.client import get_graph_token

			import httpx

			token = get_graph_token(self)
			url = (
				"https://graph.microsoft.com/v1.0/users"
				"?$select=displayName,mail,userPrincipalName,accountEnabled"
				"&$top=999"
			)
			resp = httpx.get(
				url,
				headers={"Authorization": f"Bearer {token}"},
				timeout=30,
			)
			resp.raise_for_status()
			users = resp.json().get("value", [])

			mailboxes = []
			for u in users:
				mail = u.get("mail")
				if not mail or not u.get("accountEnabled"):
					continue
				name = u.get("displayName") or mail
				mailboxes.append(f"{name} ({mail})")

			if mailboxes:
				summary = f"{len(mailboxes)} Mailboxes"
				details = "<br>".join(f"• {m}" for m in sorted(mailboxes))
				results.append(("Mailboxes", "blue", f"{summary}:<br>{details}"))
			else:
				results.append(("Mailboxes", "orange", "No mailboxes found — check User.Read.All permission"))
		except Exception as e:
			results.append(("Mailboxes", "orange", f"Could not list mailboxes: {e!s}"))

		# 3. Test Exchange Online (InvokeCommand API) — optional, only for GAL write access
		try:
			from itsyncs.graph.exchange import get_exchange_token, _invoke_command

			get_exchange_token(self)
			mail_contacts = _invoke_command(self, "Get-MailContact", {"ResultSize": "1"})
			contact_count = "accessible"
			results.append(("Exchange Online", "green", f"Connected — Mail Contacts {contact_count}"))
		except Exception as e:
			err = str(e)
			results.append(("Exchange Online", "orange",
				f"Not available (only needed for GAL write access): {err[:150]}"))

		# Set connection status — only Graph API failures are critical
		has_error = any(color == "red" for _, color, _ in results)
		self.connection_status = "Failed" if has_error else "Connected"
		self.last_validated = frappe.utils.now_datetime()

		# Build output message
		msg_parts = []
		for label, color, detail in results:
			indicator = {"green": "🟢", "blue": "🔵", "orange": "🟠", "red": "🔴"}.get(color, "⚪")
			msg_parts.append(
				f'<div style="margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px solid var(--border-color);">'
				f"<b>{indicator} {label}</b><br>{detail}</div>"
			)

		frappe.msgprint(
			"".join(msg_parts),
			title="Connection Test Results",
			indicator="red" if has_error else "green",
		)

		if has_error:
			frappe.throw("One or more connection tests failed. See details above.")
