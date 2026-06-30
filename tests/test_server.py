"""Tests for the FastMCP server tools.

Exercises every registered tool by calling its underlying function
through FastMCP's in-memory tool manager with a fake ApolloClient.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from apollo_mcp.apollo import ApolloError
from apollo_mcp.server import build_server
from tests import fixtures


def _fake_client() -> MagicMock:
    return MagicMock()


# ---- main() arg parser ----------------------------------------------------


def test_parse_args_defaults_to_stdio():
    from apollo_mcp.server import parse_args

    ns = parse_args([])
    assert ns.transport == "stdio"
    assert ns.host == "127.0.0.1"
    assert ns.port == 8000
    assert ns.path == "/mcp/"


def test_parse_args_http_with_overrides():
    from apollo_mcp.server import parse_args

    ns = parse_args(["--transport", "http", "--host", "0.0.0.0", "--port", "9000", "--path", "/api/mcp/"])
    assert ns.transport == "http"
    assert ns.host == "0.0.0.0"
    assert ns.port == 9000
    assert ns.path == "/api/mcp/"


def test_parse_args_sse_supported():
    from apollo_mcp.server import parse_args

    ns = parse_args(["--transport", "sse"])
    assert ns.transport == "sse"


def test_parse_args_rejects_unknown_transport(capsys):
    from apollo_mcp.server import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--transport", "websocket"])
    assert "invalid choice" in capsys.readouterr().err


def test_main_dispatches_stdio_when_no_args(monkeypatch):
    """main() with no args calls mcp.run() (stdio) without transport kwargs."""
    from apollo_mcp import server

    fake_mcp = MagicMock()
    monkeypatch.setattr(server, "build_server", lambda: (fake_mcp, MagicMock()))
    server.main([])
    fake_mcp.run.assert_called_once_with()


def test_main_dispatches_http_with_kwargs(monkeypatch):
    """main(['--transport', 'http']) forwards transport/host/port/path to mcp.run."""
    from apollo_mcp import server

    fake_mcp = MagicMock()
    monkeypatch.setattr(server, "build_server", lambda: (fake_mcp, MagicMock()))
    server.main(["--transport", "http", "--host", "0.0.0.0", "--port", "9001"])
    fake_mcp.run.assert_called_once_with(
        transport="http",
        host="0.0.0.0",
        port=9001,
        path="/mcp/",
    )


# ---- per-request bearer-token auth ----------------------------------------


def test_build_server_no_credentials_does_not_exit(monkeypatch):
    """In HTTP mode no startup APOLLO_API_KEY is required.

    The previous behaviour called sys.exit(1) which crash-looped Cloud Run.
    Now build_server should succeed; auth gets enforced per-request.
    """
    from apollo_mcp import server as srv

    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    mcp, apollo = srv.build_server()
    assert mcp.name == "apollo-mcp"
    assert apollo is not None  # proxy returned, not None


def test_resolve_client_uses_bearer_from_http_request(monkeypatch):
    """An HTTP request with Authorization: Bearer X yields a per-request
    ApolloClient keyed off that token."""
    from apollo_mcp import server as srv

    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    mcp, apollo = srv.build_server()

    request = MagicMock()
    request.headers = {"authorization": "Bearer per-request-key-123"}
    monkeypatch.setattr(srv, "get_http_request", lambda: request)

    resolved = apollo._resolver()
    assert resolved.api_key == "per-request-key-123"


class _FakeApolloClient:
    """Records construction/close so client-caching behaviour is observable."""

    def __init__(self, api_key, base_url=None):
        self.api_key = api_key
        self.base_url = base_url
        self.closed = False

    def search_people(self, _body):
        return {"people": [], "pagination": {}}

    def close(self):
        self.closed = True


def _http_server_with_fake_clients(monkeypatch, created):
    """build_server() wired to FakeApolloClient behind a mutable bearer header."""
    from apollo_mcp import server as srv

    def _factory(api_key, base_url=None):
        made = _FakeApolloClient(api_key, base_url)
        created.append(made)
        return made

    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    monkeypatch.setattr(srv, "ApolloClient", _factory)

    request = MagicMock()
    request.headers = {}
    monkeypatch.setattr(srv, "get_http_request", lambda: request)
    return srv, request


async def test_http_bearer_client_is_reused_across_calls(monkeypatch):
    """One ApolloClient per bearer token, kept alive so the httpx connection
    pool (and TLS session) survives between tool calls."""
    created: list = []
    srv, request = _http_server_with_fake_clients(monkeypatch, created)
    request.headers = {"authorization": "Bearer per-request-key-123"}

    mcp, _ = srv.build_server()
    for _ in range(4):
        await _call(mcp, "apollo_search_people", {})

    assert len(created) == 1, "client should be built once and reused"
    assert created[0].api_key == "per-request-key-123"
    assert created[0].closed is False, "cached client must stay open"


async def test_http_bearer_clients_are_isolated_per_token(monkeypatch):
    """Distinct tokens must never share a client (no cross-org key reuse)."""
    created: list = []
    srv, request = _http_server_with_fake_clients(monkeypatch, created)
    mcp, apollo = srv.build_server()

    request.headers = {"authorization": "Bearer token-aaa"}
    resolved_a = apollo._resolver()
    request.headers = {"authorization": "Bearer token-bbb"}
    resolved_b = apollo._resolver()
    request.headers = {"authorization": "Bearer token-aaa"}
    resolved_a_again = apollo._resolver()

    assert resolved_a is not resolved_b
    assert resolved_a.api_key == "token-aaa"
    assert resolved_b.api_key == "token-bbb"
    assert resolved_a_again is resolved_a


async def test_http_bearer_client_cache_is_bounded_and_closes_evicted(monkeypatch):
    """A stream of unique tokens must not grow the cache without limit; the
    least-recently-used client is closed when evicted."""
    created: list = []
    srv, request = _http_server_with_fake_clients(monkeypatch, created)
    mcp, apollo = srv.build_server()

    limit = srv._MAX_CACHED_TOKEN_CLIENTS
    for i in range(limit + 3):
        request.headers = {"authorization": f"Bearer token-{i}"}
        apollo._resolver()

    assert len(created) == limit + 3
    # The three oldest were evicted and closed; the rest stay open.
    assert [c.closed for c in created[:3]] == [True, True, True]
    assert all(c.closed is False for c in created[3:])


def test_resolve_client_http_without_bearer_raises(monkeypatch):
    """HTTP request without a bearer + no fallback → ToolError."""
    from apollo_mcp import server as srv
    from fastmcp.exceptions import ToolError

    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    mcp, apollo = srv.build_server()

    request = MagicMock()
    request.headers = {}
    monkeypatch.setattr(srv, "get_http_request", lambda: request)

    with pytest.raises(ToolError, match="Authentication required"):
        apollo._resolver()


def test_resolve_client_stdio_falls_back_to_env(monkeypatch):
    """No HTTP request context → env var APOLLO_API_KEY (stdio path)."""
    from apollo_mcp import server as srv

    monkeypatch.setenv("APOLLO_API_KEY", "env-key-abc")
    mcp, apollo = srv.build_server()

    def _no_request():
        raise RuntimeError("not in http context")

    monkeypatch.setattr(srv, "get_http_request", _no_request)
    resolved = apollo._resolver()
    assert resolved.api_key == "env-key-abc"


def test_resolve_client_injected_fake_wins_in_stdio(monkeypatch):
    """Stdio mode (no HTTP context) prefers the injected fake over env."""
    from apollo_mcp import server as srv

    monkeypatch.setenv("APOLLO_API_KEY", "should-not-be-used")
    fake = MagicMock()
    mcp, apollo = srv.build_server(client=fake)

    def _no_request():
        raise RuntimeError("not in http context")

    monkeypatch.setattr(srv, "get_http_request", _no_request)
    resolved = apollo._resolver()
    assert resolved is fake


def test_resolve_client_http_no_bearer_does_not_leak_env(monkeypatch):
    """HTTP request with env var set but no bearer must NOT use the env
    key — that would leak a shared key across orgs."""
    from apollo_mcp import server as srv
    from fastmcp.exceptions import ToolError

    monkeypatch.setenv("APOLLO_API_KEY", "shared-key-must-not-leak")
    mcp, apollo = srv.build_server()

    request = MagicMock()
    request.headers = {}
    monkeypatch.setattr(srv, "get_http_request", lambda: request)

    with pytest.raises(ToolError, match="Authentication required"):
        apollo._resolver()


@pytest.fixture
def server_and_client():
    fake = _fake_client()
    mcp, _ = build_server(client=fake)
    return mcp, fake


async def _call(mcp, name: str, arguments: dict) -> str:
    """Invoke a registered FastMCP tool and return its text payload."""
    result = await mcp.call_tool(name, arguments)
    # FastMCP returns a ToolResult; our tools return strings, so the
    # first text content block carries the JSON payload.
    if hasattr(result, "content") and result.content:
        return result.content[0].text
    if isinstance(result, str):
        return result
    return json.dumps(result)


# ---- list_tools ------------------------------------------------------------


async def test_all_tools_registered(server_and_client):
    mcp, _ = server_and_client
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        # Original 11
        "apollo_search_people",
        "apollo_search_companies",
        "apollo_enrich_person",
        "apollo_reveal_person",
        "apollo_enrich_company",
        "apollo_bulk_enrich_people",
        "apollo_bulk_enrich_organizations",
        "apollo_get_organization_job_postings",
        "apollo_get_complete_organization_info",
        "apollo_search_news_articles",
        "apollo_get_api_usage_stats",
        # Tier 1 additions
        "apollo_get_person",
        "apollo_search_contacts",
        "apollo_search_accounts",
        "apollo_search_deals",
        "apollo_create_deal",
        "apollo_update_deal",
        "apollo_list_deal_stages",
        "apollo_list_users",
        "apollo_create_task",
        "apollo_search_tasks",
        "apollo_bulk_complete_tasks",
        "apollo_search_sequences",
        "apollo_add_contacts_to_sequence",
        "apollo_get_organization_top_people",
        "apollo_add_to_my_prospects",
        # Tier 2 additions
        "apollo_create_contact",
        "apollo_update_contact",
        "apollo_bulk_create_contacts",
        "apollo_match_contact",
        "apollo_bulk_match_contacts",
        "apollo_list_contact_stages",
        "apollo_create_account",
        "apollo_update_account",
        "apollo_bulk_create_accounts",
        "apollo_list_account_stages",
        "apollo_bulk_create_tasks",
        "apollo_create_phone_call",
        "apollo_update_phone_call",
        "apollo_remove_contacts_from_sequence",
        "apollo_approve_sequence",
        "apollo_abort_sequence",
        "apollo_archive_sequence",
        "apollo_search_emailer_messages",
        "apollo_get_emailer_message_activities",
    }


# ---- apollo_search_people --------------------------------------------------


async def test_search_people_simplifies_response(server_and_client):
    mcp, fake = server_and_client
    fake.search_people.return_value = fixtures.mock_person_search_response

    text = await _call(mcp, "apollo_search_people", {"query": "engineer"})
    payload = json.loads(text)

    assert payload["total_results"] == 145
    assert payload["page"] == 1
    assert payload["per_page"] == 10
    assert len(payload["people"]) == 1
    assert payload["people"][0]["company"] == "Example Corp"
    assert payload["people"][0]["name"] == "John Doe"


async def test_search_people_maps_filter_names(server_and_client):
    mcp, fake = server_and_client
    fake.search_people.return_value = {"people": [], "pagination": {}}

    await _call(
        mcp,
        "apollo_search_people",
        {
            "filters": {
                "titles": ["CEO"],
                "seniority": ["C-Level"],
                "locations": ["California"],
                "company_domains": ["example.com"],
                "industries": ["Software"],
                "technologies": ["python"],
            },
            "page": 2,
            "per_page": 10,
        },
    )

    body = fake.search_people.call_args.args[0]
    assert body["person_titles"] == ["CEO"]
    assert body["person_seniorities"] == ["C-Level"]
    assert body["person_locations"] == ["California"]
    assert body["q_organization_domains_list"] == ["example.com"]
    assert body["industries"] == ["Software"]
    assert body["currently_using_any_of_technology_uids"] == ["python"]
    assert body["page"] == 2
    assert body["per_page"] == 10


async def test_search_people_propagates_apollo_errors(server_and_client):
    from fastmcp.exceptions import ToolError

    mcp, fake = server_and_client
    fake.search_people.side_effect = ApolloError("boom", status=500)

    with pytest.raises(ToolError, match="Error searching people: boom"):
        await _call(mcp, "apollo_search_people", {})


# ---- apollo_search_companies -----------------------------------------------


async def test_search_companies_simplifies_response(server_and_client):
    mcp, fake = server_and_client
    fake.search_companies.return_value = fixtures.mock_company_search_response

    text = await _call(mcp, "apollo_search_companies", {"query": "tech"})
    payload = json.loads(text)

    assert payload["total_results"] == 52
    assert len(payload["companies"]) == 1
    company = payload["companies"][0]
    assert company["name"] == "Example Corp"
    assert company["website"] == "https://example.com"
    assert company["location"] == "123 Main St, San Francisco, CA 94105"


async def test_search_companies_employee_range_dash_to_comma(server_and_client):
    mcp, fake = server_and_client
    fake.search_companies.return_value = {"organizations": [], "pagination": {}}

    await _call(
        mcp,
        "apollo_search_companies",
        {
            "filters": {
                "organization_num_employees_ranges": ["11-20", "21,50"],
            }
        },
    )

    body = fake.search_companies.call_args.args[0]
    # "11-20" must be normalised to "11,20"; "21,50" stays as-is
    assert body["organization_num_employees_ranges"] == ["11,20", "21,50"]


async def test_search_companies_revenue_range_passes_through(server_and_client):
    mcp, fake = server_and_client
    fake.search_companies.return_value = {"organizations": [], "pagination": {}}

    await _call(
        mcp,
        "apollo_search_companies",
        {
            "filters": {
                "revenue_range": {"min": 1000000, "max": 5000000},
            }
        },
    )

    body = fake.search_companies.call_args.args[0]
    assert body["revenue_range"] == {"min": 1000000, "max": 5000000}


# ---- apollo_enrich_person --------------------------------------------------


async def test_enrich_person_passes_reveal_flags(server_and_client):
    mcp, fake = server_and_client
    fake.match_person.return_value = fixtures.mock_person_enrich_response

    await _call(
        mcp,
        "apollo_enrich_person",
        {
            "email": "tim@apollo.io",
            "reveal_personal_emails": True,
            "reveal_phone_number": True,
        },
    )

    args = fake.match_person.call_args
    assert args.args[0] == {"email": "tim@apollo.io"}
    assert args.args[1] is True  # reveal_personal_emails
    assert args.args[2] is True  # reveal_phone_number


async def test_enrich_person_maps_company_alias_to_organization_name(server_and_client):
    mcp, fake = server_and_client
    fake.match_person.return_value = fixtures.mock_person_enrich_response

    await _call(
        mcp,
        "apollo_enrich_person",
        {"name": "Jane Doe", "company": "Acme"},
    )

    args = fake.match_person.call_args
    assert args.args[0] == {"name": "Jane Doe", "organization_name": "Acme"}


async def test_enrich_person_accepts_domain_and_organization_name(server_and_client):
    mcp, fake = server_and_client
    fake.match_person.return_value = fixtures.mock_person_enrich_response

    await _call(
        mcp,
        "apollo_enrich_person",
        {
            "name": "Jane Doe",
            "domain": "acme.com",
            "organization_name": "Acme",
        },
    )

    args = fake.match_person.call_args
    assert args.args[0] == {
        "name": "Jane Doe",
        "domain": "acme.com",
        "organization_name": "Acme",
    }


async def test_enrich_person_default_reveal_flags(server_and_client):
    mcp, fake = server_and_client
    fake.match_person.return_value = fixtures.mock_person_enrich_response

    await _call(mcp, "apollo_enrich_person", {"email": "tim@apollo.io"})

    args = fake.match_person.call_args
    assert args.args[1] is False
    assert args.args[2] is False


async def test_enrich_person_default_returns_compact_summary(server_and_client):
    """Default mode returns ~25 fields, not the full Apollo payload."""
    mcp, fake = server_and_client
    fake.match_person.return_value = fixtures.mock_person_enrich_response

    text = await _call(
        mcp,
        "apollo_enrich_person",
        {"linkedin_url": "https://www.linkedin.com/in/johndoe"},
    )
    payload = json.loads(text)

    assert payload["id"] == "5f7b1234567890abcdef1234"
    assert payload["name"] == "John Doe"
    assert payload["email"] == "john.doe@example.com"
    assert payload["organization"]["name"] == "Example Corp"
    assert payload["employment_history"][0]["title"] == "Software Engineer"
    # Compact mode strips all the chunky nested arrays
    assert "current_technologies" not in payload
    assert "raw_address" not in payload


async def test_enrich_person_full_returns_raw_payload(server_and_client):
    """full=true returns Apollo's raw response."""
    mcp, fake = server_and_client
    fake.match_person.return_value = fixtures.mock_person_enrich_response

    text = await _call(
        mcp,
        "apollo_enrich_person",
        {"linkedin_url": "https://www.linkedin.com/in/johndoe", "full": True},
    )
    payload = json.loads(text)

    # Raw response wraps the person under .person
    assert payload["person"]["id"] == "5f7b1234567890abcdef1234"


