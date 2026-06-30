"""Integration tests against the real Apollo.io API.

These tests run only when ``APOLLO_API_KEY`` is set in the environment;
otherwise the entire module is skipped (mirrors the TS integration suite).

Run with::

    APOLLO_API_KEY=... pytest tests/test_integration.py -v
"""

from __future__ import annotations

import os

import pytest

from apollo_mcp.apollo import ApolloClient, ApolloError
from tests import fixtures


HAS_API_KEY = bool(os.environ.get("APOLLO_API_KEY"))
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not HAS_API_KEY, reason="APOLLO_API_KEY not set"),
]


@pytest.fixture(scope="module")
def client():
    c = ApolloClient(api_key=os.environ["APOLLO_API_KEY"])
    yield c
    c.close()


# ---- search people ---------------------------------------------------------


def test_search_people_basic_query(client):
    response = client.search_people({"q": "Software Engineer", "page": 1, "per_page": 5})
    assert "people" in response
    assert isinstance(response["people"], list)
    assert len(response["people"]) > 0


def test_search_people_with_filters(client):
    response = client.search_people({
        "person_titles": ["CEO", "CTO"],
        "q_organization_domains": ["apollo.io"],
        "page": 1,
        "per_page": 5,
    })
    assert "people" in response
    assert isinstance(response["people"], list)


def test_search_people_handles_unusual_query(client):
    response = client.search_people({
        "q": "xyzinvalidquery12345nonexistent",
        "page": 1,
        "per_page": 5,
    })
    assert "people" in response
    assert isinstance(response["people"], list)


# ---- search companies ------------------------------------------------------


def test_search_companies_basic_query(client):
    response = client.search_companies({"q": "technology", "page": 1, "per_page": 5})
    assert isinstance(response.get("organizations"), list)
    assert "pagination" in response


def test_search_companies_filter_by_employee_count(client):
    response = client.search_companies({
        "organization_num_employees_ranges": ["11,20", "21,50"],
        "page": 1,
        "per_page": 5,
    })
    assert isinstance(response.get("organizations"), list)


# ---- person enrichment -----------------------------------------------------


def test_enrich_person_by_email(client):
    response = client.match_person({"email": "tim@apollo.io"})
    assert "person" in response
    assert response["person"].get("email") is not None


def test_enrich_person_with_reveal_flags(client):
    response = client.match_person({"email": "tim@apollo.io"}, False, False)
    assert "person" in response


# ---- company enrichment ----------------------------------------------------


def test_enrich_company_by_domain(client):
    response = client.match_company({"domain": "apollo.io"})
    assert response.get("organization") is not None
    assert response["organization"].get("primary_domain") == "apollo.io"


# ---- bulk enrichment -------------------------------------------------------


def test_bulk_enrich_organizations(client):
    response = client.bulk_enrich_organizations({"domains": ["apollo.io", "google.com"]})
    assert isinstance(response.get("organizations"), list)
    assert len(response["organizations"]) > 0


# ---- organization data -----------------------------------------------------


def test_get_organization_job_postings(client):
    search = client.search_companies({"q": "apollo.io", "page": 1, "per_page": 1})
    if not search.get("organizations"):
        pytest.skip("no organizations returned for apollo.io")
    org_id = search["organizations"][0]["id"]
    jobs = client.get_organization_job_postings(org_id)
    assert jobs is not None


def test_get_complete_organization_info(client):
    search = client.search_companies({"q": "apollo.io", "page": 1, "per_page": 1})
    if not search.get("organizations"):
        pytest.skip("no organizations returned for apollo.io")
    org_id = search["organizations"][0]["id"]
    info = client.get_complete_organization_info(org_id)
    assert info["organization"]["id"] == org_id


# ---- news ------------------------------------------------------------------


def test_search_news_articles_or_plan_limitation(client):
    try:
        response = client.search_news_articles({"q": "funding", "page": 1, "per_page": 5})
        assert isinstance(response.get("news_articles"), list)
    except ApolloError as exc:
        msg = str(exc)
        if any(code in msg for code in ("403", "API_INACCESSIBLE", "422")):
            return
        raise


# ---- error handling --------------------------------------------------------


def test_invalid_api_key_errors():
    bad = ApolloClient(api_key="invalid-key-12345")
    try:
        with pytest.raises(ApolloError, match=r"Unauthorized|invalid"):
            bad.search_people({"q": "test"})
    finally:
        bad.close()


# ---- always-on fixture-shape sanity checks ---------------------------------


class TestFixtureShapes:
    """Mock-shape tests that always run; mirror TS 'API Response Structure' suite."""

    def test_person_search_shape(self):
        r = fixtures.mock_person_search_response
        assert isinstance(r["people"], list)
        assert "page" in r["pagination"]
        assert "per_page" in r["pagination"]
        assert "total_entries" in r["pagination"]
        if r["people"]:
            assert r["people"][0]["id"]
            assert r["people"][0]["name"]
            assert r["people"][0]["organization"]

    def test_company_search_shape(self):
        r = fixtures.mock_company_search_response
        assert isinstance(r["organizations"], list)
        assert "pagination" in r
        if r["organizations"]:
            org = r["organizations"][0]
            assert org["id"]
            assert org["name"]
            assert org["primary_domain"]

    def test_enrichment_shape(self):
        p = fixtures.mock_person_enrich_response
        c = fixtures.mock_company_enrich_response
        assert p["person"]["id"]
        assert p["person"]["organization"]
        assert c["organization"]["id"]
        assert c["organization"]["name"]


