from itsyncs.graph.client import run_async


USER_SELECT_FIELDS = [
	"id",
	"displayName",
	"givenName",
	"surname",
	"mail",
	"userPrincipalName",
	"businessPhones",
	"mobilePhone",
	"companyName",
	"jobTitle",
	"department",
	"officeLocation",
	"streetAddress",
	"city",
	"state",
	"postalCode",
	"country",
]

ORG_CONTACT_SELECT_FIELDS = [
	"id",
	"displayName",
	"givenName",
	"surname",
	"mail",
	"phones",
	"companyName",
	"jobTitle",
	"department",
	"officeLocation",
	"addresses",
]


def fetch_all_gal_entries(client, gal_include: str) -> list[dict]:
	"""Fetch all GAL entries (users and/or org contacts)."""
	entries = []

	if gal_include in ("Users", "Both"):
		entries.extend(_fetch_all_users(client))

	if gal_include in ("Org Contacts", "Both"):
		entries.extend(_fetch_all_org_contacts(client))

	return entries


def fetch_gal_delta(
	client,
	gal_include: str,
	delta_token_users: str | None = None,
	delta_token_org: str | None = None,
) -> tuple[list[dict], list[str], str | None, str | None]:
	"""Fetch GAL changes via delta queries."""
	changed = []
	deleted_ids = []
	new_token_users = delta_token_users
	new_token_org = delta_token_org

	if gal_include in ("Users", "Both"):
		u_changed, u_deleted, new_token_users = _fetch_users_delta(client, delta_token_users)
		changed.extend(u_changed)
		deleted_ids.extend(u_deleted)

	if gal_include in ("Org Contacts", "Both"):
		o_changed, o_deleted, new_token_org = _fetch_org_contacts_delta(client, delta_token_org)
		changed.extend(o_changed)
		deleted_ids.extend(o_deleted)

	return changed, deleted_ids, new_token_users, new_token_org


def _fetch_all_users(client) -> list[dict]:
	async def _fetch():
		from kiota_abstractions.base_request_configuration import RequestConfiguration
		from msgraph.generated.users.users_request_builder import UsersRequestBuilder

		users = []
		query_params = UsersRequestBuilder.UsersRequestBuilderGetQueryParameters(
			select=USER_SELECT_FIELDS,
			top=100,
			filter="accountEnabled eq true and mail ne null",
		)
		config = RequestConfiguration(query_parameters=query_params)
		config.headers.add("ConsistencyLevel", "eventual")

		result = await client.users.get(request_configuration=config)

		if result and result.value:
			users.extend([_normalize_user(u) for u in result.value])

		while result and result.odata_next_link:
			result = await client.users.with_url(result.odata_next_link).get()
			if result and result.value:
				users.extend([_normalize_user(u) for u in result.value])

		return users

	return run_async(_fetch())


def _fetch_all_org_contacts(client) -> list[dict]:
	async def _fetch():
		from kiota_abstractions.base_request_configuration import RequestConfiguration
		from msgraph.generated.contacts.contacts_request_builder import ContactsRequestBuilder

		contacts = []
		query_params = ContactsRequestBuilder.ContactsRequestBuilderGetQueryParameters(
			select=ORG_CONTACT_SELECT_FIELDS,
			top=100,
		)
		config = RequestConfiguration(query_parameters=query_params)
		config.headers.add("ConsistencyLevel", "eventual")

		result = await client.contacts.get(request_configuration=config)

		if result and result.value:
			contacts.extend([_normalize_org_contact(c) for c in result.value])

		while result and result.odata_next_link:
			result = await client.contacts.with_url(result.odata_next_link).get()
			if result and result.value:
				contacts.extend([_normalize_org_contact(c) for c in result.value])

		return contacts

	return run_async(_fetch())


