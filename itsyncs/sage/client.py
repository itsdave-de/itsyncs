"""SQL Server connection management for Sage Office Line databases."""

from __future__ import annotations

import pymssql


def get_sage_connection(connector) -> pymssql.Connection:
	"""Create a SQL Server connection from an ITSync Connector document.

	Uses host:port format which works for both default and named instances.
	The caller is responsible for closing the connection.
	"""
	import frappe

	if isinstance(connector, str):
		connector = frappe.get_doc("ITSync Connector", connector)

	password = connector.get_password("sql_password")
	return pymssql.connect(
		server=f"{connector.sql_host}:{connector.sql_port}",
		database=connector.sql_database,
		user=connector.sql_username,
		password=password,
		charset="utf8",
		login_timeout=10,
	)


def test_sage_connection(connector) -> dict:
	"""Test connectivity and return basic stats.

	Returns dict with keys: addresses, contacts, server_version.
	"""
	conn = get_sage_connection(connector)
	try:
		cursor = conn.cursor()

		cursor.execute("SELECT @@VERSION")
		version_row = cursor.fetchone()
		server_version = (version_row[0] or "").split("\n")[0] if version_row else "Unknown"

		cursor.execute(
			"SELECT COUNT(*) FROM KHKAdressen WHERE Mandant = %s AND Aktiv = -1",
			(connector.sql_mandant,),
		)
		address_count = cursor.fetchone()[0]

		cursor.execute(
			"SELECT COUNT(*) FROM KHKAnsprechpartner WHERE Mandant = %s",
			(connector.sql_mandant,),
		)
		contact_count = cursor.fetchone()[0]

		return {
			"addresses": address_count,
			"contacts": contact_count,
			"server_version": server_version,
		}
	finally:
		conn.close()


def get_max_rowversion(connector) -> int:
	"""Get the current maximum rowversion across KHKAdressen and KHKAnsprechpartner.

	Used to initialize the delta token after a full sync.
	"""
	conn = get_sage_connection(connector)
	try:
		cursor = conn.cursor()
		max_rv = 0

		cursor.execute(
			"SELECT MAX(CONVERT(bigint, Timestamp)) FROM KHKAdressen WHERE Mandant = %s",
			(connector.sql_mandant,),
		)
		row = cursor.fetchone()
		if row and row[0]:
			max_rv = max(max_rv, row[0])

		cursor.execute(
			"SELECT MAX(CONVERT(bigint, Timestamp)) FROM KHKAnsprechpartner WHERE Mandant = %s",
			(connector.sql_mandant,),
		)
		row = cursor.fetchone()
		if row and row[0]:
			max_rv = max(max_rv, row[0])

		return max_rv
	finally:
		conn.close()
