"""Exchange Online InvokeCommand REST API for GAL write operations.

Uses the undocumented adminapi endpoint to execute Exchange cmdlets:
- New-MailContact: Create a mail contact in the GAL
- Set-Contact: Update contact properties (name, company, phone, etc.)
- Remove-MailContact: Delete a mail contact from the GAL
- Get-MailContact: List existing mail contacts

Prerequisites (Azure AD):
- Enterprise App "Office 365 Exchange Online" registered in tenant
- API permission: Exchange.ManageAsApp (with admin consent)
- Directory role: Exchange Administrator assigned to the app
"""
import time

import httpx

# Module-level token cache: {tenant_id: (token_str, expiry_timestamp)}
_exo_token_cache: dict[str, tuple[str, float]] = {}


def get_exchange_token(tenant) -> str:
	"""Get a cached Exchange Online access token."""
	import frappe

	if isinstance(tenant, str):
		tenant = frappe.get_doc("ITSync Tenant", tenant)

	key = tenant.tenant_id
	now = time.time()

	if key in _exo_token_cache:
		token_str, expiry = _exo_token_cache[key]
		if now < expiry - 300:
			return token_str

	from azure.identity import ClientSecretCredential

	cred = ClientSecretCredential(
		tenant_id=tenant.tenant_id,
		client_id=tenant.client_id,
		client_secret=tenant.get_password("client_secret"),
	)
	token = cred.get_token("https://outlook.office365.com/.default")
	_exo_token_cache[key] = (token.token, token.expires_on)
	return token.token


def _invoke_command(tenant, cmdlet_name: str, parameters: dict) -> list[dict]:
	"""Execute an Exchange cmdlet via InvokeCommand REST API.

	Returns the list of result objects from the 'value' array.
	"""
	import frappe

	if isinstance(tenant, str):
		tenant = frappe.get_doc("ITSync Tenant", tenant)

	token = get_exchange_token(tenant)
	url = f"https://outlook.office365.com/adminapi/beta/{tenant.tenant_id}/InvokeCommand"

	resp = httpx.post(
		url,
		headers={
			"Authorization": f"Bearer {token}",
			"Content-Type": "application/json",
			"Accept-Encoding": "identity",
		},
		json={"CmdletInput": {"CmdletName": cmdlet_name, "Parameters": parameters}},
		timeout=60,
	)

	if resp.status_code != 200:
		raise Exception(f"Exchange {cmdlet_name} failed ({resp.status_code}): {resp.text[:500]}")

	data = resp.json()
	warnings = data.get("@adminapi.warnings", [])
	if warnings:
		frappe.log_error(f"Exchange {cmdlet_name} warnings", str(warnings))

	return data.get("value", [])


def fetch_all_mail_contacts(tenant) -> list[dict]:
	"""Fetch all mail contacts from Exchange Online. Returns normalized contact dicts.

	Merges data from Get-MailContact (email, alias) and Get-Contact (rich fields).
	"""
	mail_contacts = _invoke_command(tenant, "Get-MailContact", {"ResultSize": "Unlimited"})
	rich_contacts = _invoke_command(tenant, "Get-Contact", {"ResultSize": "Unlimited"})

	# Index rich contact data by Identity for fast lookup
	rich_by_identity = {}
	for rc in rich_contacts:
		identity = rc.get("Identity") or rc.get("Id")
		if identity:
			rich_by_identity[identity] = rc

	contacts = []
	for mc in mail_contacts:
		identity = mc.get("Identity") or mc.get("Id")
		rich = rich_by_identity.get(identity, {})
		contacts.append(_normalize_mail_contact(mc, rich))
	return contacts


def create_mail_contact(tenant, contact_data: dict) -> str:
	"""Create a mail contact in the GAL. Returns the Alias for future operations.

	Two-step process:
	1. New-MailContact — creates the mail contact with email and name
	2. Set-Contact — sets rich properties (phone, company, address, etc.)
	   Uses retry because Exchange may need time to replicate the new object.
	"""
	email = _get_primary_email(contact_data)
	if not email:
		raise ValueError("Contact must have at least one email address to create in GAL.")

	display_name = contact_data.get("display_name") or email

	# Build New-MailContact params — include FirstName/LastName directly
	new_params = {
		"Name": display_name,
		"ExternalEmailAddress": email,
		"DisplayName": display_name,
	}
	if contact_data.get("given_name"):
		new_params["FirstName"] = contact_data["given_name"]
	if contact_data.get("surname"):
		new_params["LastName"] = contact_data["surname"]

	results = _invoke_command(tenant, "New-MailContact", new_params)

	if not results:
		raise Exception("New-MailContact returned no results.")

	alias = results[0].get("Alias")
	if not alias:
		raise Exception(f"New-MailContact did not return an Alias: {list(results[0].keys())}")

	# Set rich contact properties (company, phone, address, etc.)
	# Use email as identity — more reliable than alias (which may contain special chars)
	rich_params = _build_set_contact_params(contact_data)
	if rich_params:
		rich_params["Identity"] = email
		try:
			_invoke_with_retry(tenant, "Set-Contact", rich_params, max_retries=5)
		except Exception:
			# Non-fatal: contact exists in GAL but rich fields may be missing.
			# They will be updated on the next incremental sync.
			import frappe
			frappe.log_error(
				f"Set-Contact deferred for {email}",
				"Contact was created but rich properties could not be set due to replication delay. "
				"They will be applied on the next sync.",
			)

	return alias


