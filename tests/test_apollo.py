"""Unit tests for ApolloClient (port of src/apollo.test.ts).

Uses respx to mock httpx requests and assert on URL/method/body.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from apollo_mcp.apollo import ApolloClient, ApolloError


BASE = "https://api.apollo.io/api/v1"


@pytest.fixture
def client():
    c = ApolloClient(api_key="test-api-key")
    yield c
    c.close()


# ---- constructor -----------------------------------------------------------


def test_default_base_url():
    c = ApolloClient(api_key="test-key")
    assert c.base_url == BASE


def test_custom_base_url():
    c = ApolloClient(api_key="test-key", base_url="https://custom.api.com/api/v1")
    assert c.base_url == "https://custom.api.com/api/v1"


# ---- search_people ---------------------------------------------------------


@respx.mock
def test_search_people_basic_post(client):
    route = respx.post(f"{BASE}/mixed_people/api_search").mock(
        return_value=httpx.Response(200, json={"people": []})
    )
    client.search_people({"page": 1, "per_page": 5})

    assert route.called
    req = route.calls.last.request
    assert req.method == "POST"
    assert req.headers["x-api-key"] == "test-api-key"
    assert req.headers["content-type"] == "application/json"
    # only pagination is in the body; no filters here
    import json as _json

    assert _json.loads(req.content) == {"page": 1, "per_page": 5}


@respx.mock
def test_search_people_filters_go_to_query_string(client):
    """Apollo requires filter params in the URL query string with bracket notation."""
    route = respx.post(url__regex=rf"{BASE}/mixed_people/api_search\?.*").mock(
        return_value=httpx.Response(200, json={"people": []})
    )
    client.search_people({
        "person_titles": ["VP", "CTO"],
        "q_organization_domains": ["apollo.io"],
        "page": 1,
    })

    assert route.called
    url = str(route.calls.last.request.url)
    # Bracketed array notation for person_titles. httpx may display the
    # literal brackets even though the wire bytes are encoded — accept either.
    assert ("person_titles[]=VP" in url) or ("person_titles%5B%5D=VP" in url)
    assert ("person_titles[]=CTO" in url) or ("person_titles%5B%5D=CTO" in url)
    assert (
        "q_organization_domains_list[]=apollo.io" in url
        or "q_organization_domains_list%5B%5D=apollo.io" in url
    )


@respx.mock
def test_search_people_normalizes_legacy_aliases(client):
    route = respx.post(url__regex=rf"{BASE}/mixed_people/api_search\?.*").mock(
        return_value=httpx.Response(200, json={"people": []})
    )
    client.search_people({
        "q": "revenue leader",
        "company_domains": "apollo.io",
        "seniority": "VP",
        "technologies": ["salesforce"],
    })

    assert route.called
    url = str(route.calls.last.request.url)
    assert "q_keywords=revenue%20leader" in url
    assert (
        "q_organization_domains_list[]=apollo.io" in url
        or "q_organization_domains_list%5B%5D=apollo.io" in url
    )
    assert (
        "person_seniorities[]=VP" in url
        or "person_seniorities%5B%5D=VP" in url
    )
    assert (
        "currently_using_any_of_technology_uids[]=salesforce" in url
        or "currently_using_any_of_technology_uids%5B%5D=salesforce" in url
    )


@respx.mock
def test_search_people_canonical_query_wins_over_legacy_alias(client):
    route = respx.post(url__regex=rf"{BASE}/mixed_people/api_search\?.*").mock(
        return_value=httpx.Response(200, json={"people": []})
    )
    client.search_people({"q_keywords": "canonical", "q": "legacy"})

    assert route.called
    url = str(route.calls.last.request.url)
    assert "q_keywords=canonical" in url
    assert "legacy" not in url


@respx.mock
def test_search_people_unauthorized_raises(client):
    respx.post(f"{BASE}/mixed_people/api_search").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(ApolloError, match="Unauthorized: invalid or missing APOLLO_API_KEY"):
        client.search_people({})


@respx.mock
def test_search_people_rate_limited_with_retry_after(client):
    respx.post(f"{BASE}/mixed_people/api_search").mock(
        return_value=httpx.Response(429, headers={"retry-after": "60"}, text="Rate limited")
    )
    with pytest.raises(ApolloError, match=r"Rate limited by Apollo \(429\)\. Retry after 60s\."):
        client.search_people({})


@respx.mock
def test_search_people_rate_limited_without_retry_after(client):
    respx.post(f"{BASE}/mixed_people/api_search").mock(
        return_value=httpx.Response(429, text="Rate limited")
    )
    with pytest.raises(ApolloError, match=r"Rate limited by Apollo \(429\)\."):
        client.search_people({})


@respx.mock
def test_search_people_500_error(client):
    respx.post(f"{BASE}/mixed_people/api_search").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(ApolloError, match="Apollo error 500: Internal Server Error"):
        client.search_people({})


@respx.mock
def test_3xx_redirect_raises_apollo_error(client):
    """An unexpected redirect must surface as ApolloError, not a JSON parse error."""
    respx.post(f"{BASE}/mixed_people/api_search").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://attacker.example/foo"}
        )
    )
    with pytest.raises(ApolloError, match="unexpected redirect"):
        client.search_people({})


# ---- search_companies ------------------------------------------------------


@respx.mock
def test_search_companies_post(client):
    route = respx.post(f"{BASE}/mixed_companies/search").mock(
        return_value=httpx.Response(200, json={"organizations": []})
    )
    client.search_companies({"q": "test", "page": 1})

    assert route.called
    import json as _json

    assert _json.loads(route.calls.last.request.content) == {"q": "test", "page": 1}


# ---- match_person ----------------------------------------------------------


@respx.mock
def test_match_person_default_query_params(client):
    route = respx.post(
        f"{BASE}/people/match?reveal_personal_emails=false&reveal_phone_number=false"
    ).mock(return_value=httpx.Response(200, json={"person": {}}))

    client.match_person({"email": "test@example.com"})
    assert route.called


@respx.mock
def test_match_person_with_reveal_flags(client):
    route = respx.post(
        f"{BASE}/people/match?reveal_personal_emails=true&reveal_phone_number=true"
    ).mock(return_value=httpx.Response(200, json={"person": {}}))

    client.match_person({"email": "test@example.com"}, True, True)
    assert route.called


# ---- match_company ---------------------------------------------------------


@respx.mock
def test_match_company_post(client):
    route = respx.post(f"{BASE}/organizations/enrich").mock(
        return_value=httpx.Response(200, json={"organization": {}})
    )
    client.match_company({"domain": "example.com"})
    assert route.called


# ---- bulk_enrich_people ----------------------------------------------------


@respx.mock
def test_bulk_enrich_people_default_query_params(client):
    route = respx.post(
        f"{BASE}/people/bulk_match?reveal_personal_emails=false&reveal_phone_number=false"
    ).mock(return_value=httpx.Response(200, json={"matches": []}))
    client.bulk_enrich_people({"people": [{"email": "test@example.com"}]})
    assert route.called


# ---- bulk_enrich_organizations ---------------------------------------------


@respx.mock
def test_bulk_enrich_organizations(client):
    route = respx.post(f"{BASE}/organizations/bulk_enrich").mock(
        return_value=httpx.Response(200, json={"organizations": []})
    )
    client.bulk_enrich_organizations({"organizations": [{"domain": "example.com"}]})
    assert route.called


# ---- get_organization_job_postings -----------------------------------------


@respx.mock
def test_get_organization_job_postings(client):
    route = respx.get(f"{BASE}/organizations/org123/job_postings").mock(
        return_value=httpx.Response(200, json={"job_postings": []})
    )
    client.get_organization_job_postings("org123")

    assert route.called
    assert route.calls.last.request.method == "GET"


# ---- get_complete_organization_info ----------------------------------------


@respx.mock
def test_get_complete_organization_info(client):
    route = respx.get(f"{BASE}/organizations/org123").mock(
        return_value=httpx.Response(200, json={"organization": {}})
    )
    client.get_complete_organization_info("org123")
    assert route.called


# ---- search_news_articles --------------------------------------------------


@respx.mock
def test_search_news_articles(client):
    route = respx.post(f"{BASE}/news_articles/search").mock(
        return_value=httpx.Response(200, json={"news_articles": []})
    )
    client.search_news_articles({"q": "test news", "page": 1})
    assert route.called


# ---- get_api_usage_stats ---------------------------------------------------


@respx.mock
def test_get_api_usage_stats_post_with_empty_body(client):
    route = respx.post(f"{BASE}/usage_stats/api_usage_stats").mock(
        return_value=httpx.Response(200, json={"limits": {}, "usage": {}})
    )
    client.get_api_usage_stats()

    assert route.called
    import json as _json

    assert _json.loads(route.calls.last.request.content) == {}


@respx.mock
def test_get_api_usage_stats_403_for_non_master_key(client):
    respx.post(f"{BASE}/usage_stats/api_usage_stats").mock(
        return_value=httpx.Response(403, text="master key required")
    )
    with pytest.raises(ApolloError, match="Apollo error 403"):
        client.get_api_usage_stats()


# ---- Tier 1 endpoint tests --------------------------------------------------


@respx.mock
def test_get_person(client):
    route = respx.get(f"{BASE}/people/abc123").mock(
        return_value=httpx.Response(200, json={"person": {"id": "abc123"}})
    )
    client.get_person("abc123")
    assert route.called


@respx.mock
def test_search_contacts(client):
    route = respx.post(f"{BASE}/contacts/search").mock(
        return_value=httpx.Response(200, json={"contacts": []})
    )
    client.search_contacts({"q_keywords": "VP"})
    assert route.called


@respx.mock
def test_search_accounts(client):
    route = respx.post(f"{BASE}/accounts/search").mock(
        return_value=httpx.Response(200, json={"accounts": []})
    )
    client.search_accounts({"q_organization_name": "Apollo"})
    assert route.called


@respx.mock
def test_search_deals(client):
    route = respx.post(f"{BASE}/opportunities/search").mock(
        return_value=httpx.Response(200, json={"opportunities": []})
    )
    client.search_deals({"q_name": "Q3"})
    assert route.called


@respx.mock
def test_create_deal(client):
    route = respx.post(f"{BASE}/opportunities").mock(
        return_value=httpx.Response(200, json={"id": "deal_1"})
    )
    client.create_deal({"name": "Big Deal", "amount": "10000"})

    import json as _json

    assert route.called
    assert _json.loads(route.calls.last.request.content) == {
        "name": "Big Deal",
        "amount": "10000",
    }


@respx.mock
def test_update_deal_uses_patch(client):
    route = respx.patch(f"{BASE}/opportunities/deal_1").mock(
        return_value=httpx.Response(200, json={"id": "deal_1", "amount": "20000"})
    )
    client.update_deal("deal_1", {"amount": "20000"})

    assert route.called
    assert route.calls.last.request.method == "PATCH"


@respx.mock
def test_list_deal_stages_get(client):
    route = respx.get(f"{BASE}/opportunity_stages").mock(
        return_value=httpx.Response(200, json={"opportunity_stages": []})
    )
    client.list_deal_stages()
    assert route.called


@respx.mock
def test_list_users_with_pagination(client):
    route = respx.get(f"{BASE}/users/search?page=2&per_page=10").mock(
        return_value=httpx.Response(200, json={"users": []})
    )
    client.list_users(page=2, per_page=10)
    assert route.called


@respx.mock
def test_list_users_no_args(client):
    route = respx.get(f"{BASE}/users/search").mock(
        return_value=httpx.Response(200, json={"users": []})
    )
    client.list_users()
    assert route.called


@respx.mock
def test_create_task(client):
    route = respx.post(f"{BASE}/tasks").mock(
        return_value=httpx.Response(200, json={"success": True, "tasks": []})
    )
    client.create_task({
        "user_id": "u1",
        "contact_ids": ["c1"],
        "type": "call",
        "priority": "high",
        "status": "scheduled",
        "due_at": "2025-02-15T10:00:00Z",
    })
    assert route.called


@respx.mock
def test_search_tasks(client):
    route = respx.post(f"{BASE}/tasks/search").mock(
        return_value=httpx.Response(200, json={"tasks": []})
    )
    client.search_tasks({"owner_ids": ["u1"]})
    assert route.called


@respx.mock
def test_bulk_complete_tasks(client):
    route = respx.post(f"{BASE}/tasks/bulk_complete").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.bulk_complete_tasks({"task_ids": ["t1", "t2"]})
    assert route.called


@respx.mock
def test_search_sequences(client):
    route = respx.post(f"{BASE}/emailer_campaigns/search").mock(
        return_value=httpx.Response(200, json={"emailer_campaigns": []})
    )
    client.search_sequences({"q_name": "outbound"})
    assert route.called


@respx.mock
def test_add_contacts_to_sequence(client):
    route = respx.post(f"{BASE}/emailer_campaigns/seq_1/add_contact_ids").mock(
        return_value=httpx.Response(200, json={"contacts": []})
    )
    client.add_contacts_to_sequence("seq_1", {"contact_ids": ["c1"]})
    assert route.called


@respx.mock
def test_get_organization_top_people(client):
    route = respx.get(
        f"{BASE}/mixed_people/organization_top_people?organization_id=org_1"
    ).mock(return_value=httpx.Response(200, json={"people": []}))
    client.get_organization_top_people("org_1")
    assert route.called
    assert route.calls.last.request.method == "GET"


@respx.mock
def test_add_to_my_prospects(client):
    route = respx.post(f"{BASE}/mixed_people/add_to_my_prospects").mock(
        return_value=httpx.Response(200, json={"contacts": []})
    )
    client.add_to_my_prospects({"entity_ids": ["p1", "p2"]})
    assert route.called


# ---- Tier 2 endpoint tests --------------------------------------------------


@respx.mock
def test_create_contact(client):
    route = respx.post(f"{BASE}/contacts").mock(
        return_value=httpx.Response(200, json={"contact": {"id": "c1"}})
    )
    client.create_contact({"first_name": "Jane", "email": "jane@example.com"})
    assert route.called


@respx.mock
def test_update_contact_uses_patch(client):
    route = respx.patch(f"{BASE}/contacts/c1").mock(
        return_value=httpx.Response(200, json={"contact": {"id": "c1"}})
    )
    client.update_contact("c1", {"title": "VP Eng"})
    assert route.called
    assert route.calls.last.request.method == "PATCH"


@respx.mock
def test_bulk_create_contacts(client):
    route = respx.post(f"{BASE}/contacts/bulk_create").mock(
        return_value=httpx.Response(200, json={"created_contacts": []})
    )
    client.bulk_create_contacts({"contacts": [{"first_name": "A", "email": "a@x.com"}]})
    assert route.called


@respx.mock
def test_match_contact(client):
    route = respx.post(f"{BASE}/contacts/match").mock(
        return_value=httpx.Response(200, json={"contact": {}})
    )
    client.match_contact({"email": "a@x.com"})
    assert route.called


@respx.mock
def test_bulk_match_contacts(client):
    route = respx.post(f"{BASE}/contacts/bulk_match").mock(
        return_value=httpx.Response(200, json={"contacts": []})
    )
    client.bulk_match_contacts({"details": [{"email": "a@x.com"}]})
    assert route.called


@respx.mock
def test_list_contact_stages(client):
    route = respx.get(f"{BASE}/contact_stages").mock(
        return_value=httpx.Response(200, json={"contact_stages": []})
    )
    client.list_contact_stages()
    assert route.called


@respx.mock
def test_create_account(client):
    route = respx.post(f"{BASE}/accounts").mock(
        return_value=httpx.Response(200, json={"account": {"id": "a1"}})
    )
    client.create_account({"name": "Acme", "domain": "acme.com"})
    assert route.called


@respx.mock
def test_update_account_uses_patch(client):
    route = respx.patch(f"{BASE}/accounts/a1").mock(
        return_value=httpx.Response(200, json={"account": {"id": "a1"}})
    )
    client.update_account("a1", {"phone": "+15551234567"})
    assert route.called
    assert route.calls.last.request.method == "PATCH"


@respx.mock
def test_bulk_create_accounts(client):
    route = respx.post(f"{BASE}/accounts/bulk_create").mock(
        return_value=httpx.Response(200, json={"created_accounts": []})
    )
    client.bulk_create_accounts({"accounts": [{"name": "Acme", "domain": "acme.com"}]})
    assert route.called


@respx.mock
def test_list_account_stages(client):
    route = respx.get(f"{BASE}/account_stages").mock(
        return_value=httpx.Response(200, json={"account_stages": []})
    )
    client.list_account_stages()
    assert route.called


@respx.mock
def test_bulk_create_tasks(client):
    route = respx.post(f"{BASE}/tasks/bulk_create").mock(
        return_value=httpx.Response(200, json={"success": True, "tasks": []})
    )
    client.bulk_create_tasks({
        "user_id": "u1",
        "contact_ids": ["c1", "c2"],
        "type": "call",
        "status": "open",
        "due_at": "2025-01-01",
    })
    assert route.called


@respx.mock
def test_create_phone_call(client):
    route = respx.post(f"{BASE}/phone_calls").mock(
        return_value=httpx.Response(200, json={"phone_call": {"id": "pc1"}})
    )
    client.create_phone_call({"contact_id": "c1", "duration": 60})
    assert route.called


@respx.mock
def test_update_phone_call_uses_patch(client):
    route = respx.patch(f"{BASE}/phone_calls/pc1").mock(
        return_value=httpx.Response(200, json={"phone_call": {"id": "pc1"}})
    )
    client.update_phone_call("pc1", {"status": "completed"})
    assert route.called
    assert route.calls.last.request.method == "PATCH"


@respx.mock
def test_remove_contacts_from_sequence(client):
    route = respx.post(
        f"{BASE}/emailer_campaigns/remove_or_stop_contact_ids"
    ).mock(return_value=httpx.Response(200, json={"entity_progress_job": {}}))
    client.remove_contacts_from_sequence({
        "entity_ids": ["c1"],
        "params": {"sequence_ids": ["seq_1"], "mode": "mark_as_finished"},
    })
    assert route.called


@respx.mock
def test_approve_sequence(client):
    route = respx.post(f"{BASE}/emailer_campaigns/seq_1/approve").mock(
        return_value=httpx.Response(200, json={"emailer_campaign": {}})
    )
    client.approve_sequence("seq_1")
    assert route.called


@respx.mock
def test_abort_sequence(client):
    route = respx.post(f"{BASE}/emailer_campaigns/seq_1/abort").mock(
        return_value=httpx.Response(200, json={"emailer_campaign": {}})
    )
    client.abort_sequence("seq_1")
    assert route.called


@respx.mock
def test_archive_sequence(client):
    route = respx.post(f"{BASE}/emailer_campaigns/seq_1/archive").mock(
        return_value=httpx.Response(200, json={"emailer_campaign": {}})
    )
    client.archive_sequence("seq_1")
    assert route.called


@respx.mock
def test_search_emailer_messages(client):
    route = respx.post(f"{BASE}/emailer_messages/search").mock(
        return_value=httpx.Response(200, json={"emailer_messages": []})
    )
    client.search_emailer_messages({"emailer_campaign_ids": ["seq_1"]})
    assert route.called


@respx.mock
def test_get_emailer_message_activities(client):
    route = respx.get(f"{BASE}/emailer_messages/m1/activities").mock(
        return_value=httpx.Response(200, json={"emailer_message_activities": []})
    )
    client.get_emailer_message_activities("m1")
    assert route.called


# ---- Path/QS injection regression tests -------------------------------------


@respx.mock
def test_id_with_path_traversal_is_url_encoded(client):
    """A caller-controlled id containing '/' or '..' must not change the URL path."""
    route = respx.get(
        f"{BASE}/organizations/..%2F..%2Fv1%2Fusers%2Fsearch"
    ).mock(return_value=httpx.Response(200, json={}))
    client.get_complete_organization_info("../../v1/users/search")
    assert route.called


@respx.mock
def test_top_people_query_string_injection_is_blocked(client):
    """An id with '&admin=true' must end up urlencoded, not as a real param."""
    route = respx.get(
        f"{BASE}/mixed_people/organization_top_people"
        f"?organization_id=org_1%26admin%3Dtrue"
    ).mock(return_value=httpx.Response(200, json={}))
    client.get_organization_top_people("org_1&admin=true")
    assert route.called


@respx.mock
def test_update_contact_id_with_slash_is_encoded(client):
    """PATCH on a malicious id must not retarget the route."""
    route = respx.patch(
        f"{BASE}/contacts/..%2Faccounts%2Facc_evil"
    ).mock(return_value=httpx.Response(200, json={}))
    client.update_contact("../accounts/acc_evil", {"phone": "+1"})
    assert route.called


@respx.mock
def test_approve_sequence_id_with_path_segments_is_encoded(client):
    route = respx.post(
        f"{BASE}/emailer_campaigns/seq_1%2Fdisable_safety/approve"
    ).mock(return_value=httpx.Response(200, json={}))
    client.approve_sequence("seq_1/disable_safety")
    assert route.called


# ---- Concurrency: lazy-init Lock --------------------------------------------


def test_get_client_is_thread_safe():
    """Confirm only one httpx.Client is created across concurrent threads."""
    import threading
    from apollo_mcp.apollo import ApolloClient

    c = ApolloClient(api_key="k")
    barrier = threading.Barrier(20)
    seen: set[int] = set()
    lock = threading.Lock()

    def hit() -> None:
        barrier.wait()
        client = c._get_client()
        with lock:
            seen.add(id(client))

    threads = [threading.Thread(target=hit) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    c.close()

    assert len(seen) == 1, f"expected single client instance, got {len(seen)}"


# ---- Regression: 2xx bodies that are not JSON --------------------------------


@respx.mock
def test_204_no_content_returns_empty_dict(client):
    """Sequence approve/abort/archive answer 204; that must not explode."""
    respx.post(f"{BASE}/emailer_campaigns/seq1/approve").mock(
        return_value=httpx.Response(204)
    )
    assert client.approve_sequence("seq1") == {}


@respx.mock
def test_200_with_empty_body_returns_empty_dict(client):
    respx.post(f"{BASE}/tasks/bulk_complete").mock(
        return_value=httpx.Response(200, content=b"")
    )
    assert client.bulk_complete_tasks({"task_ids": ["t1"]}) == {}


@respx.mock
def test_non_json_body_raises_apollo_error_not_json_decode_error(client):
    """An HTML error page with a 200 must surface as a readable ApolloError."""
    respx.post(f"{BASE}/contacts/search").mock(
        return_value=httpx.Response(
            200,
            text="<html>upstream maintenance</html>",
            headers={"content-type": "text/html"},
        )
    )
    with pytest.raises(ApolloError) as exc_info:
        client.search_contacts({})
    message = str(exc_info.value)
    assert "non-JSON" in message
    assert "text/html" in message
    assert "maintenance" in message


# ---- Regression: query-string serialization of dicts and bools ---------------


def test_query_string_serializes_mapping_with_bracket_subkeys():
    from apollo_mcp.apollo import _build_query_string

    qs = _build_query_string({"revenue_range": {"min": 100, "max": 500}})
    assert qs == "?revenue_range[min]=100&revenue_range[max]=500"


def test_query_string_drops_none_subkeys():
    from apollo_mcp.apollo import _build_query_string

    assert _build_query_string({"revenue_range": {"min": 100, "max": None}}) == (
        "?revenue_range[min]=100"
    )


def test_query_string_renders_booleans_lowercase():
    from apollo_mcp.apollo import _build_query_string

    assert _build_query_string({"flag": True}) == "?flag=true"
    assert _build_query_string({"flag": False}) == "?flag=false"


def test_query_string_renders_booleans_lowercase_inside_arrays():
    from apollo_mcp.apollo import _build_query_string

    assert _build_query_string({"person_titles": [True]}) == "?person_titles[]=true"


# ---- Regression: job-postings pagination ------------------------------------


@respx.mock
def test_job_postings_forwards_pagination_params(client):
    route = respx.get(
        f"{BASE}/organizations/org123/job_postings?page=3&per_page=50"
    ).mock(return_value=httpx.Response(200, json={"job_postings": []}))
    client.get_organization_job_postings("org123", {"page": 3, "per_page": 50})
    assert route.called


@respx.mock
def test_job_postings_without_params_has_no_query_string(client):
    route = respx.get(f"{BASE}/organizations/org123/job_postings").mock(
        return_value=httpx.Response(200, json={"job_postings": []})
    )
    client.get_organization_job_postings("org123")
    assert route.called
    assert route.calls.last.request.url.query == b""


# ---- Regression: use after close --------------------------------------------


def test_use_after_close_raises_instead_of_resurrecting_client():
    c = ApolloClient(api_key="k")
    first = c._get_client()
    c.close()
    assert first.is_closed
    with pytest.raises(RuntimeError, match="closed"):
        c._get_client()


def test_close_is_idempotent():
    c = ApolloClient(api_key="k")
    c._get_client()
    c.close()
    c.close()  # must not raise