# ---- apollo_reveal_person --------------------------------------------------


async def test_reveal_person_simplifies_payload(server_and_client):
    mcp, fake = server_and_client
    fake.match_person.return_value = {
        "person": {
            "id": "abc",
            "first_name": "Jane",
            "last_name": "Doe",
            "name": "Jane Doe",
            "title": "VP Eng",
            "email": "jane@example.com",
            "email_status": "verified",
            "linkedin_url": "https://linkedin.com/in/janedoe",
            "phone_numbers": [{"sanitized_number": "+15551234567"}],
            "organization": {"name": "Example Corp"},
            "city": "SF",
            "state": "CA",
            "country": "US",
            "departments": ["engineering"],
            "seniority": "vp",
        }
    }

    text = await _call(mcp, "apollo_reveal_person", {"id": "abc"})
    payload = json.loads(text)

    assert payload["id"] == "abc"
    assert payload["phone"] == "+15551234567"
    assert payload["company"] == "Example Corp"


# ---- apollo_enrich_company -------------------------------------------------


async def test_enrich_company_calls_match(server_and_client):
    mcp, fake = server_and_client
    fake.match_company.return_value = fixtures.mock_company_enrich_response

    await _call(mcp, "apollo_enrich_company", {"domain": "apollo.io"})
    fake.match_company.assert_called_once_with({"domain": "apollo.io"})


