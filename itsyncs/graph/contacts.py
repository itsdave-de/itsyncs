from itsyncs.graph.client import run_async

CONTACT_SELECT_FIELDS = [
	"id",
	"displayName",
	"givenName",
	"surname",
	"emailAddresses",
	"businessPhones",
	"mobilePhone",
	"companyName",
	"jobTitle",
	"department",
	"officeLocation",
	"businessAddress",
	"homeAddress",
	"otherAddress",
	"imAddresses",
	"categories",
	"birthday",
	"personalNotes",
]


def fetch_contact_folders(client, email_address: str) -> list[dict]:
	"""Fetch all contact folders from a mailbox. Returns list of {id, name, parent_folder_id}."""

	async def _fetch():
		from kiota_abstractions.base_request_configuration import RequestConfiguration
		from msgraph.generated.users.item.contact_folders.contact_folders_request_builder import (
			ContactFoldersRequestBuilder,
		)

		query = ContactFoldersRequestBuilder.ContactFoldersRequestBuilderGetQueryParameters(
			select=["id", "displayName", "parentFolderId"],
			top=100,
		)
		config = RequestConfiguration(query_parameters=query)
		config.headers.add("Prefer", 'IdType="ImmutableId"')

		result = await client.users.by_user_id(email_address).contact_folders.get(
			request_configuration=config
		)

		folders = []
		if result and result.value:
			for f in result.value:
				folders.append({
					"id": f.id,
					"name": f.display_name,
					"parent_folder_id": f.parent_folder_id,
				})

		while result and result.odata_next_link:
			result = await client.users.by_user_id(email_address).contact_folders.with_url(
				result.odata_next_link
			).get()
			if result and result.value:
				for f in result.value:
					folders.append({
						"id": f.id,
						"name": f.display_name,
						"parent_folder_id": f.parent_folder_id,
					})

		return folders

	return run_async(_fetch())


def fetch_all_contacts(client, email_address: str, folder_id: str | None = None) -> list[dict]:
	"""Fetch all contacts from a mailbox via httpx. Returns a list of normalized contact dicts.

	Args:
		client: GraphServiceClient — used to extract tenant for token.
		folder_id: Optional contact folder ID. If None, fetches from default contacts folder.
	"""
	import httpx

	from itsyncs.graph.client import get_graph_token

	tenant = _get_tenant_from_client(client)
	token = get_graph_token(tenant)

	select = ",".join(CONTACT_SELECT_FIELDS)
	if folder_id:
		base = f"https://graph.microsoft.com/v1.0/users/{email_address}/contactFolders/{folder_id}/contacts"
	else:
		base = f"https://graph.microsoft.com/v1.0/users/{email_address}/contacts"

	url = f"{base}?$select={select}&$top=100&$orderby=displayName"
	headers = {
		"Authorization": f"Bearer {token}",
		"Prefer": 'IdType="ImmutableId"',
	}

	contacts = []
	with httpx.Client() as http:
		while url:
			resp = http.get(url, headers=headers, timeout=60)
			if resp.status_code != 200:
				raise Exception(f"Graph API error {resp.status_code}: {resp.text[:300]}")
			data = resp.json()
			for raw in data.get("value", []):
				contacts.append(_normalize_contact(raw))
			url = data.get("@odata.nextLink")

	return contacts


def fetch_contact_delta(
	client,
	email_address: str,
	delta_token: str | None = None,
	folder_id: str | None = None,
) -> tuple[list[dict], list[dict], list[str], str]:
	"""Fetch contact changes using delta query via httpx.

	Args:
		client: GraphServiceClient — used to extract tenant for token.
		folder_id: Optional contact folder ID. Defaults to "contacts" (the well-known default folder).

	Returns:
		(added_or_modified, [], deleted_ids, new_delta_token)
	"""
	import httpx

	from itsyncs.graph.client import get_graph_token

	tenant = _get_tenant_from_client(client)
	token = get_graph_token(tenant)

	headers = {
		"Authorization": f"Bearer {token}",
		"Prefer": 'IdType="ImmutableId"',
	}

	if delta_token:
		url = delta_token
	else:
		effective_folder = folder_id or "contacts"
		url = (
			f"https://graph.microsoft.com/v1.0/users/{email_address}"
			f"/contactFolders/{effective_folder}/contacts/delta"
		)

	changed = []
	deleted_ids = []
	new_delta_token = None

	with httpx.Client() as http:
		while url:
			resp = http.get(url, headers=headers, timeout=60)
			if resp.status_code != 200:
				raise Exception(f"Graph API delta error {resp.status_code}: {resp.text[:300]}")
			data = resp.json()

			for item in data.get("value", []):
				if item.get("@removed"):
					deleted_ids.append(item["id"])
				else:
					changed.append(_normalize_contact(item))

			url = data.get("@odata.nextLink")
			if not url:
				new_delta_token = data.get("@odata.deltaLink")

	return changed, [], deleted_ids, new_delta_token or ""