def update_mail_contact(tenant, identity: str, contact_data: dict):
	"""Update an existing mail contact in the GAL."""
	# Update mail properties (DisplayName, email)
	mail_params = {}
	display_name = contact_data.get("display_name")
	if display_name:
		mail_params["DisplayName"] = display_name

	email = _get_primary_email(contact_data)
	if email:
		mail_params["ExternalEmailAddress"] = email

	if mail_params:
		mail_params["Identity"] = identity
		_invoke_command(tenant, "Set-MailContact", mail_params)

	# Update rich contact properties
	rich_params = _build_set_contact_params(contact_data)
	if rich_params:
		rich_params["Identity"] = identity
		_invoke_command(tenant, "Set-Contact", rich_params)


def delete_mail_contact(tenant, identity: str):
	"""Delete a mail contact from the GAL."""
	_invoke_command(tenant, "Remove-MailContact", {
		"Identity": identity,
		"Confirm": False,
	})


def _invoke_with_retry(tenant, cmdlet_name: str, parameters: dict, max_retries: int = 5):
	"""Invoke a command with retry for replication delays."""
	for attempt in range(max_retries):
		try:
			return _invoke_command(tenant, cmdlet_name, parameters)
		except Exception as e:
			if attempt < max_retries - 1 and ("couldn't be found" in str(e) or "NotFound" in str(e)):
				time.sleep(5 * (attempt + 1))
				continue
			raise


# Common German → ISO-3166 country name mapping for Exchange
_COUNTRY_MAP = {
	"deutschland": "Germany",
	"österreich": "Austria",
	"schweiz": "Switzerland",
	"niederlande": "Netherlands",
	"frankreich": "France",
	"italien": "Italy",
	"spanien": "Spain",
	"belgien": "Belgium",
	"dänemark": "Denmark",
	"schweden": "Sweden",
	"norwegen": "Norway",
	"finnland": "Finland",
	"polen": "Poland",
	"tschechien": "Czech Republic",
	"ungarn": "Hungary",
	"großbritannien": "United Kingdom",
	"vereinigte staaten": "United States",
	"vereinigtes königreich": "United Kingdom",
}


def _normalize_country(country: str | None) -> str | None:
	"""Convert country name to Exchange-compatible format (English name or ISO code)."""
	if not country:
		return None
	# Already a 2-letter ISO code
	if len(country) == 2 and country.isalpha():
		return country.upper()
	# Check German → English mapping
	mapped = _COUNTRY_MAP.get(country.lower())
	if mapped:
		return mapped
	return country


def _build_set_contact_params(contact_data: dict) -> dict:
	"""Build Set-Contact parameters from normalized contact data."""
	params = {}

	if contact_data.get("given_name"):
		params["FirstName"] = contact_data["given_name"]
	if contact_data.get("surname"):
		params["LastName"] = contact_data["surname"]
	if contact_data.get("company_name"):
		params["Company"] = contact_data["company_name"]
	if contact_data.get("department"):
		params["Department"] = contact_data["department"]
	if contact_data.get("job_title"):
		params["Title"] = contact_data["job_title"]
	if contact_data.get("office_location"):
		params["Office"] = contact_data["office_location"]

	# Phone numbers
	phones = contact_data.get("business_phones") or []
	if phones:
		params["Phone"] = phones[0]
	if contact_data.get("mobile_phone"):
		params["MobilePhone"] = contact_data["mobile_phone"]

	# Address
	addr = contact_data.get("business_address")
	if addr:
		if addr.get("street"):
			params["StreetAddress"] = addr["street"]
		if addr.get("city"):
			params["City"] = addr["city"]
		if addr.get("state"):
			params["StateOrProvince"] = addr["state"]
		if addr.get("postal_code"):
			params["PostalCode"] = addr["postal_code"]
		country = _normalize_country(addr.get("country_or_region"))
		if country:
			params["CountryOrRegion"] = country

	return params


def _normalize_mail_contact(mc: dict, rich: dict | None = None) -> dict:
	"""Normalize an Exchange MailContact to our standard contact dict.

	Args:
		mc: Data from Get-MailContact (email, alias, display name).
		rich: Optional data from Get-Contact (first name, company, phone, etc.).
	"""
	rich = rich or {}

	# ExternalEmailAddress has "SMTP:" prefix
	ext_email = mc.get("ExternalEmailAddress", "")
	if ext_email.upper().startswith("SMTP:"):
		ext_email = ext_email[5:]

	email_addresses = []
	if ext_email:
		email_addresses.append({"address": ext_email, "name": mc.get("DisplayName")})

	return {
		"id": mc.get("Alias") or mc.get("Guid") or "",
		"display_name": mc.get("DisplayName"),
		"given_name": rich.get("FirstName"),
		"surname": rich.get("LastName"),
		"email_addresses": email_addresses,
		"business_phones": [rich["Phone"]] if rich.get("Phone") else [],
		"mobile_phone": rich.get("MobilePhone"),
		"company_name": rich.get("Company"),
		"job_title": rich.get("Title"),
		"department": rich.get("Department"),
		"office_location": rich.get("Office"),
		"business_address": _extract_address(rich),
		"personal_notes": rich.get("Notes"),
	}


def _extract_address(mc: dict) -> dict | None:
	"""Extract business address from mail contact fields."""
	street = mc.get("StreetAddress")
	city = mc.get("City")
	state = mc.get("StateOrProvince")
	postal = mc.get("PostalCode")
	country = mc.get("CountryOrRegion")

	if any([street, city, state, postal, country]):
		return {
			"street": street,
			"city": city,
			"state": state,
			"postal_code": postal,
			"country_or_region": country,
		}
	return None


def _get_primary_email(contact_data: dict) -> str | None:
	"""Get primary email from normalized contact data."""
	emails = contact_data.get("email_addresses") or []
	if emails:
		return emails[0].get("address")
	return None
