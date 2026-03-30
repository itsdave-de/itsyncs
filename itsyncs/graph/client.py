import asyncio
import time

import frappe
from azure.identity.aio import ClientSecretCredential
from msgraph import GraphServiceClient


# Module-level token cache: {tenant_id: (token_str, expiry_timestamp)}
_token_cache: dict[str, tuple[str, float]] = {}


def get_graph_client(tenant) -> GraphServiceClient:
	"""Create an authenticated GraphServiceClient for the given tenant."""
	if isinstance(tenant, str):
		tenant = frappe.get_doc("ITSync Tenant", tenant)

	credential = ClientSecretCredential(
		tenant_id=tenant.tenant_id,
		client_id=tenant.client_id,
		client_secret=tenant.get_password("client_secret"),
	)

	scopes = ["https://graph.microsoft.com/.default"]
	client = GraphServiceClient(credentials=credential, scopes=scopes)
	client._tenant_name = tenant.name  # Used by _get_tenant_from_client for httpx fallback
	return client


def get_graph_token(tenant) -> str:
	"""Get a cached access token for direct HTTP calls (synchronous)."""
	if isinstance(tenant, str):
		tenant = frappe.get_doc("ITSync Tenant", tenant)

	key = tenant.tenant_id
	now = time.time()

	# Return cached token if still valid (with 5 min buffer)
	if key in _token_cache:
		token_str, expiry = _token_cache[key]
		if now < expiry - 300:
			return token_str

	# Fetch new token
	async def _get_token():
		credential = ClientSecretCredential(
			tenant_id=tenant.tenant_id,
			client_id=tenant.client_id,
			client_secret=tenant.get_password("client_secret"),
		)
		try:
			token = await credential.get_token("https://graph.microsoft.com/.default")
			return token.token, token.expires_on
		finally:
			await credential.close()

	token_str, expiry = run_async(_get_token())
	_token_cache[key] = (token_str, expiry)
	return token_str


def run_async(coro):
	"""Run an async coroutine in a synchronous context.

	The msgraph-sdk is fully async. Since Frappe is synchronous,
	we need this helper to bridge the gap.
	"""
	try:
		loop = asyncio.get_event_loop()
		if loop.is_running():
			import concurrent.futures

			with concurrent.futures.ThreadPoolExecutor() as pool:
				return pool.submit(asyncio.run, coro).result()
		else:
			return loop.run_until_complete(coro)
	except RuntimeError:
		return asyncio.run(coro)
