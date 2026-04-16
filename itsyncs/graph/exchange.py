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

# Module-level primary-domain cache: {tenant_id: "contoso.onmicrosoft.com"}
_primary_domain_cache: dict[str, str] = {}

# Documented system mailbox GUID (same for every Microsoft 365 tenant)
SYSTEM_MAILBOX_GUID = "bb558c35-97f1-4cb9-8ff7-d53741dc928c"


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


def _resolve_primary_domain(tenant) -> str | None:
	"""Return the tenant's primary onmicrosoft.com domain, caching across calls.

	Looks up, in order:
	  1. module-level cache
	  2. tenant.primary_domain field on the doc
	  3. Get-OrganizationConfig via REST (without anchor header, one-time)

	Result 3 is persisted onto the doc so subsequent worker restarts skip the
	lookup. Returns None if nothing works — callers must handle that by skipping
	the anchor header.
	"""
	import frappe

	tid = tenant.tenant_id
	if tid in _primary_domain_cache:
		return _primary_domain_cache[tid]

	existing = getattr(tenant, "primary_domain", None)
	if existing:
		_primary_domain_cache[tid] = existing
		return existing

	# One-off unrouted probe to learn the primary domain.
	try:
		token = get_exchange_token(tenant)
		url = f"https://outlook.office365.com/adminapi/beta/{tid}/InvokeCommand"
		resp = httpx.post(
			url,
			headers={
				"Authorization": f"Bearer {token}",
				"Content-Type": "application/json",
				"Accept-Encoding": "identity",
			},
			json={"CmdletInput": {"CmdletName": "Get-OrganizationConfig", "Parameters": {}}},
			timeout=30,
		)
		if resp.status_code == 200:
			values = resp.json().get("value", [])
			if values:
				# Get-OrganizationConfig returns Name = "<tenant>.onmicrosoft.com"
				domain = values[0].get("Name") or values[0].get("Identity")
				if domain:
					_primary_domain_cache[tid] = domain
					try:
						frappe.db.set_value("ITSync Tenant", tenant.name, "primary_domain",
											domain, update_modified=False)
						frappe.db.commit()
					except Exception:
						pass  # Non-fatal — cache in memory still works
					return domain
	except Exception:
		# Silent: fall back to no anchor. Caller handles the None return value.
		pass

	return None


def _build_anchor(tenant) -> str | None:
	"""Build the X-AnchorMailbox value for app-only Exchange routing."""
	domain = _resolve_primary_domain(tenant)
	if not domain:
		return None
	return f"APP:SystemMailbox{{{SYSTEM_MAILBOX_GUID}}}@{domain}"


def _invoke_command(tenant, cmdlet_name: str, parameters: dict) -> list[dict]:
	"""Execute an Exchange cmdlet via InvokeCommand REST API.

	Sends the X-AnchorMailbox header so the request is pinned to the tenant's
	backend server. Without this header, Exchange routes requests to arbitrary
	backends — a New-MailContact may land on one server and the immediately
	following Set-Contact on another, causing NotFound + long retry waits.

	Returns the list of result objects from the 'value' array.
	"""
	import frappe

	if isinstance(tenant, str):
		tenant = frappe.get_doc("ITSync Tenant", tenant)

	token = get_exchange_token(tenant)
	url = f"https://outlook.office365.com/adminapi/beta/{tenant.tenant_id}/InvokeCommand"

	headers = {
		"Authorization": f"Bearer {token}",
		"Content-Type": "application/json",
		"Accept-Encoding": "identity",
	}
	anchor = _build_anchor(tenant)
	if anchor:
		headers["X-AnchorMailbox"] = anchor

	body = {"CmdletInput": {"CmdletName": cmdlet_name, "Parameters": parameters}}

	# Up to 2 attempts for transient transport faults (ReadTimeout, 429, 503).
	# NotFound/replication retries happen one layer up in _invoke_with_retry.
	last_exc = None
	for attempt in range(2):
		try:
			resp = httpx.post(url, headers=headers, json=body, timeout=30)
		except httpx.ReadTimeout as e:
			last_exc = e
			if attempt == 0:
				time.sleep(1)
				continue
			raise Exception(f"Exchange {cmdlet_name} failed: read timeout after 2 attempts") from e
		except httpx.HTTPError as e:
			raise Exception(f"Exchange {cmdlet_name} transport error: {e}") from e

		if resp.status_code == 429 or resp.status_code == 503:
			retry_after = resp.headers.get("Retry-After")
			try:
				wait = int(retry_after) if retry_after else 2
			except ValueError:
				wait = 5
			if attempt == 0:
				time.sleep(min(wait, 10))
				continue
			raise Exception(f"Exchange {cmdlet_name} throttled ({resp.status_code}), Retry-After={retry_after}")

		if resp.status_code != 200:
			raise Exception(f"Exchange {cmdlet_name} failed ({resp.status_code}): {resp.text[:500]}")

		# success
		data = resp.json()
		results = data.get("value", [])

		# Follow pagination if Exchange returned a nextLink.
		# The adminapi defaults to 1000 per page even with ResultSize=Unlimited.
		next_link = data.get("@odata.nextLink")
		while next_link:
			try:
				page_resp = httpx.post(next_link, headers=headers, json={}, timeout=30)
			except httpx.ReadTimeout:
				break  # partial results are better than none
			if page_resp.status_code != 200:
				break
			page_data = page_resp.json()
			results.extend(page_data.get("value", []))
			next_link = page_data.get("@odata.nextLink")

		return results

	# Defensive: loop should always either return or raise
	if last_exc:
		raise last_exc
	raise Exception(f"Exchange {cmdlet_name}: unreachable")


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
			_invoke_with_retry(tenant, "Set-Contact", rich_params, max_retries=8)
		except Exception:
			# Non-fatal: contact exists in GAL but rich fields may be missing.
			# They will be updated on the next incremental sync. We intentionally
			# do NOT call frappe.log_error here — it would pop a toast for every
			# System Manager. The deferred Set-Contact is expected noise during
			# bulk syncs; the sync log's own details field already records it.
			pass

	return alias


