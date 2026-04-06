# ITSync

Frappe app for synchronizing contacts between different sources — Sage Office Line, Exchange Online Mailboxes, Shared Mailboxes, and the Global Address List (GAL).

## Features

- **Sage SQL Source** — Read contacts directly from Sage Office Line databases via SQL Server
- **Exchange Online** — Sync to/from Mailboxes, Shared Mailboxes, and the GAL
- **Automatic Normalization** — Phone numbers (E.164), email addresses, name cleanup, junk filtering
- **Incremental Sync** — Efficient change detection via SQL Server rowversion and Microsoft Graph delta queries
- **Scheduled Sync** — Configurable intervals (15 min to daily) with automatic background execution
- **Matching** — Email, Email+Name, or Fuzzy matching to prevent duplicates
- **Logging** — Full sync history with created/updated/deleted/error counts
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

## Connector Types

| Type | Direction | Authentication | Use Case |
|------|-----------|----------------|----------|
| **Sage SQL** | Source only | SQL Server login | Sage Office Line ERP contacts |
| **Shared Mailbox** | Source + Target | Azure AD App (Graph API) | Team contact directories |
| **Mailbox** | Source + Target | Azure AD App (Graph API) | Personal mailbox contacts |
| **GAL** | Source + Target | Azure AD App (Exchange Online) | Global Address List |

## DocTypes

| DocType | Purpose |
|---------|---------|
| **ITSync Tenant** | Azure AD app registration credentials |
| **ITSync Connector** | Source/target connection (Sage SQL, Mailbox, GAL) |
| **ITSync Pair** | Links source → target, configures schedule and behavior |
| **ITSync Mapping** | Tracks source ↔ target contact relationships |
| **ITSync Log** | Sync execution history with stats and error details |
| **ITSync Backup** | Contact backup snapshots (VCF/CSV/JSON) |

## Module Structure

```
itsyncs/
├── graph/              # Microsoft Graph API integration
│   ├── client.py       # OAuth token management, Graph client
│   ├── contacts.py     # Mailbox contact CRUD, delta queries
│   ├── exchange.py     # Exchange Online GAL operations
│   └── gal.py          # Azure AD user/org contact fetching
├── sage/               # Sage Office Line integration
│   ├── client.py       # SQL Server connection management
│   ├── contacts.py     # Contact queries, delta sync, format conversion
│   └── normalizer.py   # Phone, email, name normalization and junk filtering
├── sync/               # Core sync engine
│   ├── engine.py       # Initial sync, incremental sync, preview
│   ├── matching.py     # Contact matching (email, name, fuzzy)
│   └── backup.py       # VCF/CSV/JSON export
└── tasks.py            # Scheduled background sync
```

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app itsyncs
```

### Requirements

- **Frappe v16** with Python 3.14+
- **Node.js 24+**
- For Sage SQL connectors: `freetds-dev` system package (for pymssql)

```bash
# On Debian/Ubuntu
sudo apt install freetds-dev
```

## Setup Guide

For step-by-step setup instructions with screenshots, see:

**[Sage SQL → Exchange Setup Guide](docs/itsyncs-sage-setup-guide.html)**

Quick overview of the setup steps:

1. **Create ITSync Tenant** — Azure AD app credentials (Tenant ID, Client ID, Secret)
2. **Create Source Connector** — Sage SQL with database connection details
3. **Create Target Connector** — Exchange Shared Mailbox with email address
4. **Create Sync Pair** — Link source to target, configure schedule
5. **Generate Preview** — Verify contact counts before syncing
6. **Run Initial Sync** — Create all contacts in the target
7. **Enable Auto Sync** — Automatic incremental syncs on schedule

## Azure AD Permissions

### For Mailbox / Shared Mailbox connectors

| Permission (Application) | Purpose |
|---------------------------|---------|
| `Contacts.ReadWrite` | Read/write contacts in mailboxes |
| `User.Read.All` | Resolve mailbox addresses |

### For GAL connectors (additional)

| Permission (Application) | Purpose |
|---------------------------|---------|
| `Exchange.ManageAsApp` | Write mail contacts to GAL |

Plus: Enterprise App "Office 365 Exchange Online" and Exchange Administrator directory role.

## Sage SQL Contact Normalization

The Sage connector automatically cleans contact data during import:

| Issue | Fix |
|-------|-----|
| Phone formats (`(05146) 987269`, `040/78961-119`) | Normalized to E.164 (`+495146987269`) |
| Email issues (whitespace, `(at)`, broken TLDs) | Auto-corrected |
| Functional entries ("Rechnungen", "INFO", "AVIS") | Filtered out |
| Order numbers as contact names | Filtered out |
| Missing country code | Defaults to `DE` |
| Homepage without protocol | `https://` prepended |

## Contributing

This app uses `pre-commit` for code formatting and linting:

```bash
cd apps/itsyncs
pre-commit install
```

Tools: ruff, eslint, prettier, pyupgrade

## License

GPL-3.0