# ---- bulk enrichment -------------------------------------------------------


async def test_bulk_enrich_people_strips_none_fields(server_and_client):
    mcp, fake = server_and_client
    fake.bulk_enrich_people.return_value = fixtures.mock_bulk_enrich_people_response

    await _call(
        mcp,
        "apollo_bulk_enrich_people",
        {
            "people": [
                {"email": "a@example.com"},
                {"name": "Jane", "company": "Acme"},
                {"name": "Dana", "domain": "example.com"},
            ],
            "reveal_personal_emails": True,
        },
    )

    args = fake.bulk_enrich_people.call_args
    body = args.args[0]
    # Apollo expects body field name `details`, not `people`
    assert body == {
        "details": [
            {"email": "a@example.com"},
            {"name": "Jane", "organization_name": "Acme"},
            {"name": "Dana", "domain": "example.com"},
        ]
    }
    assert args.args[1] is True  # reveal_personal_emails
    assert args.args[2] is False  # reveal_phone_number default


async def test_bulk_enrich_organizations_sends_domains_list(server_and_client):
    mcp, fake = server_and_client
    fake.bulk_enrich_organizations.return_value = (
        fixtures.mock_bulk_enrich_organizations_response
    )

    await _call(
        mcp,
        "apollo_bulk_enrich_organizations",
        {"organizations": [{"domain": "apollo.io"}, {"domain": "google.com"}, {"name": "Acme"}]},
    )

    body = fake.bulk_enrich_organizations.call_args.args[0]
    # Apollo expects a flat list under `domains`. Items with no domain are
    # dropped so passing only `name` doesn't poison the body.
    assert body == {"domains": ["apollo.io", "google.com"]}


