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
import re
import time

import httpx

# Module-level token cache: {tenant_id: (token_str, expiry_timestamp)}
_exo_token_cache: dict[str, tuple[str, float]] = {}

# Module-level primary-domain cache: {tenant_id: "contoso.onmicrosoft.com"}
_primary_domain_cache: dict[str, str] = {}

# Documented system mailbox GUID (same for every Microsoft 365 tenant)
SYSTEM_MAILBOX_GUID = "bb558c35-97f1-4cb9-8ff7-d53741dc928c"


class PermanentExchangeError(Exception):
	"""Raised when New-MailContact fails for a structural reason that will
	reproduce on every retry: the SMTP address already belongs to another
	recipient in the tenant, or the Name collides with multiple existing
	directory objects.

	Carries a structured `kind` + `detail` so the engine can record a
	Conflict-status mapping and the UI can show a meaningful reason instead
	of the raw 409/500 cmdlet payload.
	"""

	def __init__(self, kind: str, detail: str):
		self.kind = kind
		self.detail = detail
		super().__init__(f"{kind}: {detail}")


# Robust against 0 or N backslash-escape layers around the quote chars:
# Exchange's response is JSON-encoded, and depending on whether we look at
# resp.text or a re-serialized version, the quote may appear as ", \", \\", …
_PROXY_RE = re.compile(r'LegacyExchangeDN of \\*"([^"\\]+?)\\*"')
_PROXY_SMTP_RE = re.compile(r'SMTP:([^\\"]+?)\\*"')
_IDENTITY_RE = re.compile(r'matching identity \\*"([^"\\]+?)\\*"')


def _classify_permanent_error(exc: Exception) -> "PermanentExchangeError | None":
	"""Inspect an exception from _invoke_command. If it represents a permanent
	(no-retry-will-help) conflict, return a PermanentExchangeError with structured
	info. Else return None — caller should treat as transient and re-raise.
	"""
	msg = str(exc)
	if "ProxyAddressExistsException" in msg or "ProxyAddressExists" in msg:
		smtp_m = _PROXY_SMTP_RE.search(msg)
		guid_m = _PROXY_RE.search(msg)
		parts = []
		if smtp_m:
			parts.append(f"SMTP {smtp_m.group(1)}")
		if guid_m:
			parts.append(f"already used by object {guid_m.group(1)}")
		detail = "; ".join(parts) or "another recipient in the tenant already owns this SMTP"
		return PermanentExchangeError("ProxyAddressExists", detail)
	if "multiple recipients matching identity" in msg:
		ident_m = _IDENTITY_RE.search(msg)
		identity = ident_m.group(1) if ident_m else "unknown"
		return PermanentExchangeError(
			"AmbiguousIdentity",
			f"name '{identity}' matches multiple existing directory objects",
		)
	# Malformed SMTP — Exchange rejects the email syntactically. Won't fix on retry.
	if "is not an SMTP e-mail address" in msg or "RecipientTaskException" in msg and "SMTP" in msg:
		bad_m = re.search(r"external e-mail address ([^\s]+?) is not", msg)
		bad = bad_m.group(1) if bad_m else "unknown"
		return PermanentExchangeError(
			"InvalidEmailAddress",
			f"'{bad}' is not a valid SMTP address (likely typo/special characters in source)",
		)
	# Soft-deleted recipient holds the proxy address: Exchange can't return a fresh
	# ExternalDirectoryObjectId because the slot is reserved. Won't resolve without
	# tenant-admin cleanup of the soft-deleted user.
	if "ExternalDirectoryObjectId was not returned" in msg:
		return PermanentExchangeError(
			"SoftDeletedRecipient",
			"SMTP is reserved by a soft-deleted recipient; tenant admin must release it",
		)
	return None


def _unique_contact_name(email: str, display_name: str) -> str:
	"""Build a unique `Name` (CN) for New-MailContact.

	Exchange treats `Name` as the canonical Identity. Using the DisplayName here
	is unreliable: two external contacts with identical names (very common —
	„Andreas Meyer", „Markus Schwarz") collide and New-MailContact fails with
	`500 multiple recipients matching identity`. The SMTP address is unique by
	construction (one MailContact per SMTP), so we use it as the Name. The
	visible DisplayName stays human-readable via the separate -DisplayName
	parameter, so Outlook users see no change.

	AD CN limit is 64 chars; emails almost always fit. For outliers we hash.
	"""
	if email and len(email) <= 64:
		return email
	import hashlib
	suffix = hashlib.sha256((email or display_name or "").encode("utf-8")).hexdigest()[:8]
	base = (display_name or email or "contact")[:50]
	return f"{base}_{suffix}"


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
		return data.get("value", [])

	# Defensive: loop should always either return or raise
	if last_exc:
		raise last_exc
	raise Exception(f"Exchange {cmdlet_name}: unreachable")


