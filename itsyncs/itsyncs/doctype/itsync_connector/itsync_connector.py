import frappe
from frappe.model.document import Document


class ITSyncConnector(Document):
	def validate(self):
		if self.connector_type == "GAL":
			self.is_writable = 1
			self.email_address = None
		else:
			self.is_writable = 1
			if not self.email_address:
				frappe.throw("Email Address is required for Mailbox and Shared Mailbox connectors.")

		# Detect folder change and reset sync state
		if not self.is_new():
			old_folder = frappe.db.get_value("ITSync Connector", self.name, "contact_folder") or ""
			new_folder = self.contact_folder or ""
			if old_folder != new_folder:
				self._on_folder_changed()

		self.test_connection()

	def _on_folder_changed(self):
		"""Reset sync state when contact folder changes."""
		# Clear delta token (no longer valid for new folder)
		self.delta_token = None

		# Reset all pairs that use this connector (as source or target)
		source_pairs = frappe.get_all("ITSync Pair", filters={"source": self.name}, fields=["name", "initial_sync_complete"])
		target_pairs = frappe.get_all("ITSync Pair", filters={"target": self.name}, fields=["name", "initial_sync_complete"])
		seen = set()
		pairs = []
		for p in source_pairs + target_pairs:
			if p.name not in seen:
				seen.add(p.name)
				pairs.append(p)
		for pair in pairs:
			if pair.initial_sync_complete:
				frappe.db.set_value("ITSync Pair", pair.name, {
					"initial_sync_complete": 0,
					"enabled": 0,
					"status": "Draft",
				})
				frappe.msgprint(
					f"Sync pair <b>{pair.name}</b> has been reset to Draft because the contact folder changed. "
					"A new initial sync is required.",
					alert=True,
					indicator="orange",
				)

	@frappe.whitelist()
	def test_connection(self):
		from itsyncs.graph.client import get_graph_client, run_async

		tenant = frappe.get_doc("ITSync Tenant", self.tenant)
		if tenant.connection_status != "Connected":
			frappe.throw("Tenant connection is not established. Please validate the tenant first.")

		try:
			client = get_graph_client(tenant)

			if self.connector_type in ("Mailbox", "Shared Mailbox"):
				self.contact_count = self._count_mailbox_contacts(client)
			elif self.connector_type == "GAL":
				self.contact_count = self._count_gal_entries(client)

			self.connection_status = "Connected"
			self.last_validated = frappe.utils.now_datetime()
			frappe.msgprint(
				f"Connection successful. {self.contact_count} contacts found.",
				alert=True,
				indicator="green",
			)
		except Exception as e:
			self.connection_status = "Failed"
			frappe.throw(f"Connection test failed: {e!s}")

	@frappe.whitelist()
	def fetch_folders(self):
		"""Fetch available contact folders from the mailbox."""
		if self.connector_type not in ("Mailbox", "Shared Mailbox"):
			frappe.throw("Contact folders are only available for Mailbox connectors.")
		if not self.email_address:
			frappe.throw("Email Address is required.")

		from itsyncs.graph.client import get_graph_client
		from itsyncs.graph.contacts import fetch_contact_folders

		tenant = frappe.get_doc("ITSync Tenant", self.tenant)
		client = get_graph_client(tenant)
		folders = fetch_contact_folders(client, self.email_address)
		return folders

	def _count_mailbox_contacts(self, client):
		from itsyncs.graph.client import run_async

		async def _count():
			from kiota_abstractions.base_request_configuration import RequestConfiguration
			from msgraph.generated.users.item.contacts.contacts_request_builder import (
				ContactsRequestBuilder,
			)

			query = ContactsRequestBuilder.ContactsRequestBuilderGetQueryParameters(
				count=True, top=1, select=["id"]
			)
			config = RequestConfiguration(query_parameters=query)
			config.headers.add("ConsistencyLevel", "eventual")

			user = client.users.by_user_id(self.email_address)
			if self.contact_folder:
				result = await user.contact_folders.by_contact_folder_id(
					self.contact_folder
				).contacts.get(request_configuration=config)
			else:
				result = await user.contacts.get(request_configuration=config)
			return getattr(result, "odata_count", None) or 0

		return run_async(_count())

	def _count_gal_entries(self, client):
		from itsyncs.graph.client import run_async

		async def _count():
			count = 0
			if self.gal_include in ("Users", "Both"):
				from msgraph.generated.users.users_request_builder import UsersRequestBuilder

				query = UsersRequestBuilder.UsersRequestBuilderGetQueryParameters(
					count=True, top=1, select=["id"]
				)
				from kiota_abstractions.base_request_configuration import RequestConfiguration

				config = RequestConfiguration(query_parameters=query)
				config.headers.add("ConsistencyLevel", "eventual")
				result = await client.users.get(request_configuration=config)
				count += getattr(result, "odata_count", None) or 0

			if self.gal_include in ("Org Contacts", "Both"):
				from msgraph.generated.contacts.contacts_request_builder import ContactsRequestBuilder

				query = ContactsRequestBuilder.ContactsRequestBuilderGetQueryParameters(
					count=True, top=1, select=["id"]
				)
				from kiota_abstractions.base_request_configuration import RequestConfiguration

				config = RequestConfiguration(query_parameters=query)
				config.headers.add("ConsistencyLevel", "eventual")
				result = await client.contacts.get(request_configuration=config)
				count += getattr(result, "odata_count", None) or 0

			return count

		return run_async(_count())
