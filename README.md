<div align="center">

# apollo-mcp

**[Apollo.io][apollo] tools for agents, MCP clients, and the command line.**

[![Tests](https://img.shields.io/badge/tests-288%20passing-brightgreen.svg)](#testing)
[![Coverage](https://img.shields.io/badge/coverage-92%25-brightgreen.svg)](#testing)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-FastMCP%203-6f42c1.svg)][fastmcp]
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

</div>

---

`apollo-mcp` exposes Apollo's B2B data APIs through a [Model Context Protocol][mcp]
server built with [FastMCP 3][fastmcp]. It also ships `apollo-cli`, a small
command-line wrapper for the most common search and enrichment workflows.

## Highlights

- **45 MCP tools** spanning prospect search, enrichment, CRM records, deals,
  tasks, sequences, email engagement, phone calls, and API usage stats.
- **Two transports** — stdio for Claude Desktop, Claude Code, and local MCP
  clients; Streamable HTTP for hosted or remote deployments.
- **Per-request auth in HTTP mode** so one environment key is never shared
  across remote clients or organizations.
- **Defensive request construction** for Apollo's endpoint-specific wire-format
  quirks (see [Apollo Wire-Format Notes](#apollo-wire-format-notes)).
- **A CLI** for quick search and enrichment without writing any code.
- **288 tests** — 255 unit plus 33 live integration tests, with the unit suite
  alone covering most of the package.

## Contents

- [Quickstart](#quickstart)
- [Installation](#installation)
- [Configuration](#configuration)
- [MCP Server](#mcp-server)
- [CLI](#cli)
- [Tool Catalog](#tool-catalog)
- [Apollo Wire-Format Notes](#apollo-wire-format-notes)
- [Development](#development)
- [Testing](#testing)
- [Security](#security)
- [Contributing](#contributing)
- [License](#license)

## Quickstart

```bash
git clone https://github.com/dyngai/apollo-mcp.git
cd apollo-mcp
uv sync --extra dev

cp env.example .env        # then set APOLLO_API_KEY in .env
```

Run the MCP server over stdio:

```bash
uv run apollo-mcp
```

Or run a CLI command:

```bash
uv run apollo-cli search-people \
  --person_titles "Manager,Director" \
  --person_locations "Virginia"
```

## Installation

### With uv

[`uv`][uv] is the recommended workflow for local development:

```bash
uv sync                 # runtime dependencies
uv sync --extra dev     # plus pytest, respx, coverage
```

### With pip

```bash
pip install -e .
pip install -e ".[dev]"
```

Both install the `apollo-mcp` and `apollo-cli` console scripts.

## Configuration

`python-dotenv` is loaded automatically, so a local `.env` file works for stdio
and CLI usage.

| Variable | Required | Default | Notes |
| --- | :---: | --- | --- |
| `APOLLO_API_KEY` | stdio / CLI | — | Apollo API key used by local clients. |
| `APOLLO_BASE_URL` | No | `https://api.apollo.io/api/v1` | Override for testing or proxies. |

> [!NOTE]
> Several Apollo endpoints require a **master** API key — user and stage
> listings, deal mutations, sequence search, and API usage stats. Non-master
> keys generally receive `403 API_INACCESSIBLE` on those endpoints. Tools that
> need one say so in their description.

## MCP Server

```bash
apollo-mcp                               # stdio (default)
apollo-mcp --transport http              # HTTP on 127.0.0.1:8000/mcp/
apollo-mcp --transport http --host 0.0.0.0 --port 9000 --path /api/mcp/
apollo-mcp --transport sse --port 8001   # legacy SSE transport
apollo-mcp --version
apollo-mcp --help
```

| Flag | Default | Description |
| --- | --- | --- |
| `--transport` | `stdio` | One of `stdio`, `http`, or `sse`. |
| `--host` | `127.0.0.1` | HTTP/SSE bind address. |
| `--port` | `8000` | HTTP/SSE port. |
| `--path` | `/mcp/` | HTTP path prefix. FastMCP preserves the trailing slash. |

### Claude Desktop / Claude Code

For stdio-based clients, point the MCP client at `apollo-mcp` and pass the
Apollo key in the server environment:

```json
{
  "mcpServers": {
    "apollo": {
      "command": "apollo-mcp",
      "env": {
        "APOLLO_API_KEY": "your-apollo-api-key",
        "APOLLO_BASE_URL": "https://api.apollo.io/api/v1"
      }
    }
  }
}
```

You can also use a virtualenv-relative command such as `.venv/bin/apollo-mcp`,
or run through `uvx`.

### HTTP / Remote Clients

```bash
apollo-mcp --transport http --host 0.0.0.0 --port 8000
```

The endpoint is then available at `http://<host>:8000/mcp/`, and every request
must carry its own Apollo key:

```http
Authorization: Bearer <apollo-api-key>
```

> [!IMPORTANT]
> In HTTP mode the bearer token is **required** and `APOLLO_API_KEY` is never
> used as a fallback, even when it is set in the server environment. That keeps
> a shared environment key from leaking across organizations. Requests without
> a bearer token fail with a clear "Authentication required" error.

Each distinct bearer token gets one long-lived `ApolloClient`, so HTTP
connections and TLS sessions are pooled and reused across tool calls rather
than renegotiated every time. The cache is bounded, and the least-recently-used
client is closed on eviction.

If you deploy behind a reverse proxy or TLS terminator, forward the configured
`--path` prefix unchanged.

### Docker

```bash
docker build -t apollo-mcp .
docker run --rm -p 8080:8080 apollo-mcp
```

The container starts the HTTP transport on `0.0.0.0:8080/mcp/` as a non-root
user. Clients still need to send `Authorization: Bearer <apollo-api-key>`.

## CLI

The CLI covers the highest-leverage search and enrichment commands.

| Command | Purpose |
| --- | --- |
| `search-people` | Search for people. Requires `--person_titles`. |
| `search-companies` | Search for companies. |
| `enrich-person` | Enrich a person by email, LinkedIn URL, or name + company. |
| `enrich-company` | Enrich a company by domain or name. |
| `bulk-enrich-people` | Bulk person enrichment via `--json`. |
| `bulk-enrich-companies` | Bulk company enrichment via `--json`. |
| `org-jobs` | Job postings for an organization. |
| `org-info` | Full organization profile by ID. |
| `search-news` | Search news articles. |

```bash
# Search for people. Apollo requires at least person_titles for this flow.
apollo-cli search-people \
  --person_titles "Manager,Director" \
  --person_locations "Virginia"

# Find a specific person by enrichment instead of broad search.
apollo-cli enrich-person \
  --first_name "John" \
  --last_name "Doe" \
  --organization_name "Company"

# Other examples.
apollo-cli enrich-person --email "tim@apollo.io"
apollo-cli search-companies --q "technology" --organization_num_employees_ranges "51,200"
apollo-cli org-jobs --id "5e66b6381e05b4008c8331b8" --page 2 --per_page 50
```

### Argument conventions

- `--key value` and `--key=value` are both supported.
- Comma-separated values become arrays: `--person_titles "CEO,CTO,VP"`.
- Employee ranges keep their comma as **one** range:
  `--organization_num_employees_ranges "11,20"` becomes `["11,20"]`.
- Complex request bodies can be passed with `--json '{"q":"test","page":1}'`.
- Bare digits become integers (`--page 2`), but values with a leading zero stay
  strings so identifiers like `--postal_code 02134` are preserved intact.

```bash
apollo-cli --help
apollo-cli <command> --help
```

## Tool Catalog

<details open>
<summary><b>Prospect Search and Enrichment</b> — 13 tools</summary>

| Tool | Purpose |
| --- | --- |
| `apollo_search_people` | People search with Apollo-compatible filter mapping. |
| `apollo_search_companies` | Company search with location, size, revenue, funding, jobs, and technology filters. |
| `apollo_enrich_person` | Single-person enrichment with compact output by default. |
| `apollo_reveal_person` | Reveal full contact info from an Apollo person ID. |
| `apollo_get_person` | Look up a person by Apollo ID. Use `full=true` for raw payloads. |
| `apollo_enrich_company` | Single-company enrichment by domain or name. |
| `apollo_bulk_enrich_people` | Bulk people enrichment. |
| `apollo_bulk_enrich_organizations` | Bulk organization enrichment. Requires at least one `domain`. |
| `apollo_get_organization_job_postings` | Job postings for an organization. |
| `apollo_get_complete_organization_info` | Full organization profile by ID. |
| `apollo_get_organization_top_people` | Top contacts at an organization. |
| `apollo_search_news_articles` | News article search. Requires `organization_ids`. |
| `apollo_add_to_my_prospects` | Save people to "My Prospects". |

</details>

<details>
<summary><b>CRM Contacts</b> — 7 tools</summary>

| Tool | Purpose |
| --- | --- |
| `apollo_search_contacts` | Search your Apollo CRM contacts. |
| `apollo_match_contact` | Match a single CRM contact. |
| `apollo_bulk_match_contacts` | Bulk-match CRM contacts. Rate limit: 100/hour, 20/minute. |
| `apollo_create_contact` | Create a single CRM contact. |
| `apollo_update_contact` | Update a CRM contact. |
| `apollo_bulk_create_contacts` | Create contacts in bulk (up to 100 per call). |
| `apollo_list_contact_stages` | List configured contact-stage IDs. |

</details>

<details>
<summary><b>CRM Accounts</b> — 5 tools</summary>

| Tool | Purpose |
| --- | --- |
| `apollo_search_accounts` | Search your Apollo CRM accounts. |
| `apollo_create_account` | Create a single CRM account. |
| `apollo_update_account` | Update a CRM account. |
| `apollo_bulk_create_accounts` | Create accounts in bulk. |
| `apollo_list_account_stages` | List configured account-stage IDs. |

</details>

<details>
<summary><b>Deals</b> — 4 tools</summary>

| Tool | Purpose |
| --- | --- |
| `apollo_search_deals` | Search deals/opportunities. |
| `apollo_create_deal` | Create a deal. Requires a master API key. |
| `apollo_update_deal` | Update an existing deal. Requires a master API key. |
| `apollo_list_deal_stages` | List configured pipeline stages. Requires a master API key. |

</details>

<details>
<summary><b>Tasks</b> — 4 tools</summary>

| Tool | Purpose |
| --- | --- |
| `apollo_create_task` | Create one task scoped to one or more contacts. |
| `apollo_search_tasks` | Search tasks. |
| `apollo_bulk_create_tasks` | Create one task per contact in a list. |
| `apollo_bulk_complete_tasks` | Mark multiple tasks complete. |

</details>

<details>
<summary><b>Sequences</b> — 6 tools</summary>

| Tool | Purpose |
| --- | --- |
| `apollo_search_sequences` | Find emailer sequences/campaigns. Requires a master API key. |
| `apollo_add_contacts_to_sequence` | Add contacts to a sequence. |
| `apollo_remove_contacts_from_sequence` | Remove or stop contacts in sequences. |
| `apollo_approve_sequence` | Approve a sequence to start sending. |
| `apollo_abort_sequence` | Abort an active sequence. |
| `apollo_archive_sequence` | Archive a sequence. |

</details>

<details>
<summary><b>Email Engagement, Phone Calls, and Admin</b> — 6 tools</summary>

| Tool | Purpose |
| --- | --- |
| `apollo_search_emailer_messages` | Search sent emailer messages. |
| `apollo_get_emailer_message_activities` | Get opens, clicks, replies, and bounces for a message. |
| `apollo_create_phone_call` | Log a phone call. |
| `apollo_update_phone_call` | Update a phone-call record. |
| `apollo_list_users` | List teammates. Requires a master API key. |
| `apollo_get_api_usage_stats` | View per-endpoint usage and rate limits. Requires a master API key. |

</details>

## Apollo Wire-Format Notes

Apollo's API has several endpoint-specific request shapes that this package
handles explicitly.

**Request construction**

- People-search filters are sent as URL query parameters with bracket notation:
  `person_titles[]=VP&person_titles[]=CTO`. Sending them in the JSON body is
  silently ignored and returns arbitrary results.
- People-search aliases such as `q`, `company_domains`, and
  `q_organization_domains` are normalized to the current Apollo fields
  `q_keywords` and `q_organization_domains_list[]`. An explicitly supplied
  canonical field always wins over its alias.
- Range filters serialize with bracketed sub-keys — `revenue_range[min]=100` —
  and booleans render lowercase (`true`/`false`) as Apollo expects.
- Company employee ranges accept dash format at the MCP boundary (`"11-20"`)
  and are normalized to Apollo's comma format (`"11,20"`).
- Apollo uses `PATCH` to update contacts, accounts, deals, and phone calls.
- `bulk_enrich_organizations` expects `{"domains": [...]}`, not objects;
  `bulk_enrich_people` expects `{"details": [...]}`, not `{"people": [...]}`.
- Caller-supplied IDs interpolated into URL paths are URL-encoded to prevent
  path traversal and query-string injection.

**Pagination**

`per_page` is capped at **100**. Apollo rejects larger values outright with
HTTP 422 (`Per page not supported` on people search, `Per Page Limit Crossed.`
on company search) rather than clamping them, so the MCP schema enforces
`1–100` before a request is ever sent.

**Responses**

- `apollo_search_people` and `apollo_search_companies` always return compact
  per-result summaries so MCP clients do not receive unnecessarily large
  payloads.
- `apollo_enrich_person` and `apollo_get_person` are compact by default but
  accept `full=true` for the raw Apollo record — those can exceed 100 KB and
  may overflow MCP token limits, so reach for it only when you need the
  complete `employment_history` / `current_technologies` / organization block.
- The lower-level `ApolloClient` never trims; it always returns the raw
  response.
- Endpoints that answer `204 No Content` — sequence approve, abort, and archive,
  and bulk task completion — decode to `{}` rather than raising.
- A non-JSON body (for example an upstream HTML error page returned with a 200)
  raises an `ApolloError` naming the status, content type, and a body snippet,
  instead of a bare JSON decoding error.

## Development

```text
apollo_mcp/
  apollo.py     # ApolloClient: httpx wrapper, errors, redirects, query strings
  server.py     # FastMCP server and all tool registrations
  cli.py        # apollo-cli argument parsing and command dispatch

tests/
  test_apollo.py        # respx-mocked client tests
  test_server.py        # FastMCP tool tests with a fake Apollo client
  test_cli.py           # subprocess and in-process CLI tests
  test_integration.py   # live Apollo API tests, auto-skip without a key
```

```bash
uv sync --extra dev      # install development dependencies
uv run apollo-mcp        # run the server locally
uv run apollo-cli --help # run the CLI locally
```

## Testing

```bash
# Unit tests only — no Apollo key required.
pytest -m "not integration"

# Live integration tests only.
APOLLO_API_KEY=... pytest -m integration

# Everything.
APOLLO_API_KEY=... pytest

# With coverage.
APOLLO_API_KEY=... pytest --cov=apollo_mcp --cov-report=term-missing
```

The integration suite is marked `integration` and auto-skips when
`APOLLO_API_KEY` is not set. Those tests are read-only — searches, enrichments,
and stage listings — so they do not mutate CRM data, though enrichment calls do
consume Apollo credits.

| Suite | Tests | Notes |
| --- | ---: | --- |
| Unit | 255 | Mocked transports, no network. |
| Integration | 33 | Live Apollo API, requires a key. |
| **Total** | **288** | Both run in well under a minute. |

The unit suite covers 92% of the package on its own; the badge above tracks
that figure, since it is the one reproducible without an Apollo key.

> [!NOTE]
> The test counts and coverage percentage in this README are generated, not
> hand-maintained. After changing the suite, run:
>
> ```bash
> python scripts/coverage_badge.py --write   # refresh them
> python scripts/coverage_badge.py --check   # verify (CI runs this)
> ```

## Security

- Do not commit Apollo API keys.
- `.env` and `.mcp.json` are gitignored because they commonly contain secrets.
- API keys are never intentionally logged by the package.
- HTTP transport requires `Authorization: Bearer <apollo-api-key>` per request
  and never falls back to a server-side environment key.
- Path traversal and query-string injection regressions are covered in
  `tests/test_apollo.py`.

If you open an issue, remove API keys, emails, tokens, and other sensitive
customer data from logs and screenshots first.

## Contributing

Contributions are welcome.

1. Fork the repository.
2. Create a feature branch.
3. Add or update tests for behavioral changes.
4. Run the relevant commands from [Testing](#testing).
5. Open a pull request with a concise description of the change.

For API-shape changes, prefer tests that assert the exact HTTP method, path,
query string, and JSON body sent to Apollo.

## License

MIT. See [LICENSE](LICENSE).

[apollo]: https://docs.apollo.io/reference/
[fastmcp]: https://github.com/PrefectHQ/fastmcp
[mcp]: https://modelcontextprotocol.io/
[uv]: https://github.com/astral-sh/uv