def create_contact(tenant, email_address: str, contact_data: dict, folder_id: str | None = None) -> str:
	"""Create a contact in the target mailbox via raw HTTP. Returns the new contact ID.

	Args:
		folder_id: Optional contact folder ID. If None, creates in default contacts folder.
	"""
	import httpx

	from itsyncs.graph.client import get_graph_token

	token = get_graph_token(tenant)
	body = _build_contact_body(contact_data)

	if folder_id:
		url = f"https://graph.microsoft.com/v1.0/users/{email_address}/contactFolders/{folder_id}/contacts"
	else:
		url = f"https://graph.microsoft.com/v1.0/users/{email_address}/contacts"

	with httpx.Client() as http:
		resp = http.post(
			url,
			headers={
				"Authorization": f"Bearer {token}",
				"Content-Type": "application/json",
				"Prefer": 'IdType="ImmutableId"',
			},
			json=body,
			timeout=30,
		)
		if resp.status_code not in (200, 201):
			raise Exception(f"Graph API error {resp.status_code}: {resp.text[:300]}")
		return resp.json()["id"]


def update_contact(tenant, email_address: str, contact_id: str, contact_data: dict):
	"""Update an existing contact in the target mailbox via raw HTTP."""
	import httpx

	from itsyncs.graph.client import get_graph_token

	token = get_graph_token(tenant)
	body = _build_contact_body(contact_data)

	with httpx.Client() as http:
		resp = http.patch(
			f"https://graph.microsoft.com/v1.0/users/{email_address}/contacts/{contact_id}",
			headers={
				"Authorization": f"Bearer {token}",
				"Content-Type": "application/json",
			},
			json=body,
			timeout=30,
		)
		if resp.status_code not in (200, 204):
			raise Exception(f"Graph API error {resp.status_code}: {resp.text[:300]}")


def delete_contact(tenant, email_address: str, contact_id: str):
	"""Delete a contact from the target mailbox via raw HTTP."""
	import httpx

	from itsyncs.graph.client import get_graph_token

	token = get_graph_token(tenant)

	with httpx.Client() as http:
		resp = http.delete(
			f"https://graph.microsoft.com/v1.0/users/{email_address}/contacts/{contact_id}",
			headers={"Authorization": f"Bearer {token}"},
			timeout=30,
		)
		if resp.status_code not in (200, 204):
			raise Exception(f"Graph API error {resp.status_code}: {resp.text[:300]}")