def create_mail_contact_basic(tenant, contact_data: dict) -> str:
	"""Create a mail contact via New-MailContact ONLY — no Set-Contact.

	Returns the Alias. Intended for the delayed-batch pattern in _run_initial_sync:
	many of these in rapid succession, then a wait for Exchange replication, then
	set_mail_contact_rich_fields() for each in a second pass.
	"""
	email = _get_primary_email(contact_data)
	if not email:
		raise ValueError("Contact must have at least one email address to create in GAL.")

	display_name = contact_data.get("display_name") or email
	params = {
		"Name": display_name,
		"ExternalEmailAddress": email,
		"DisplayName": display_name,
	}
	if contact_data.get("given_name"):
		params["FirstName"] = contact_data["given_name"]
	if contact_data.get("surname"):
		params["LastName"] = contact_data["surname"]

	results = _invoke_command(tenant, "New-MailContact", params)
	if not results:
		raise Exception("New-MailContact returned no results.")
	alias = results[0].get("Alias")
	if not alias:
		raise Exception(f"New-MailContact did not return an Alias: {list(results[0].keys())}")
	return alias


def set_mail_contact_rich_fields(tenant, identity: str, contact_data: dict):
	"""Apply rich properties (company, phone, address, etc.) to an existing
	mail contact via Set-Contact. Does not wrap in retry — the delayed-batch
	caller already waited for replication, so failures here are surprising and
	should surface as real errors rather than silently retrying."""
	rich_params = _build_set_contact_params(contact_data)
	if not rich_params:
		return
	rich_params["Identity"] = identity
	_invoke_command(tenant, "Set-Contact", rich_params)


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
	"""Delete a mail contact from the GAL. Uses the retry wrapper for resilience
	against transient NotFound responses (can happen right after identity changes)."""
	_invoke_with_retry(tenant, "Remove-MailContact", {
		"Identity": identity,
		"Confirm": False,
	}, max_retries=3)


def _invoke_with_retry(tenant, cmdlet_name: str, parameters: dict, max_retries: int = 8):
	"""Invoke a command with retry for replication delays.

	Flat 1-second backoff × up to 8 attempts = max ~7 s of sleeps on top of
	~1.5 s per Graph call. The anchor-mailbox fix eliminates most retries
	(a Set-Contact right after New-MailContact should usually succeed on
	attempt 1), but backends can still be eventually-consistent and some
	requests hit an edge server that hasn't caught up — these retries keep
	the worst case bounded to ~15 s instead of failing outright.
	"""
	for attempt in range(max_retries):
		try:
			return _invoke_command(tenant, cmdlet_name, parameters)
		except Exception as e:
			if attempt < max_retries - 1 and ("couldn't be found" in str(e) or "NotFound" in str(e)):
				time.sleep(1)
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