def _invoke_command_all(tenant, cmdlet_name: str, parameters: dict) -> list[dict]:
	"""Fetch all results for a Get-* cmdlet, working around the 1000-per-page
	hard limit on the InvokeCommand endpoint.

	Strategy: issue one normal call. If exactly 1000 results come back (= the
	server truncated), split into alphabetic halves by DisplayName and recurse.
	This typically needs 3–5 API calls for ~2000 contacts — much cheaper than
	26 per-letter calls.
	"""
	results = _invoke_command(tenant, cmdlet_name, {**parameters, "ResultSize": "Unlimited"})
	if len(results) < 1000:
		return results

	# Hit the 1000-cap. Split alphabetically.
	return _invoke_command_split(tenant, cmdlet_name, parameters, filter_expr=None, depth=0)


def _invoke_command_split(tenant, cmdlet_name, parameters, filter_expr, depth):
	"""Recursively split a Get-* cmdlet call into alphabetic sub-ranges until
	each sub-range returns <1000 results."""
	params = {**parameters, "ResultSize": "Unlimited"}
	if filter_expr:
		params["Filter"] = filter_expr

	results = _invoke_command(tenant, cmdlet_name, params)

	if len(results) < 1000 or depth > 5:
		# Under the limit, or we've split deep enough — accept what we have
		return results

	# Find the midpoint letter from the results
	names = sorted(r.get("DisplayName") or "" for r in results)
	mid_name = names[len(names) // 2]
	mid_char = mid_name[0].upper() if mid_name else "M"

	# Build sub-filters
	if filter_expr and "-and" not in filter_expr and "-ge" in filter_expr:
		# Existing lower bound — add upper bound for left half
		left_filter = f"{filter_expr} -and DisplayName -lt '{mid_char}'"
		right_filter = f"DisplayName -ge '{mid_char}'"
		# Preserve any original upper bound
		if "-lt" in filter_expr:
			parts = filter_expr.split("-lt")
			right_filter += f" -and DisplayName -lt{parts[-1]}"
	elif filter_expr and "-lt" in filter_expr and "-ge" not in filter_expr:
		# Existing upper bound only
		left_filter = f"DisplayName -lt '{mid_char}'"
		right_filter = f"DisplayName -ge '{mid_char}' -and {filter_expr}"
	else:
		left_filter = f"DisplayName -lt '{mid_char}'"
		right_filter = f"DisplayName -ge '{mid_char}'"

	left = _invoke_command_split(tenant, cmdlet_name, parameters, left_filter, depth + 1)
	right = _invoke_command_split(tenant, cmdlet_name, parameters, right_filter, depth + 1)
	return left + right


def fetch_all_mail_contacts(tenant) -> list[dict]:
	"""Fetch all mail contacts from Exchange Online. Returns normalized contact dicts.

	Merges data from Get-MailContact (email, alias) and Get-Contact (rich fields).
	Uses _invoke_command_all to handle the 1000-per-page limit transparently.
	"""
	mail_contacts = _invoke_command_all(tenant, "Get-MailContact", {})
	rich_contacts = _invoke_command_all(tenant, "Get-Contact", {})

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

	# Build New-MailContact params — include FirstName/LastName directly.
	# Name (CN) is constructed to be unique even when DisplayName collides; see
	# _unique_contact_name's docstring.
	new_params = {
		"Name": _unique_contact_name(email, display_name),
		"ExternalEmailAddress": email,
		"DisplayName": display_name,
	}
	if contact_data.get("given_name"):
		new_params["FirstName"] = contact_data["given_name"]
	if contact_data.get("surname"):
		new_params["LastName"] = contact_data["surname"]

	try:
		results = _invoke_command(tenant, "New-MailContact", new_params)
	except Exception as e:
		perm = _classify_permanent_error(e)
		if perm is not None:
			raise perm from e
		raise

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
		"Name": _unique_contact_name(email, display_name),
		"ExternalEmailAddress": email,
		"DisplayName": display_name,
	}
	if contact_data.get("given_name"):
		params["FirstName"] = contact_data["given_name"]
	if contact_data.get("surname"):
		params["LastName"] = contact_data["surname"]

	try:
		results = _invoke_command(tenant, "New-MailContact", params)
	except Exception as e:
		perm = _classify_permanent_error(e)
		if perm is not None:
			raise perm from e
		raise
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