# ---- organization endpoints ------------------------------------------------


async def test_get_organization_job_postings(server_and_client):
    mcp, fake = server_and_client
    fake.get_organization_job_postings.return_value = fixtures.mock_job_postings_response

    text = await _call(mcp, "apollo_get_organization_job_postings", {"organization_id": "org1"})
    payload = json.loads(text)

    fake.get_organization_job_postings.assert_called_once_with("org1")
    assert payload["job_postings"][0]["title"] == "Senior Software Engineer"


async def test_get_complete_organization_info(server_and_client):
    mcp, fake = server_and_client
    fake.get_complete_organization_info.return_value = {"organization": {"id": "org1"}}

    text = await _call(mcp, "apollo_get_complete_organization_info", {"organization_id": "org1"})
    payload = json.loads(text)

    fake.get_complete_organization_info.assert_called_once_with("org1")
    assert payload["organization"]["id"] == "org1"


# ---- news ------------------------------------------------------------------


async def test_search_news_articles_requires_organization_ids(server_and_client):
    from fastmcp.exceptions import ToolError

    mcp, _ = server_and_client
    with pytest.raises(ToolError, match="organization_ids is required"):
        await _call(mcp, "apollo_search_news_articles", {"organization_ids": []})


async def test_search_news_articles_explicit_filters(server_and_client):
    mcp, fake = server_and_client
    fake.search_news_articles.return_value = fixtures.mock_news_articles_response

    await _call(
        mcp,
        "apollo_search_news_articles",
        {
            "organization_ids": ["5e66b6381e05b4008c8331b8"],
            "categories": ["hires", "investment"],
            "published_at_min": "2025-01-01",
            "published_at_max": "2025-05-15",
            "page": 2,
            "per_page": 10,
        },
    )

    body = fake.search_news_articles.call_args.args[0]
    assert body["organization_ids"] == ["5e66b6381e05b4008c8331b8"]
    assert body["categories"] == ["hires", "investment"]
    assert body["published_at[min]"] == "2025-01-01"
    assert body["published_at[max]"] == "2025-05-15"
    assert body["page"] == 2
    assert body["per_page"] == 10
    # No legacy 'q' or arbitrary 'filters' bag should leak through
    assert "q" not in body


async def test_search_news_articles_minimal(server_and_client):
    mcp, fake = server_and_client
    fake.search_news_articles.return_value = fixtures.mock_news_articles_response

    await _call(
        mcp,
        "apollo_search_news_articles",
        {"organization_ids": ["5e66b6381e05b4008c8331b8"]},
    )

    body = fake.search_news_articles.call_args.args[0]
    assert body == {"organization_ids": ["5e66b6381e05b4008c8331b8"]}


# ---- usage stats -----------------------------------------------------------


async def test_get_api_usage_stats(server_and_client):
    mcp, fake = server_and_client
    fake.get_api_usage_stats.return_value = {
        "rate_limits": {"per_minute": 60, "per_hour": 600, "per_day": 5000},
        "usage": {"per_minute": 3, "per_hour": 12, "per_day": 412},
    }

    text = await _call(mcp, "apollo_get_api_usage_stats", {})
    payload = json.loads(text)

    fake.get_api_usage_stats.assert_called_once_with()
    assert payload["rate_limits"]["per_minute"] == 60
    assert payload["usage"]["per_day"] == 412


async def test_get_api_usage_stats_403_surfaces_as_tool_error(server_and_client):
    from fastmcp.exceptions import ToolError

    mcp, fake = server_and_client
    fake.get_api_usage_stats.side_effect = ApolloError(
        "Apollo error 403: master key required", status=403
    )

    with pytest.raises(ToolError, match="Error getting API usage stats"):
        await _call(mcp, "apollo_get_api_usage_stats", {})


# ---- Tier 1 tool tests -----------------------------------------------------