def _fetch_users_delta(client, delta_token):
	async def _fetch():
		from kiota_abstractions.base_request_configuration import RequestConfiguration

		changed = []
		deleted_ids = []

		if delta_token:
			result = await client.users.delta.with_url(delta_token).get()
		else:
			config = RequestConfiguration()
			config.headers.add("ConsistencyLevel", "eventual")
			result = await client.users.delta.get(request_configuration=config)

		new_token = None
		while result:
			if result.value:
				for item in result.value:
					if hasattr(item, "additional_data") and item.additional_data.get("@removed"):
						deleted_ids.append(f"user:{item.id}")
					else:
						changed.append(_normalize_user(item))

			if result.odata_next_link:
				result = await client.users.delta.with_url(result.odata_next_link).get()
			elif result.odata_delta_link:
				new_token = result.odata_delta_link
				break
			else:
				break

		return changed, deleted_ids, new_token

	return run_async(_fetch())


def _fetch_org_contacts_delta(client, delta_token):
	async def _fetch():
		from kiota_abstractions.base_request_configuration import RequestConfiguration

		changed = []
		deleted_ids = []

		if delta_token:
			result = await client.contacts.delta.with_url(delta_token).get()
		else:
			config = RequestConfiguration()
			config.headers.add("ConsistencyLevel", "eventual")
			result = await client.contacts.delta.get(request_configuration=config)

		new_token = None
		while result:
			if result.value:
				for item in result.value:
					if hasattr(item, "additional_data") and item.additional_data.get("@removed"):
						deleted_ids.append(f"org:{item.id}")
					else:
						changed.append(_normalize_org_contact(item))

			if result.odata_next_link:
				result = await client.contacts.delta.with_url(result.odata_next_link).get()
			elif result.odata_delta_link:
				new_token = result.odata_delta_link
				break
			else:
				break

		return changed, deleted_ids, new_token

	return run_async(_fetch())


def _normalize_user(user) -> dict:
	"""Convert a Graph API user to a normalized contact dict."""
	email_addresses = []
	if user.mail:
		email_addresses.append({"address": user.mail, "name": user.display_name})

	business_address = None
	if any([
		getattr(user, "street_address", None),
		getattr(user, "city", None),
		getattr(user, "state", None),
		getattr(user, "postal_code", None),
		getattr(user, "country", None),
	]):
		business_address = {
			"street": getattr(user, "street_address", None),
			"city": getattr(user, "city", None),
			"state": getattr(user, "state", None),
			"postal_code": getattr(user, "postal_code", None),
			"country_or_region": getattr(user, "country", None),
		}

	return {
		"id": f"user:{user.id}",
		"display_name": user.display_name,
		"given_name": user.given_name,
		"surname": user.surname,
		"email_addresses": email_addresses,
		"business_phones": user.business_phones or [],
		"mobile_phone": user.mobile_phone,
		"company_name": user.company_name,
		"job_title": user.job_title,
		"department": user.department,
		"office_location": user.office_location,
		"business_address": business_address,
		"personal_notes": None,
	}


def _normalize_org_contact(contact) -> dict:
	"""Convert a Graph API org contact to a normalized contact dict."""
	email_addresses = []
	if contact.mail:
		email_addresses.append({"address": contact.mail, "name": contact.display_name})

	business_phones = []
	mobile_phone = None
	if hasattr(contact, "phones") and contact.phones:
		for phone in contact.phones:
			if phone.type and "mobile" in phone.type.value.lower():
				mobile_phone = phone.number
			elif phone.number:
				business_phones.append(phone.number)

	business_address = None
	if hasattr(contact, "addresses") and contact.addresses:
		for addr in contact.addresses:
			if addr.city or addr.street:
				business_address = {
					"street": addr.street,
					"city": addr.city,
					"state": addr.state,
					"postal_code": addr.postal_code,
					"country_or_region": addr.country_or_region,
				}
				break

	return {
		"id": f"org:{contact.id}",
		"display_name": contact.display_name,
		"given_name": contact.given_name,
		"surname": contact.surname,
		"email_addresses": email_addresses,
		"business_phones": business_phones,
		"mobile_phone": mobile_phone,
		"company_name": contact.company_name,
		"job_title": contact.job_title,
		"department": contact.department,
		"office_location": contact.office_location,
		"business_address": business_address,
		"personal_notes": None,
	}
