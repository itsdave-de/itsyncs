# ITSync

Frappe app for synchronizing contacts between different sources — Sage Office Line, Exchange Online Mailboxes, Shared Mailboxes, and the Global Address List (GAL).

## Features

- **Multi-Source** — Sage SQL (read-only), Exchange Mailboxes, Shared Mailboxes, GAL
- **Incremental Sync** — Change detection via SQL Server rowversion / Graph delta queries
- **Normalization** — Automatic phone (E.164), email, and name cleanup for Sage data
- **Scheduled Sync** — Configurable intervals with background execution
- **Matching** — Email, Email+Name, or Fuzzy matching to prevent duplicates
- **Backup** — Export contacts as VCF, CSV, or JSON

## Architecture

```
Source Connectors              Sync Engine              Target Connectors
┌─────────────────┐                                    ┌─────────────────┐
│   Sage SQL      │──┐                            ┌──▶│ Shared Mailbox  │
│   (read-only)   │  │    ┌──────────────────┐    │   └─────────────────┘
└─────────────────┘  ├──▶│   Normalize       │────┤   ┌─────────────────┐
┌─────────────────┐  │   │   Match           │    ├──▶│   Mailbox       │
│   Mailbox       │──┤   │   Create/Update   │    │   └─────────────────┘
└─────────────────┘  │   │   Delete          │    │   ┌─────────────────┐
┌─────────────────┐  │   └──────────────────┘    └──▶│   GAL           │
│   GAL           │──┘                                └─────────────────┘
└─────────────────┘
```

## Module Structure

```
itsyncs/
├── graph/              # Microsoft Graph API (OAuth, Contacts, Exchange, GAL)
├── sage/               # Sage Office Line (SQL Server, normalization)
├── sync/               # Sync engine, matching, backup
├── itsyncs/doctype/    # Frappe DocTypes (Tenant, Connector, Pair, Mapping, Log, Backup)
└── tasks.py            # Scheduled background sync
```

## Installation

```bash
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app itsyncs
```

For Sage SQL connectors, install FreeTDS: `sudo apt install freetds-dev`

Requires Frappe v16, Python 3.14+, Node.js 24+.

## Setup & Configuration

**[Setup Guide with Screenshots](docs/itsyncs-sage-setup-guide.html)**

## Azure AD Permissions

| Permission (Application) | Required for |
|---------------------------|-------------|
| `Contacts.ReadWrite` | Mailbox / Shared Mailbox |
| `User.Read.All` | All Exchange connectors |
| `Exchange.ManageAsApp` | GAL write access only |

Admin consent must be granted. GAL write additionally requires the Exchange Administrator directory role.

## License

GPL-3.0