async def test_get_person_tool(server_and_client):
    mcp, fake = server_and_client
    fake.get_person.return_value = {"person": {"id": "abc"}}
    await _call(mcp, "apollo_get_person", {"person_id": "abc"})
    fake.get_person.assert_called_once_with("abc")


async def test_create_deal_required_name(server_and_client):
    mcp, fake = server_and_client
    fake.create_deal.return_value = {"id": "d1"}
    await _call(
        mcp,
        "apollo_create_deal",
        {"name": "Q3 Big", "amount": "10000", "owner_id": "u1"},
    )
    body = fake.create_deal.call_args.args[0]
    assert body == {"name": "Q3 Big", "amount": "10000", "owner_id": "u1"}


async def test_update_deal_requires_at_least_one_field(server_and_client):
    from fastmcp.exceptions import ToolError

    mcp, _ = server_and_client
    with pytest.raises(ToolError, match="at least one field"):
        await _call(mcp, "apollo_update_deal", {"opportunity_id": "d1"})


async def test_update_deal_passes_fields(server_and_client):
    mcp, fake = server_and_client
    fake.update_deal.return_value = {"id": "d1"}
    await _call(
        mcp,
        "apollo_update_deal",
        {"opportunity_id": "d1", "amount": "20000"},
    )
    args = fake.update_deal.call_args.args
    assert args[0] == "d1"
    assert args[1] == {"amount": "20000"}


async def test_list_deal_stages_no_args(server_and_client):
    mcp, fake = server_and_client
    fake.list_deal_stages.return_value = {"opportunity_stages": []}
    await _call(mcp, "apollo_list_deal_stages", {})
    fake.list_deal_stages.assert_called_once_with()


async def test_list_users_pagination(server_and_client):
    mcp, fake = server_and_client
    fake.list_users.return_value = {"users": []}
    await _call(mcp, "apollo_list_users", {"page": 2, "per_page": 50})
    fake.list_users.assert_called_once_with(page=2, per_page=50)


async def test_create_task_passes_all_required(server_and_client):
    mcp, fake = server_and_client
    fake.create_task.return_value = {"success": True, "tasks": []}
    await _call(
        mcp,
        "apollo_create_task",
        {
            "user_id": "u1",
            "contact_ids": ["c1", "c2"],
            "type": "call",
            "priority": "high",
            "status": "scheduled",
            "due_at": "2025-02-15T10:00:00Z",
            "note": "Follow up",
        },
    )
    body = fake.create_task.call_args.args[0]
    assert body == {
        "user_id": "u1",
        "contact_ids": ["c1", "c2"],
        "type": "call",
        "priority": "high",
        "status": "scheduled",
        "due_at": "2025-02-15T10:00:00Z",
        "note": "Follow up",
    }


async def test_bulk_complete_tasks(server_and_client):
    mcp, fake = server_and_client
    fake.bulk_complete_tasks.return_value = {"success": True}
    await _call(mcp, "apollo_bulk_complete_tasks", {"task_ids": ["t1", "t2"]})
    fake.bulk_complete_tasks.assert_called_once_with({"task_ids": ["t1", "t2"]})


async def test_add_contacts_to_sequence(server_and_client):
    mcp, fake = server_and_client
    fake.add_contacts_to_sequence.return_value = {"contacts": []}
    await _call(
        mcp,
        "apollo_add_contacts_to_sequence",
        {
            "sequence_id": "seq_1",
            "contact_ids": ["c1"],
            "send_email_from_email_account_id": "ea_1",
        },
    )
    args = fake.add_contacts_to_sequence.call_args.args
    assert args[0] == "seq_1"
    assert args[1] == {
        "contact_ids": ["c1"],
        "send_email_from_email_account_id": "ea_1",
    }


# ---- Behavioral coverage for the 14 previously-untested tools --------------


async def test_search_contacts_passes_through(server_and_client):
    mcp, fake = server_and_client
    fake.search_contacts.return_value = {"contacts": []}
    await _call(
        mcp,
        "apollo_search_contacts",
        {"q_keywords": "VP Eng", "contact_stage_ids": ["stage1"], "page": 2, "per_page": 5},
    )
    body = fake.search_contacts.call_args.args[0]
    assert body == {
        "q_keywords": "VP Eng",
        "contact_stage_ids": ["stage1"],
        "page": 2,
        "per_page": 5,
    }


async def test_search_accounts_passes_through(server_and_client):
    mcp, fake = server_and_client
    fake.search_accounts.return_value = {"accounts": []}
    await _call(
        mcp,
        "apollo_search_accounts",
        {"q_organization_name": "Apollo", "per_page": 3},
    )
    body = fake.search_accounts.call_args.args[0]
    assert body == {"q_organization_name": "Apollo", "per_page": 3}


async def test_search_deals_passes_through(server_and_client):
    mcp, fake = server_and_client
    fake.search_deals.return_value = {"opportunities": []}
    await _call(mcp, "apollo_search_deals", {"q_name": "Q3", "owner_ids": ["u1"]})
    body = fake.search_deals.call_args.args[0]
    assert body == {"q_name": "Q3", "owner_ids": ["u1"]}


async def test_search_tasks_passes_through(server_and_client):
    mcp, fake = server_and_client
    fake.search_tasks.return_value = {"tasks": []}
    await _call(mcp, "apollo_search_tasks", {"owner_ids": ["u1"], "per_page": 5})
    body = fake.search_tasks.call_args.args[0]
    assert body == {"owner_ids": ["u1"], "per_page": 5}


async def test_search_sequences_passes_through(server_and_client):
    mcp, fake = server_and_client
    fake.search_sequences.return_value = {"emailer_campaigns": []}
    await _call(mcp, "apollo_search_sequences", {"q_name": "outbound", "per_page": 3})
    body = fake.search_sequences.call_args.args[0]
    assert body == {"q_name": "outbound", "per_page": 3}


async def test_match_contact_filters_none(server_and_client):
    mcp, fake = server_and_client
    fake.match_contact.return_value = {"contact": {}}
    await _call(mcp, "apollo_match_contact", {"email": "tim@apollo.io"})
    body = fake.match_contact.call_args.args[0]
    assert body == {"email": "tim@apollo.io"}