def _build_contact_body(contact_data: dict) -> dict:
	"""Build a Graph API contact JSON body from normalized contact data."""
	body = {}

	field_map = {
		"given_name": "givenName",
		"surname": "surname",
		"display_name": "displayName",
		"middle_name": "middleName",
		"nickname": "nickName",
		"title": "title",
		"generation": "generation",
		"initials": "initials",
		"file_as": "fileAs",
		"company_name": "companyName",
		"job_title": "jobTitle",
		"department": "department",
		"profession": "profession",
		"office_location": "officeLocation",
		"business_home_page": "businessHomePage",
		"manager": "manager",
		"assistant_name": "assistantName",
		"mobile_phone": "mobilePhone",
		"spouse_name": "spouseName",
		"personal_notes": "personalNotes",
	}

	for src, dst in field_map.items():
		val = contact_data.get(src)
		if val is not None:
			body[dst] = val

	if contact_data.get("categories"):
		body["categories"] = contact_data["categories"]

	if contact_data.get("birthday"):
		# Graph expects a dateTimeOffset; midnight UTC on the given date.
		body["birthday"] = f"{str(contact_data['birthday'])[:10]}T00:00:00Z"

	if contact_data.get("home_phones"):
		body["homePhones"] = contact_data["home_phones"]

	for src, dst in (("home_address", "homeAddress"), ("other_address", "otherAddress")):
		addr = contact_data.get(src)
		if addr:
			built = {
				k: v for k, v in {
					"street": addr.get("street"),
					"city": addr.get("city"),
					"state": addr.get("state"),
					"postalCode": addr.get("postal_code"),
					"countryOrRegion": addr.get("country_or_region"),
				}.items() if v is not None
			}
			if built:
				body[dst] = built

	if contact_data.get("business_phones"):
		body["businessPhones"] = contact_data["business_phones"]

	if contact_data.get("email_addresses"):
		body["emailAddresses"] = [
			{"address": e["address"], "name": e.get("name")}
			for e in contact_data["email_addresses"]
		]

	if contact_data.get("business_address"):
		addr = contact_data["business_address"]
		body["businessAddress"] = {
			k: v
			for k, v in {
				"street": addr.get("street"),
				"city": addr.get("city"),
				"state": addr.get("state"),
				"postalCode": addr.get("postal_code"),
				"countryOrRegion": addr.get("country_or_region"),
			}.items()
			if v is not None
		}

	return body


def _normalize_contact(raw: dict) -> dict:
	"""Normalize a raw JSON contact dict from the Graph API."""
	email_addresses = []
	for e in raw.get("emailAddresses") or []:
		if e.get("address"):
			email_addresses.append({"address": e["address"], "name": e.get("name")})

	business_address = None
	addr = raw.get("businessAddress")
	if addr and any([addr.get("street"), addr.get("city"), addr.get("state"),
					 addr.get("postalCode"), addr.get("countryOrRegion")]):
		business_address = {
			"street": addr.get("street"),
			"city": addr.get("city"),
			"state": addr.get("state"),
			"postal_code": addr.get("postalCode"),
			"country_or_region": addr.get("countryOrRegion"),
		}

	def _addr(raw_addr):
		if raw_addr and any([raw_addr.get("street"), raw_addr.get("city"), raw_addr.get("state"),
							  raw_addr.get("postalCode"), raw_addr.get("countryOrRegion")]):
			return {
				"street": raw_addr.get("street"),
				"city": raw_addr.get("city"),
				"state": raw_addr.get("state"),
				"postal_code": raw_addr.get("postalCode"),
				"country_or_region": raw_addr.get("countryOrRegion"),
			}
		return None

	return {
		"id": raw.get("id"),
		"display_name": raw.get("displayName"),
		"given_name": raw.get("givenName"),
		"surname": raw.get("surname"),
		"middle_name": raw.get("middleName"),
		"nickname": raw.get("nickName"),
		"title": raw.get("title"),
		"generation": raw.get("generation"),
		"initials": raw.get("initials"),
		"file_as": raw.get("fileAs"),
		"email_addresses": email_addresses,
		"business_phones": raw.get("businessPhones") or [],
		"home_phones": raw.get("homePhones") or [],
		"mobile_phone": raw.get("mobilePhone"),
		"company_name": raw.get("companyName"),
		"job_title": raw.get("jobTitle"),
		"department": raw.get("department"),
		"profession": raw.get("profession"),
		"office_location": raw.get("officeLocation"),
		"business_home_page": raw.get("businessHomePage"),
		"manager": raw.get("manager"),
		"assistant_name": raw.get("assistantName"),
		"business_address": business_address,
		"home_address": _addr(raw.get("homeAddress")),
		"other_address": _addr(raw.get("otherAddress")),
		"birthday": (raw.get("birthday") or "")[:10] or None,
		"spouse_name": raw.get("spouseName"),
		"categories": raw.get("categories") or [],
		"personal_notes": raw.get("personalNotes"),
	}


def _get_tenant_from_client(client):
	"""Extract the tenant doc from a GraphServiceClient.

	Relies on the _tenant_name attribute set by get_graph_client().
	"""
	import frappe

	tenant_name = getattr(client, "_tenant_name", None)
	if tenant_name:
		return frappe.get_doc("ITSync Tenant", tenant_name)
	raise Exception("Cannot determine tenant from client. Use get_graph_client() which sets _tenant_name.")