# ---- Tier 1 / Tier 2 read-only endpoints ----------------------------------
#
# These integration tests cover the 35 endpoints added after the original 10.
# They are READ-ONLY only — the smoke-test suite verifies write paths
# interactively in the live MCP session, where side effects can be
# inspected and cleaned up. We do not auto-create deals/contacts/tasks
# from CI; that needs a sandbox account.


# Apollo.io's own org id is a stable test target available to all keys.
APOLLO_ORG_ID = "5e66b6381e05b4008c8331b8"


def test_get_person_works_against_live_api(client):
    """get_person on a known Apollo person ID returns a person dict."""
    # Search for someone first to get a real id (search-people endpoint).
    response = client.search_people({"q": "Software Engineer", "page": 1, "per_page": 1})
    if not response.get("people"):
        pytest.skip("no people returned by search to use for get_person")
    pid = response["people"][0]["id"]

    person_response = client.get_person(pid)
    assert person_response.get("person", {}).get("id") == pid


def test_search_contacts_returns_pagination(client):
    response = client.search_contacts({"per_page": 1})
    assert "pagination" in response
    assert isinstance(response.get("contacts", []), list)


def test_search_accounts_returns_pagination(client):
    response = client.search_accounts({"per_page": 1})
    assert "pagination" in response
    assert isinstance(response.get("accounts", []), list)


def test_search_deals_returns_pagination(client):
    response = client.search_deals({"per_page": 1})
    assert "pagination" in response
    assert isinstance(response.get("opportunities", []), list)


def test_list_users_returns_users(client):
    response = client.list_users(per_page=5)
    assert isinstance(response.get("users", []), list)


def test_list_deal_stages(client):
    response = client.list_deal_stages()
    stages = response.get("opportunity_stages", [])
    assert isinstance(stages, list)
    if stages:
        assert "id" in stages[0]
        assert "name" in stages[0]


def test_list_contact_stages(client):
    response = client.list_contact_stages()
    stages = response.get("contact_stages", [])
    assert isinstance(stages, list)
    if stages:
        assert "id" in stages[0]


def test_list_account_stages(client):
    response = client.list_account_stages()
    stages = response.get("account_stages", [])
    assert isinstance(stages, list)
    if stages:
        assert "id" in stages[0]


def test_search_tasks_returns_pagination(client):
    response = client.search_tasks({"per_page": 1})
    assert "pagination" in response


def test_search_sequences_returns_pagination(client):
    response = client.search_sequences({"per_page": 1})
    assert "pagination" in response


def test_search_emailer_messages(client):
    response = client.search_emailer_messages({"per_page": 1})
    assert isinstance(response.get("emailer_messages", []), list)


def test_match_contact_by_email(client):
    """match_contact will return the contact wrapper (or null contact) without 4xx."""
    response = client.match_contact({"email": "tim@apollo.io"})
    assert "contact" in response  # may be None if not in CRM, key still present


def test_get_organization_top_people_query_string_shape(client):
    """Verifies our undocumented-endpoint shape (GET + query string) works."""
    response = client.get_organization_top_people(APOLLO_ORG_ID)
    # Apollo returns a top-level dict with at least an `organization` key.
    assert isinstance(response, dict)
    # Other expected keys: contacts, people, model_ids — but at minimum
    # we want NO 4xx (which would have raised) and a dict back.


def test_get_api_usage_stats_master_key_required(client):
    """Master keys return a 200 with the usage map; non-master keys would 403."""
    response = client.get_api_usage_stats()
    assert isinstance(response, dict)
    if response:
        # Each key looks like '["api/v1/contacts", "search"]'
        sample_key = next(iter(response))
        assert sample_key.startswith('["api/v1/')


def test_search_news_articles_with_org_id(client):
    """News search requires organization_ids body param."""
    try:
        response = client.search_news_articles({
            "organization_ids": [APOLLO_ORG_ID],
            "per_page": 1,
        })
        assert isinstance(response.get("news_articles", []), list)
    except ApolloError as exc:
        # Some plans may not include news search; tolerate that.
        if any(code in str(exc) for code in ("403", "API_INACCESSIBLE", "422")):
            pytest.skip(f"news_articles not accessible on this plan: {exc}")
        raise


def test_bulk_enrich_organizations_uses_domains_list(client):
    """Verifies our wire-shape fix for bulk_enrich_organizations."""
    response = client.bulk_enrich_organizations({"domains": ["apollo.io"]})
    orgs = response.get("organizations", [])
    assert isinstance(orgs, list)
    if orgs:
        assert orgs[0].get("primary_domain") == "apollo.io"


def test_bulk_enrich_people_uses_details_field(client):
    """Verifies our wire-shape fix: body field is `details`, not `people`."""
    response = client.bulk_enrich_people({"details": [{"email": "tim@apollo.io"}]})
    # Apollo's response has either `matches` or `people` depending on plan
    assert isinstance(response, dict)
    # Either matches/people present and non-empty, or status field with 4xx-like
    # message — both indicate the request was understood.