async def test_bulk_match_contacts_wraps_details(server_and_client):
    mcp, fake = server_and_client
    fake.bulk_match_contacts.return_value = {"contacts": []}
    await _call(
        mcp,
        "apollo_bulk_match_contacts",
        {"details": [{"email": "a@x.com"}, {"linkedin_url": "https://example.com/in/jane"}]},
    )
    body = fake.bulk_match_contacts.call_args.args[0]
    assert body == {
        "details": [
            {"email": "a@x.com"},
            {"linkedin_url": "https://example.com/in/jane"},
        ]
    }


async def test_list_contact_stages_no_args(server_and_client):
    mcp, fake = server_and_client
    fake.list_contact_stages.return_value = {"contact_stages": []}
    await _call(mcp, "apollo_list_contact_stages", {})
    fake.list_contact_stages.assert_called_once_with()


async def test_list_account_stages_no_args(server_and_client):
    mcp, fake = server_and_client
    fake.list_account_stages.return_value = {"account_stages": []}
    await _call(mcp, "apollo_list_account_stages", {})
    fake.list_account_stages.assert_called_once_with()


async def test_bulk_create_accounts_wraps_with_extras(server_and_client):
    mcp, fake = server_and_client
    fake.bulk_create_accounts.return_value = {"created_accounts": []}
    await _call(
        mcp,
        "apollo_bulk_create_accounts",
        {
            "accounts": [{"name": "Acme", "domain": "acme.com"}],
            "run_dedupe": True,
            "append_label_names": ["Hot"],
        },
    )
    body = fake.bulk_create_accounts.call_args.args[0]
    assert body == {
        "accounts": [{"name": "Acme", "domain": "acme.com"}],
        "run_dedupe": True,
        "append_label_names": ["Hot"],
    }


async def test_update_phone_call_passes_id_and_fields(server_and_client):
    mcp, fake = server_and_client
    fake.update_phone_call.return_value = {"phone_call": {"id": "pc1"}}
    await _call(
        mcp,
        "apollo_update_phone_call",
        {"phone_call_id": "pc1", "status": "completed", "note": "ok"},
    )
    args = fake.update_phone_call.call_args.args
    assert args[0] == "pc1"
    assert args[1] == {"status": "completed", "note": "ok"}


async def test_update_phone_call_requires_field(server_and_client):
    from fastmcp.exceptions import ToolError

    mcp, _ = server_and_client
    with pytest.raises(ToolError, match="at least one field"):
        await _call(mcp, "apollo_update_phone_call", {"phone_call_id": "pc1"})


async def test_abort_sequence_calls_correct_id(server_and_client):
    mcp, fake = server_and_client
    fake.abort_sequence.return_value = {}
    await _call(mcp, "apollo_abort_sequence", {"sequence_id": "seq_1"})
    fake.abort_sequence.assert_called_once_with("seq_1")


async def test_archive_sequence_calls_correct_id(server_and_client):
    mcp, fake = server_and_client
    fake.archive_sequence.return_value = {}
    await _call(mcp, "apollo_archive_sequence", {"sequence_id": "seq_1"})
    fake.archive_sequence.assert_called_once_with("seq_1")


async def test_get_emailer_message_activities_calls_correct_id(server_and_client):
    mcp, fake = server_and_client
    fake.get_emailer_message_activities.return_value = {"emailer_message_activities": []}
    await _call(
        mcp,
        "apollo_get_emailer_message_activities",
        {"message_id": "m1"},
    )
    fake.get_emailer_message_activities.assert_called_once_with("m1")


async def test_get_organization_top_people(server_and_client):
    mcp, fake = server_and_client
    fake.get_organization_top_people.return_value = {"people": []}
    await _call(
        mcp,
        "apollo_get_organization_top_people",
        {"organization_id": "org_1"},
    )
    fake.get_organization_top_people.assert_called_once_with("org_1")


async def test_add_to_my_prospects(server_and_client):
    mcp, fake = server_and_client
    fake.add_to_my_prospects.return_value = {"contacts": []}
    await _call(
        mcp,
        "apollo_add_to_my_prospects",
        {"entity_ids": ["p1", "p2"], "analytics_context": "Searcher: People"},
    )
    body = fake.add_to_my_prospects.call_args.args[0]
    assert body == {
        "entity_ids": ["p1", "p2"],
        "analytics_context": "Searcher: People",
    }


# ---- Tier 2 tool tests -----------------------------------------------------


async def test_create_contact_filters_none(server_and_client):
    mcp, fake = server_and_client
    fake.create_contact.return_value = {"contact": {"id": "c1"}}
    await _call(
        mcp,
        "apollo_create_contact",
        {"first_name": "Jane", "email": "jane@example.com", "extra": {"phone": "555"}},
    )
    body = fake.create_contact.call_args.args[0]
    assert body == {
        "first_name": "Jane",
        "email": "jane@example.com",
        "phone": "555",
    }


async def test_update_contact_requires_a_field(server_and_client):
    from fastmcp.exceptions import ToolError

    mcp, _ = server_and_client
    with pytest.raises(ToolError, match="at least one field"):
        await _call(mcp, "apollo_update_contact", {"contact_id": "c1"})


async def test_update_contact_passes_id_and_fields(server_and_client):
    mcp, fake = server_and_client
    fake.update_contact.return_value = {"contact": {"id": "c1"}}
    await _call(
        mcp,
        "apollo_update_contact",
        {"contact_id": "c1", "title": "VP Eng", "label_names": ["Hot"]},
    )
    args = fake.update_contact.call_args.args
    assert args[0] == "c1"
    assert args[1] == {"title": "VP Eng", "label_names": ["Hot"]}


async def test_bulk_create_contacts_passes_through(server_and_client):
    mcp, fake = server_and_client
    fake.bulk_create_contacts.return_value = {"created_contacts": []}
    await _call(
        mcp,
        "apollo_bulk_create_contacts",
        {
            "contacts": [{"first_name": "A", "email": "a@x.com"}],
            "run_dedupe": True,
            "append_label_names": ["Hot"],
        },
    )
    body = fake.bulk_create_contacts.call_args.args[0]
    assert body == {
        "contacts": [{"first_name": "A", "email": "a@x.com"}],
        "run_dedupe": True,
        "append_label_names": ["Hot"],
    }


async def test_create_account_only_provided_fields(server_and_client):
    mcp, fake = server_and_client
    fake.create_account.return_value = {"account": {"id": "a1"}}
    await _call(
        mcp,
        "apollo_create_account",
        {"name": "Acme", "domain": "acme.com"},
    )
    body = fake.create_account.call_args.args[0]
    assert body == {"name": "Acme", "domain": "acme.com"}


async def test_update_account_requires_field(server_and_client):
    from fastmcp.exceptions import ToolError

    mcp, _ = server_and_client
    with pytest.raises(ToolError, match="at least one field"):
        await _call(mcp, "apollo_update_account", {"account_id": "a1"})


async def test_remove_contacts_from_sequence_wraps_body(server_and_client):
    mcp, fake = server_and_client
    fake.remove_contacts_from_sequence.return_value = {"entity_progress_job": {}}
    await _call(
        mcp,
        "apollo_remove_contacts_from_sequence",
        {"contact_ids": ["c1", "c2"], "sequence_ids": ["seq_1"]},
    )
    body = fake.remove_contacts_from_sequence.call_args.args[0]
    assert body == {
        "entity_ids": ["c1", "c2"],
        "params": {"sequence_ids": ["seq_1"], "mode": "mark_as_finished"},
    }


async def test_approve_sequence_calls_correct_id(server_and_client):
    mcp, fake = server_and_client
    fake.approve_sequence.return_value = {}
    await _call(mcp, "apollo_approve_sequence", {"sequence_id": "seq_1"})
    fake.approve_sequence.assert_called_once_with("seq_1")


async def test_search_emailer_messages_maps_sequence_ids(server_and_client):
    mcp, fake = server_and_client
    fake.search_emailer_messages.return_value = {"emailer_messages": []}
    await _call(
        mcp,
        "apollo_search_emailer_messages",
        {"sequence_ids": ["seq_1"], "per_page": 10},
    )
    body = fake.search_emailer_messages.call_args.args[0]
    assert body == {"emailer_campaign_ids": ["seq_1"], "per_page": 10}


async def test_bulk_create_tasks(server_and_client):
    mcp, fake = server_and_client
    fake.bulk_create_tasks.return_value = {"success": True, "tasks": []}
    await _call(
        mcp,
        "apollo_bulk_create_tasks",
        {
            "user_id": "u1",
            "contact_ids": ["c1", "c2"],
            "type": "call",
            "status": "open",
            "due_at": "2025-01-01",
            "priority": "high",
        },
    )
    body = fake.bulk_create_tasks.call_args.args[0]
    assert body["user_id"] == "u1"
    assert body["contact_ids"] == ["c1", "c2"]
    assert body["priority"] == "high"


async def test_create_phone_call_with_extra(server_and_client):
    mcp, fake = server_and_client
    fake.create_phone_call.return_value = {"phone_call": {"id": "pc1"}}
    await _call(
        mcp,
        "apollo_create_phone_call",
        {
            "contact_id": "c1",
            "duration": 90,
            "status": "completed",
            "extra": {"recording_url": "https://example.com/rec.mp3"},
        },
    )
    body = fake.create_phone_call.call_args.args[0]
    assert body == {
        "contact_id": "c1",
        "duration": 90,
        "status": "completed",
        "recording_url": "https://example.com/rec.mp3",
    }


@pytest.mark.parametrize(
    ("tool_name", "client_method", "arguments"),
    [
        ("apollo_search_companies", "search_companies", {}),
        ("apollo_enrich_person", "match_person", {"email": "tim@apollo.io"}),
        ("apollo_reveal_person", "match_person", {"id": "p1"}),
        ("apollo_enrich_company", "match_company", {"domain": "apollo.io"}),
        (
            "apollo_bulk_enrich_people",
            "bulk_enrich_people",
            {"people": [{"email": "tim@apollo.io"}]},
        ),
        (
            "apollo_bulk_enrich_organizations",
            "bulk_enrich_organizations",
            {"organizations": [{"domain": "apollo.io"}]},
        ),
        (
            "apollo_get_organization_job_postings",
            "get_organization_job_postings",
            {"organization_id": "org1"},
        ),
        (
            "apollo_get_complete_organization_info",
            "get_complete_organization_info",
            {"organization_id": "org1"},
        ),
        (
            "apollo_search_news_articles",
            "search_news_articles",
            {"organization_ids": ["org1"]},
        ),
        ("apollo_get_person", "get_person", {"person_id": "p1"}),
        ("apollo_search_contacts", "search_contacts", {}),
        ("apollo_search_accounts", "search_accounts", {}),
        ("apollo_search_deals", "search_deals", {}),
        ("apollo_create_deal", "create_deal", {"name": "Deal"}),
        ("apollo_update_deal", "update_deal", {"opportunity_id": "d1", "amount": "10"}),
        ("apollo_list_deal_stages", "list_deal_stages", {}),
        ("apollo_list_users", "list_users", {}),
        (
            "apollo_create_task",
            "create_task",
            {
                "user_id": "u1",
                "contact_ids": ["c1"],
                "type": "call",
                "priority": "high",
                "status": "scheduled",
                "due_at": "2025-01-01T00:00:00Z",
            },
        ),
        ("apollo_search_tasks", "search_tasks", {}),
        ("apollo_bulk_complete_tasks", "bulk_complete_tasks", {"task_ids": ["t1"]}),
        ("apollo_search_sequences", "search_sequences", {}),
        (
            "apollo_add_contacts_to_sequence",
            "add_contacts_to_sequence",
            {"sequence_id": "seq1", "contact_ids": ["c1"]},
        ),
        (
            "apollo_get_organization_top_people",
            "get_organization_top_people",
            {"organization_id": "org1"},
        ),
        ("apollo_add_to_my_prospects", "add_to_my_prospects", {"entity_ids": ["p1"]}),
        ("apollo_create_contact", "create_contact", {"email": "a@example.com"}),
        ("apollo_update_contact", "update_contact", {"contact_id": "c1", "title": "VP"}),
        (
            "apollo_bulk_create_contacts",
            "bulk_create_contacts",
            {"contacts": [{"email": "a@example.com"}]},
        ),
        ("apollo_match_contact", "match_contact", {"email": "a@example.com"}),
        (
            "apollo_bulk_match_contacts",
            "bulk_match_contacts",
            {"details": [{"email": "a@example.com"}]},
        ),
        ("apollo_list_contact_stages", "list_contact_stages", {}),
        ("apollo_create_account", "create_account", {"domain": "apollo.io"}),
        ("apollo_update_account", "update_account", {"account_id": "a1", "name": "Acme"}),
        (
            "apollo_bulk_create_accounts",
            "bulk_create_accounts",
            {"accounts": [{"domain": "apollo.io"}]},
        ),
        ("apollo_list_account_stages", "list_account_stages", {}),
        (
            "apollo_bulk_create_tasks",
            "bulk_create_tasks",
            {
                "user_id": "u1",
                "contact_ids": ["c1"],
                "type": "call",
                "status": "open",
                "due_at": "2025-01-01",
            },
        ),
        ("apollo_create_phone_call", "create_phone_call", {"contact_id": "c1"}),
        (
            "apollo_update_phone_call",
            "update_phone_call",
            {"phone_call_id": "pc1", "status": "completed"},
        ),
        (
            "apollo_remove_contacts_from_sequence",
            "remove_contacts_from_sequence",
            {"contact_ids": ["c1"], "sequence_ids": ["seq1"]},
        ),
        ("apollo_approve_sequence", "approve_sequence", {"sequence_id": "seq1"}),
        ("apollo_abort_sequence", "abort_sequence", {"sequence_id": "seq1"}),
        ("apollo_archive_sequence", "archive_sequence", {"sequence_id": "seq1"}),
        ("apollo_search_emailer_messages", "search_emailer_messages", {}),
        (
            "apollo_get_emailer_message_activities",
            "get_emailer_message_activities",
            {"message_id": "m1"},
        ),
    ],
)
async def test_tools_wrap_apollo_errors(tool_name, client_method, arguments, server_and_client):
    from fastmcp.exceptions import ToolError

    mcp, fake = server_and_client
    getattr(fake, client_method).side_effect = ApolloError("apollo down", status=500)

    with pytest.raises(ToolError, match="apollo down"):
        await _call(mcp, tool_name, arguments)


# ---- Regression: company location formatting --------------------------------


async def test_company_location_omits_blank_components(server_and_client):
    """A blank middle field must not leave a dangling separator."""
    mcp, fake = server_and_client
    fake.search_companies.return_value = {
        "organizations": [
            {"id": "1", "name": "A", "city": "San Francisco", "state": "", "country": "USA"},
            {"id": "2", "name": "B", "city": "", "state": "CA", "country": ""},
            {"id": "3", "name": "C", "city": "Berlin", "state": None, "country": "Germany"},
        ],
        "pagination": {},
    }
    payload = json.loads(await _call(mcp, "apollo_search_companies", {}))
    locations = [c["location"] for c in payload["companies"]]
    assert locations == ["San Francisco, USA", "CA", "Berlin, Germany"]


async def test_company_location_prefers_raw_address(server_and_client):
    mcp, fake = server_and_client
    fake.search_companies.return_value = {
        "organizations": [{"id": "1", "name": "A", "raw_address": "1 Main St", "city": "SF"}],
        "pagination": {},
    }
    payload = json.loads(await _call(mcp, "apollo_search_companies", {}))
    assert payload["companies"][0]["location"] == "1 Main St"


# ---- Regression: bulk enrich organizations with no domains -------------------


async def test_bulk_enrich_organizations_rejects_name_only_input(server_and_client):
    """Name-only input can't be matched by Apollo; fail loudly instead of
    posting an empty domain list and returning a misleading empty result."""
    from fastmcp.exceptions import ToolError

    mcp, fake = server_and_client
    with pytest.raises(ToolError, match="requires a `domain`"):
        await _call(
            mcp,
            "apollo_bulk_enrich_organizations",
            {"organizations": [{"name": "Apollo"}, {"name": "Google"}]},
        )
    fake.bulk_enrich_organizations.assert_not_called()


async def test_bulk_enrich_organizations_allows_partial_domains(server_and_client):
    """As long as one domain is present the call goes through."""
    mcp, fake = server_and_client
    fake.bulk_enrich_organizations.return_value = {"organizations": []}
    await _call(
        mcp,
        "apollo_bulk_enrich_organizations",
        {"organizations": [{"name": "Apollo"}, {"domain": "google.com"}]},
    )
    assert fake.bulk_enrich_organizations.call_args.args[0] == {"domains": ["google.com"]}


async def test_bulk_enrich_organizations_empty_list_is_not_an_error(server_and_client):
    mcp, fake = server_and_client
    fake.bulk_enrich_organizations.return_value = {"organizations": []}
    await _call(mcp, "apollo_bulk_enrich_organizations", {"organizations": []})
    assert fake.bulk_enrich_organizations.call_args.args[0] == {"domains": []}


# ---- Regression: per_page bound matches Apollo's real 100 cap ----------------


async def test_per_page_above_apollo_cap_is_rejected_by_schema(server_and_client):
    """Apollo answers 422 for per_page > 100 ("Per page not supported" /
    "Per Page Limit Crossed."), so the schema must not advertise 200."""
    from pydantic import ValidationError

    mcp, fake = server_and_client
    for tool in ("apollo_search_people", "apollo_search_companies"):
        with pytest.raises(ValidationError, match="less than or equal to 100"):
            await _call(mcp, tool, {"per_page": 150})
    fake.search_people.assert_not_called()
    fake.search_companies.assert_not_called()


async def test_per_page_at_apollo_cap_is_accepted(server_and_client):
    mcp, fake = server_and_client
    fake.search_people.return_value = {"people": [], "pagination": {}}
    await _call(mcp, "apollo_search_people", {"per_page": 100})
    assert fake.search_people.call_args.args[0]["per_page"] == 100
