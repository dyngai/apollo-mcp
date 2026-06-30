"""Apollo.io MCP server (FastMCP).

Registers 45 tools that wrap :class:`apollo_mcp.apollo.ApolloClient` and
surface Apollo's B2B data to any MCP client (Claude Desktop, web hosts,
etc.). Defaults to stdio transport; HTTP transport is available via the
``--transport http`` CLI flag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections import OrderedDict
from typing import Annotated, Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from pydantic import BaseModel, Field

from apollo_mcp import __version__
from apollo_mcp.apollo import ApolloClient, ApolloError

load_dotenv()

# Upper bound on cached per-bearer-token ApolloClients (see build_server).
_MAX_CACHED_TOKEN_CLIENTS = 32

# ---- Pydantic models for richer filter schemas ------------------------------


class RangeFilter(BaseModel):
    """Min/max numeric range used by several company-search filters."""

    min: float | None = None
    max: float | None = None


class PeopleSearchFilters(BaseModel):
    locations: list[str] | None = Field(
        default=None,
        description="Person locations (cities, states, countries)",
    )
    seniority: list[str] | None = Field(
        default=None,
        description="Seniority levels (e.g., ['C-Level', 'VP', 'Director'])",
    )
    titles: list[str] | None = Field(
        default=None,
        description="Job titles (e.g., ['CEO', 'CTO', 'Sales Manager'])",
    )
    departments: list[str] | None = Field(
        default=None,
        description="Departments (e.g., ['Engineering', 'Sales', 'Marketing'])",
    )
    company_domains: list[str] | None = Field(
        default=None,
        description="Company domains (e.g., ['google.com', 'microsoft.com'])",
    )
    company_names: list[str] | None = Field(
        default=None,
        description="Company names",
    )
    industries: list[str] | None = Field(
        default=None,
        description="Industries (e.g., ['Software', 'Healthcare', 'Finance'])",
    )
    technologies: list[str] | None = Field(
        default=None,
        description="Technologies they use (e.g., ['python', 'react', 'salesforce'])",
    )
    years_of_experience: list[str] | None = Field(
        default=None,
        description="Experience ranges (e.g., ['1-3', '4-6', '7-10'])",
    )
    education_degrees: list[str] | None = Field(
        default=None,
        description="Education degrees (e.g., ['MBA', 'PhD', 'Bachelor'])",
    )
    education_schools: list[str] | None = Field(
        default=None,
        description="Education institutions",
    )


class CompanySearchFilters(BaseModel):
    organization_num_employees_ranges: list[str] | None = Field(
        default=None,
        description=(
            "Employee count ranges in comma format (e.g., ['11,20', '21,50']). "
            "Very restrictive - may return 0 results if too narrow."
        ),
    )
    organization_locations: list[str] | None = Field(
        default=None,
        description=(
            "Company locations (cities, states, countries). "
            "Most effective filter for narrowing results."
        ),
    )
    organization_not_locations: list[str] | None = Field(
        default=None,
        description="Exclude companies from specific locations",
    )
    q_organization_keyword_tags: list[str] | None = Field(
        default=None,
        description=(
            "Industry keywords for filtering (e.g., ['edtech', 'saas', 'fintech']). "
            "Essential for industry targeting."
        ),
    )
    q_organization_name: str | None = Field(
        default=None,
        description="Specific company name filter",
    )
    currently_using_any_of_technology_uids: list[str] | None = Field(
        default=None,
        description="Technologies companies use (e.g., ['salesforce', 'aws', 'react'])",
    )
    revenue_range: RangeFilter | None = Field(
        default=None,
        description="Revenue range in dollars (no commas/symbols)",
    )
    latest_funding_amount_range: RangeFilter | None = Field(
        default=None,
        description="Latest funding round amount range",
    )
    total_funding_range: RangeFilter | None = Field(
        default=None,
        description="Total funding amount range",
    )
    q_organization_job_titles: list[str] | None = Field(
        default=None,
        description="Job titles in active postings",
    )
    organization_job_locations: list[str] | None = Field(
        default=None,
        description="Job posting locations",
    )
    organization_num_jobs_range: RangeFilter | None = Field(
        default=None,
        description="Number of active job postings range",
    )


class BulkPersonInput(BaseModel):
    email: str | None = None
    linkedin_url: str | None = None
    name: str | None = None
    domain: str | None = None
    organization_name: str | None = None
    # Backwards-compatible alias from older MCP schema. Apollo expects
    # `organization_name`, so this is translated before sending.
    company: str | None = None


class BulkOrganizationInput(BaseModel):
    domain: str | None = None
    name: str | None = None


def _bulk_person_detail(person: BulkPersonInput) -> dict[str, Any]:
    detail = {k: v for k, v in person.model_dump().items() if v is not None}
    company = detail.pop("company", None)
    if company is not None:
        detail.setdefault("organization_name", company)
    return detail


# ---- Server setup -----------------------------------------------------------


class _ApolloProxy:
    """Resolves to a per-request :class:`ApolloClient`.

    Tools call ``apollo.search_people(body)`` etc.; this proxy picks the
    right client per invocation: the client cached for the request's bearer
    token in HTTP mode, env-var fallback in stdio, or the test-injected fake.
    """

    __slots__ = ("_resolver",)

    def __init__(self, resolver):
        self._resolver = resolver

    def __getattr__(self, name: str):
        def _call(*args, **kwargs):
            return getattr(self._resolver(), name)(*args, **kwargs)

        return _call


def build_server(client: ApolloClient | None = None) -> tuple[FastMCP, "_ApolloProxy"]:
    """Create the FastMCP server and register all tools.

    Auth resolution order, per tool call:
      1. If ``client`` was passed in (tests inject a fake), use it.
      2. If invoked over HTTP and the request carries
         ``Authorization: Bearer <apollo-api-key>``, build a per-request
         :class:`ApolloClient` from that token.
      3. Otherwise fall back to ``APOLLO_API_KEY`` from the environment
         (kept for stdio/Claude Desktop and CLI flows).
      4. With none of the above, raise a :class:`ToolError` so the MCP
         client surfaces a clear "auth required" message.
    """
    base_url_env = os.environ.get("APOLLO_BASE_URL")

    injected_client: ApolloClient | None = client
    env_fallback_client: ApolloClient | None = None
    if injected_client is None:
        env_key = os.environ.get("APOLLO_API_KEY")
        if env_key:
            env_fallback_client = ApolloClient(api_key=env_key, base_url=base_url_env)

    # One long-lived ApolloClient per bearer token. Building a fresh client
    # for every tool call (and closing it after) threw away httpx's
    # connection pool each time, forcing a new TCP + TLS handshake to Apollo
    # on every request. Bounded so a stream of distinct tokens can't grow
    # without limit; the least-recently-used client is closed on eviction.
    token_clients: OrderedDict[str, ApolloClient] = OrderedDict()
    token_clients_lock = threading.Lock()

    def _client_for_token(token: str) -> ApolloClient:
        with token_clients_lock:
            cached = token_clients.get(token)
            if cached is not None:
                token_clients.move_to_end(token)
                return cached
            fresh = ApolloClient(api_key=token, base_url=base_url_env)
            token_clients[token] = fresh
            while len(token_clients) > _MAX_CACHED_TOKEN_CLIENTS:
                _, evicted = token_clients.popitem(last=False)
                evicted.close()
            return fresh

    def _resolve_client() -> ApolloClient:
        try:
            request = get_http_request()
        except RuntimeError:
            request = None

        if request is not None:
            # HTTP mode: bearer is required; never fall through to the env
            # var so the shared key can't leak across orgs.
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
                if token:
                    return _client_for_token(token)
            if injected_client is not None:
                return injected_client  # tests still drive via injection
            raise ToolError(
                "Authentication required: send Authorization: Bearer "
                "<apollo-api-key>",
            )

        # Stdio path (Claude Desktop, CLI): fall back to the test-injected
        # client first, then to APOLLO_API_KEY from the environment.
        if injected_client is not None:
            return injected_client
        if env_fallback_client is not None:
            return env_fallback_client

        raise ToolError(
            "No Apollo API key available: set APOLLO_API_KEY or pass "
            "Authorization: Bearer <key> on the request.",
        )

    apollo = _ApolloProxy(_resolve_client)

    mcp = FastMCP(name="apollo-mcp", version="0.1.0")

    def _to_text(payload: Any) -> str:
        return json.dumps(payload, indent=2, default=str)

    def _wrap_error(action: str, exc: Exception) -> ToolError:
        return ToolError(f"Error {action}: {exc}")

    # ---- apollo_search_people ------------------------------------------
    @mcp.tool(
        name="apollo_search_people",
        description=(
            "Search for people in Apollo with advanced filtering options. "
            "Use filters to target specific roles, locations, seniority levels, and companies."
        ),
    )
    def apollo_search_people(
        query: Annotated[
            str | None,
            Field(default=None, description="Search query for names, titles, or keywords"),
        ] = None,
        filters: Annotated[
            PeopleSearchFilters | None,
            Field(default=None, description="Advanced filtering options for targeting specific people"),
        ] = None,
        page: Annotated[
            int | None,
            Field(default=None, description="Page number (default: 1)"),
        ] = None,
        per_page: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                le=100,
                description=(
                    "Results per page (1-100, default: 25). Apollo rejects "
                    "anything above 100 with HTTP 422."
                ),
            ),
        ] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            if query:
                body["q_keywords"] = query

            if filters is not None:
                f = filters
                if f.titles:
                    body["person_titles"] = f.titles
                if f.seniority:
                    body["person_seniorities"] = f.seniority
                if f.locations:
                    body["person_locations"] = f.locations
                if f.company_domains:
                    body["q_organization_domains_list"] = f.company_domains
                if f.company_names:
                    body["organization_names"] = f.company_names
                if f.departments:
                    body["departments"] = f.departments
                if f.industries:
                    body["industries"] = f.industries
                if f.technologies:
                    body["currently_using_any_of_technology_uids"] = f.technologies
                if f.years_of_experience:
                    body["years_of_experience"] = f.years_of_experience
                if f.education_degrees:
                    body["education_degrees"] = f.education_degrees
                if f.education_schools:
                    body["education_schools"] = f.education_schools

            if page is not None:
                body["page"] = page
            if per_page is not None:
                body["per_page"] = per_page

            result = apollo.search_people(body) or {}
            people = result.get("people") or []
            simplified_people = [
                {
                    "id": person.get("id"),
                    "name": person.get("name"),
                    "title": person.get("title"),
                    "company": (person.get("organization") or {}).get("name"),
                    "location": person.get("formatted_address"),
                    "linkedin_url": person.get("linkedin_url"),
                    "email": person.get("email"),
                    "seniority": person.get("seniority"),
                }
                for person in people
            ]

            pagination = result.get("pagination") or {}
            summary = {
                "total_results": pagination.get("total_entries", 0),
                "page": pagination.get("page", 1),
                "per_page": pagination.get("per_page", 25),
                "people": simplified_people,
            }
            return _to_text(summary)
        except ApolloError as exc:
            raise _wrap_error("searching people", exc) from exc

    # ---- apollo_search_companies ---------------------------------------
    @mcp.tool(
        name="apollo_search_companies",
        description=(
            "Search for companies/organizations in Apollo with comprehensive filtering options. "
            "Use filters to target specific company types, locations, sizes, and industries."
        ),
    )
    def apollo_search_companies(
        query: Annotated[
            str | None,
            Field(default=None, description="Search query for company names, industries, or keywords"),
        ] = None,
        filters: Annotated[
            CompanySearchFilters | None,
            Field(
                default=None,
                description="Advanced filtering options. Start with location + keywords for best results.",
            ),
        ] = None,
        page: Annotated[
            int | None,
            Field(default=None, description="Page number (default: 1)"),
        ] = None,
        per_page: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                le=100,
                description=(
                    "Results per page (1-100, default: 25). Apollo rejects "
                    "anything above 100 with HTTP 422."
                ),
            ),
        ] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            if query:
                body["q"] = query
            if page is not None:
                body["page"] = page
            if per_page is not None:
                body["per_page"] = per_page

            if filters is not None:
                f = filters

                if f.organization_num_employees_ranges:
                    body["organization_num_employees_ranges"] = [
                        rng.replace("-", ",") if "-" in rng else rng
                        for rng in f.organization_num_employees_ranges
                    ]

                if f.organization_locations:
                    body["organization_locations"] = f.organization_locations

                if f.organization_not_locations:
                    body["organization_not_locations"] = f.organization_not_locations

                if f.revenue_range:
                    rr: dict[str, Any] = {}
                    if f.revenue_range.min is not None:
                        rr["min"] = f.revenue_range.min
                    if f.revenue_range.max is not None:
                        rr["max"] = f.revenue_range.max
                    if rr:
                        body["revenue_range"] = rr

                if f.currently_using_any_of_technology_uids:
                    body["currently_using_any_of_technology_uids"] = (
                        f.currently_using_any_of_technology_uids
                    )

                if f.q_organization_keyword_tags:
                    body["q_organization_keyword_tags"] = f.q_organization_keyword_tags

                if f.q_organization_name:
                    body["q_organization_name"] = f.q_organization_name

                if f.latest_funding_amount_range:
                    lr: dict[str, Any] = {}
                    if f.latest_funding_amount_range.min is not None:
                        lr["min"] = f.latest_funding_amount_range.min
                    if f.latest_funding_amount_range.max is not None:
                        lr["max"] = f.latest_funding_amount_range.max
                    if lr:
                        body["latest_funding_amount_range"] = lr

                if f.total_funding_range:
                    tr: dict[str, Any] = {}
                    if f.total_funding_range.min is not None:
                        tr["min"] = f.total_funding_range.min
                    if f.total_funding_range.max is not None:
                        tr["max"] = f.total_funding_range.max
                    if tr:
                        body["total_funding_range"] = tr

                if f.q_organization_job_titles:
                    body["q_organization_job_titles"] = f.q_organization_job_titles

                if f.organization_job_locations:
                    body["organization_job_locations"] = f.organization_job_locations

                if f.organization_num_jobs_range:
                    jr: dict[str, Any] = {}
                    if f.organization_num_jobs_range.min is not None:
                        jr["min"] = f.organization_num_jobs_range.min
                    if f.organization_num_jobs_range.max is not None:
                        jr["max"] = f.organization_num_jobs_range.max
                    if jr:
                        body["organization_num_jobs_range"] = jr

            result = apollo.search_companies(body) or {}
            organizations = result.get("organizations") or []

            def _location(company: dict[str, Any]) -> str | None:
                if company.get("raw_address"):
                    return company.get("raw_address")
                # Drop blanks before joining; stripping afterwards can't
                # remove a gap in the middle ("San Francisco, , USA").
                parts = [
                    str(part).strip()
                    for part in (
                        company.get("city"),
                        company.get("state"),
                        company.get("country"),
                    )
                    if part and str(part).strip()
                ]
                return ", ".join(parts) or None

            simplified_companies = [
                {
                    "id": company.get("id"),
                    "name": company.get("name"),
                    "website": company.get("website_url"),
                    "industry": company.get("industry"),
                    "employee_count": company.get("employee_count"),
                    "location": _location(company),
                    "linkedin_url": company.get("linkedin_url"),
                    "founded_year": company.get("founded_year"),
                    "phone": company.get("phone"),
                    "revenue": company.get("organization_revenue_printed"),
                    "market_cap": company.get("market_cap"),
                }
                for company in organizations
            ]

            pagination = result.get("pagination") or {}
            summary = {
                "total_results": pagination.get("total_entries", 0),
                "page": pagination.get("page", 1),
                "per_page": pagination.get("per_page", 25),
                "companies": simplified_companies,
            }
            return _to_text(summary)
        except ApolloError as exc:
            raise _wrap_error("searching companies", exc) from exc

    # ---- apollo_enrich_person ------------------------------------------
    @mcp.tool(
        name="apollo_enrich_person",
        description=(
            "Enrich a person by email, linkedin_url, or name+domain/name+organization_name using "
            "Apollo match. Returns a compact summary by default (~25 fields "
            "trimmed to the most useful ones). Pass full=true to return the "
            "raw Apollo payload — those can exceed 100KB and may overflow "
            "MCP token limits, so only use full=true when you need the "
            "complete employment_history / current_technologies / org block."
        ),
    )
    def apollo_enrich_person(
        email: Annotated[str | None, Field(default=None)] = None,
        linkedin_url: Annotated[str | None, Field(default=None)] = None,
        name: Annotated[str | None, Field(default=None)] = None,
        domain: Annotated[str | None, Field(default=None)] = None,
        organization_name: Annotated[str | None, Field(default=None)] = None,
        company: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Deprecated alias for organization_name; kept for compatibility."
                ),
            ),
        ] = None,
        reveal_personal_emails: Annotated[
            bool | None,
            Field(default=None, description="Reveal personal emails (default: false)"),
        ] = None,
        reveal_phone_number: Annotated[
            bool | None,
            Field(default=None, description="Reveal phone numbers (default: false)"),
        ] = None,
        full: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Return the raw Apollo response. Defaults to false (compact)."
                ),
            ),
        ] = False,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            if email is not None:
                body["email"] = email
            if linkedin_url is not None:
                body["linkedin_url"] = linkedin_url
            if name is not None:
                body["name"] = name
            if domain is not None:
                body["domain"] = domain
            if organization_name is not None:
                body["organization_name"] = organization_name
            if company is not None:
                body.setdefault("organization_name", company)
            payload = apollo.match_person(
                body,
                bool(reveal_personal_emails) if reveal_personal_emails is not None else False,
                bool(reveal_phone_number) if reveal_phone_number is not None else False,
            ) or {}
            if full:
                return _to_text(payload)

            person = payload.get("person") or {}
            org = person.get("organization") or {}
            phones = person.get("phone_numbers") or []
            employment_history = person.get("employment_history") or []

            simplified = {
                "id": person.get("id"),
                "first_name": person.get("first_name"),
                "last_name": person.get("last_name"),
                "name": person.get("name"),
                "title": person.get("title"),
                "headline": person.get("headline"),
                "email": person.get("email"),
                "email_status": person.get("email_status"),
                "linkedin_url": person.get("linkedin_url"),
                "twitter_url": person.get("twitter_url"),
                "github_url": person.get("github_url"),
                "facebook_url": person.get("facebook_url"),
                "photo_url": person.get("photo_url"),
                "phone": phones[0].get("sanitized_number") if phones else None,
                "city": person.get("city"),
                "state": person.get("state"),
                "country": person.get("country"),
                "formatted_address": person.get("formatted_address"),
                "departments": person.get("departments"),
                "subdepartments": person.get("subdepartments"),
                "functions": person.get("functions"),
                "seniority": person.get("seniority"),
                "organization_id": person.get("organization_id"),
                "organization": {
                    "id": org.get("id"),
                    "name": org.get("name"),
                    "primary_domain": org.get("primary_domain"),
                    "website_url": org.get("website_url"),
                    "linkedin_url": org.get("linkedin_url"),
                    "industry": org.get("industry"),
                    "estimated_num_employees": org.get("estimated_num_employees"),
                    "founded_year": org.get("founded_year"),
                } if org else None,
                "employment_history": [
                    {
                        "current": role.get("current"),
                        "title": role.get("title"),
                        "organization_name": role.get("organization_name"),
                        "organization_id": role.get("organization_id"),
                        "start_date": role.get("start_date"),
                        "end_date": role.get("end_date"),
                    }
                    for role in employment_history
                ],
            }
            return _to_text(simplified)
        except ApolloError as exc:
            raise _wrap_error("enriching person", exc) from exc

    # ---- apollo_reveal_person ------------------------------------------
    @mcp.tool(
        name="apollo_reveal_person",
        description=(
            "Reveal full contact info (name, email, phone, LinkedIn) for a person by their "
            "Apollo ID. Use person IDs from search results. Costs 1 export credit per call."
        ),
    )
    def apollo_reveal_person(
        id: Annotated[str, Field(description="Apollo person ID from search results")],
        reveal_personal_emails: Annotated[
            bool | None,
            Field(default=None, description="Reveal personal emails (default: false)"),
        ] = None,
        reveal_phone_number: Annotated[
            bool | None,
            Field(default=None, description="Reveal phone numbers (default: false)"),
        ] = None,
    ) -> str:
        try:
            if not id:
                raise ToolError("Person ID is required")
            payload = apollo.match_person(
                {"id": id},
                bool(reveal_personal_emails) if reveal_personal_emails is not None else False,
                bool(reveal_phone_number) if reveal_phone_number is not None else False,
            ) or {}
            person = payload.get("person") or {}
            phone_numbers = person.get("phone_numbers") or []
            phone = phone_numbers[0].get("sanitized_number") if phone_numbers else None

            simplified = {
                "id": person.get("id"),
                "first_name": person.get("first_name"),
                "last_name": person.get("last_name"),
                "name": person.get("name"),
                "title": person.get("title"),
                "email": person.get("email"),
                "email_status": person.get("email_status"),
                "linkedin_url": person.get("linkedin_url"),
                "phone": phone,
                "company": (person.get("organization") or {}).get("name"),
                "city": person.get("city"),
                "state": person.get("state"),
                "country": person.get("country"),
                "photo_url": person.get("photo_url"),
                "twitter_url": person.get("twitter_url"),
                "departments": person.get("departments"),
                "seniority": person.get("seniority"),
            }
            return _to_text(simplified)
        except ToolError:
            raise
        except ApolloError as exc:
            raise _wrap_error("revealing person", exc) from exc

    # ---- apollo_enrich_company -----------------------------------------
    @mcp.tool(
        name="apollo_enrich_company",
        description="Enrich a company/organization by domain or name using Apollo match.",
    )
    def apollo_enrich_company(
        domain: Annotated[str | None, Field(default=None)] = None,
        name: Annotated[str | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            if domain is not None:
                body["domain"] = domain
            if name is not None:
                body["name"] = name
            payload = apollo.match_company(body)
            return _to_text(payload)
        except ApolloError as exc:
            raise _wrap_error("enriching company", exc) from exc

    # ---- apollo_bulk_enrich_people -------------------------------------
    @mcp.tool(
        name="apollo_bulk_enrich_people",
        description=(
            "Bulk enrich multiple people using Apollo match. Provide an array of people with "
            "email, linkedin_url, name+domain, or name+organization_name."
        ),
    )
    def apollo_bulk_enrich_people(
        people: Annotated[list[BulkPersonInput], Field(description="People to enrich")],
        reveal_personal_emails: Annotated[
            bool | None,
            Field(default=None, description="Reveal personal emails (default: false)"),
        ] = None,
        reveal_phone_number: Annotated[
            bool | None,
            Field(default=None, description="Reveal phone numbers (default: false)"),
        ] = None,
    ) -> str:
        try:
            # Apollo expects body field name `details`, not `people`.
            body = {
                "details": [
                    _bulk_person_detail(p)
                    for p in people
                ]
            }
            payload = apollo.bulk_enrich_people(
                body,
                bool(reveal_personal_emails) if reveal_personal_emails is not None else False,
                bool(reveal_phone_number) if reveal_phone_number is not None else False,
            )
            return _to_text(payload)
        except ApolloError as exc:
            raise _wrap_error("bulk enriching people", exc) from exc

    # ---- apollo_bulk_enrich_organizations ------------------------------
    @mcp.tool(
        name="apollo_bulk_enrich_organizations",
        description=(
            "Bulk enrich multiple organizations using Apollo match. Provide an array of "
            "organizations with domain or name."
        ),
    )
    def apollo_bulk_enrich_organizations(
        organizations: Annotated[
            list[BulkOrganizationInput],
            Field(description="Organizations to enrich (only `domain` is used by Apollo)"),
        ],
    ) -> str:
        try:
            # Apollo's /organizations/bulk_enrich takes a flat list of
            # domain strings under the `domains` key.
            domains = [org.domain for org in organizations if org.domain]
            if organizations and not domains:
                # Silently posting {"domains": []} here returns an empty
                # result that looks like "no matches" rather than bad input.
                raise ToolError(
                    "bulk enriching organizations requires a `domain` for at "
                    "least one organization; Apollo's bulk_enrich endpoint "
                    "cannot match on `name`. Use apollo_enrich_company for "
                    "name-only lookups.",
                )
            payload = apollo.bulk_enrich_organizations({"domains": domains})
            return _to_text(payload)
        except ToolError:
            raise
        except ApolloError as exc:
            raise _wrap_error("bulk enriching organizations", exc) from exc

    # ---- apollo_get_organization_job_postings --------------------------
    @mcp.tool(
        name="apollo_get_organization_job_postings",
        description="Get job postings for a specific organization by organization ID.",
    )
    def apollo_get_organization_job_postings(
        organization_id: Annotated[str, Field(description="The Apollo organization ID")],
    ) -> str:
        try:
            payload = apollo.get_organization_job_postings(organization_id)
            return _to_text(payload)
        except ApolloError as exc:
            raise _wrap_error("getting job postings", exc) from exc

    # ---- apollo_get_complete_organization_info -------------------------
    @mcp.tool(
        name="apollo_get_complete_organization_info",
        description="Get complete information for a specific organization by organization ID.",
    )
    def apollo_get_complete_organization_info(
        organization_id: Annotated[str, Field(description="The Apollo organization ID")],
    ) -> str:
        try:
            payload = apollo.get_complete_organization_info(organization_id)
            return _to_text(payload)
        except ApolloError as exc:
            raise _wrap_error("getting organization info", exc) from exc

    # ---- apollo_search_news_articles -----------------------------------
    @mcp.tool(
        name="apollo_search_news_articles",
        description=(
            "Search Apollo for news articles about specific companies. "
            "organization_ids is REQUIRED — Apollo returns 422 without it. "
            "Optionally filter by event categories (e.g. 'hires', 'investment', "
            "'contract') and a published-date range."
        ),
    )
    def apollo_search_news_articles(
        organization_ids: Annotated[
            list[str],
            Field(
                description=(
                    "Apollo organization IDs to scope the search. Required by Apollo. "
                    "Look these up via apollo_search_companies or apollo_enrich_company."
                )
            ),
        ],
        categories: Annotated[
            list[str] | None,
            Field(
                default=None,
                description=(
                    "Event categories to include (e.g. ['hires', 'investment', 'contract']). "
                    "More categories are discoverable via Apollo's News search filter UI."
                ),
            ),
        ] = None,
        published_at_min: Annotated[
            str | None,
            Field(
                default=None,
                description="Lower bound of the publication-date range (YYYY-MM-DD).",
            ),
        ] = None,
        published_at_max: Annotated[
            str | None,
            Field(
                default=None,
                description="Upper bound of the publication-date range (YYYY-MM-DD).",
            ),
        ] = None,
        page: Annotated[
            int | None,
            Field(default=None, description="Page number (default: 1)"),
        ] = None,
        per_page: Annotated[
            int | None,
            Field(
                default=None,
                description="Results per page (default: 25). Smaller values improve latency.",
            ),
        ] = None,
    ) -> str:
        try:
            if not organization_ids:
                raise ToolError("organization_ids is required for news article search")

            # Apollo's /news_articles/search accepts the bracket-notation
            # parameter names `published_at[min]` and `published_at[max]`
            # as **literal JSON keys** in the body. This was verified live
            # (returned 35 articles for apollo.io with min=2024-01-01).
            # The alternative nested form `{"published_at": {"min": ...}}`
            # is not documented for this endpoint, so we stick with the
            # docs-matching keys.
            body: dict[str, Any] = {"organization_ids": organization_ids}
            if categories:
                body["categories"] = categories
            if published_at_min:
                body["published_at[min]"] = published_at_min
            if published_at_max:
                body["published_at[max]"] = published_at_max
            if page is not None:
                body["page"] = page
            if per_page is not None:
                body["per_page"] = per_page

            payload = apollo.search_news_articles(body)
            return _to_text(payload)
        except ToolError:
            raise
        except ApolloError as exc:
            raise _wrap_error("searching news articles", exc) from exc

    # ---- Tier 1 tools --------------------------------------------------

    @mcp.tool(
        name="apollo_get_person",
        description=(
            "Look up a single person by Apollo ID via /people/{id}. "
            "By default returns a compact summary (~50 fields trimmed to the most "
            "useful ones). Pass full=true to return the raw Apollo payload "
            "(can be 100KB+ — only do this when you specifically need the full record)."
        ),
    )
    def apollo_get_person(
        person_id: Annotated[str, Field(description="Apollo person ID")],
        full: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Return the raw Apollo response. Defaults to false (compact). "
                    "Raw payloads are typically 100KB+ and may exceed MCP token limits."
                ),
            ),
        ] = False,
    ) -> str:
        try:
            payload = apollo.get_person(person_id) or {}
            if full:
                return _to_text(payload)

            person = payload.get("person") or {}
            org = person.get("organization") or {}
            phones = person.get("phone_numbers") or []
            employment_history = person.get("employment_history") or []

            simplified = {
                "id": person.get("id"),
                "first_name": person.get("first_name"),
                "last_name": person.get("last_name"),
                "name": person.get("name"),
                "title": person.get("title"),
                "headline": person.get("headline"),
                "email": person.get("email"),
                "email_status": person.get("email_status"),
                "linkedin_url": person.get("linkedin_url"),
                "twitter_url": person.get("twitter_url"),
                "github_url": person.get("github_url"),
                "facebook_url": person.get("facebook_url"),
                "photo_url": person.get("photo_url"),
                "phone": phones[0].get("sanitized_number") if phones else None,
                "city": person.get("city"),
                "state": person.get("state"),
                "country": person.get("country"),
                "formatted_address": person.get("formatted_address"),
                "time_zone": person.get("time_zone"),
                "departments": person.get("departments"),
                "subdepartments": person.get("subdepartments"),
                "functions": person.get("functions"),
                "seniority": person.get("seniority"),
                "organization_id": person.get("organization_id"),
                "organization": {
                    "id": org.get("id"),
                    "name": org.get("name"),
                    "primary_domain": org.get("primary_domain"),
                    "website_url": org.get("website_url"),
                    "linkedin_url": org.get("linkedin_url"),
                    "industry": org.get("industry"),
                    "estimated_num_employees": org.get("estimated_num_employees"),
                    "founded_year": org.get("founded_year"),
                } if org else None,
                "employment_history": [
                    {
                        "current": role.get("current"),
                        "title": role.get("title"),
                        "organization_name": role.get("organization_name"),
                        "organization_id": role.get("organization_id"),
                        "start_date": role.get("start_date"),
                        "end_date": role.get("end_date"),
                    }
                    for role in employment_history
                ],
            }
            return _to_text(simplified)
        except ApolloError as exc:
            raise _wrap_error("getting person", exc) from exc

    @mcp.tool(
        name="apollo_search_contacts",
        description=(
            "Search YOUR Apollo CRM contacts (vs the prospect database). "
            "Accepts arbitrary Apollo search params (q_keywords, contact_label_ids, "
            "contact_stage_ids, sort_ascending, sort_by_field, page, per_page, etc.)."
        ),
    )
    def apollo_search_contacts(
        q_keywords: Annotated[str | None, Field(default=None, description="Free-text search")] = None,
        contact_stage_ids: Annotated[list[str] | None, Field(default=None)] = None,
        contact_label_ids: Annotated[list[str] | None, Field(default=None)] = None,
        page: Annotated[int | None, Field(default=None)] = None,
        per_page: Annotated[int | None, Field(default=None)] = None,
        extra: Annotated[
            dict[str, Any] | None,
            Field(default=None, description="Additional Apollo body params (escape hatch)"),
        ] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            if q_keywords:
                body["q_keywords"] = q_keywords
            if contact_stage_ids:
                body["contact_stage_ids"] = contact_stage_ids
            if contact_label_ids:
                body["contact_label_ids"] = contact_label_ids
            if page is not None:
                body["page"] = page
            if per_page is not None:
                body["per_page"] = per_page
            if extra:
                body.update(extra)
            return _to_text(apollo.search_contacts(body))
        except ApolloError as exc:
            raise _wrap_error("searching contacts", exc) from exc

    @mcp.tool(
        name="apollo_search_accounts",
        description=(
            "Search YOUR Apollo CRM accounts (vs the prospect database). "
            "Accepts q_organization_name, account_stage_ids, sort fields, pagination, etc."
        ),
    )
    def apollo_search_accounts(
        q_organization_name: Annotated[str | None, Field(default=None)] = None,
        account_stage_ids: Annotated[list[str] | None, Field(default=None)] = None,
        page: Annotated[int | None, Field(default=None)] = None,
        per_page: Annotated[int | None, Field(default=None)] = None,
        extra: Annotated[
            dict[str, Any] | None,
            Field(default=None, description="Additional Apollo body params (escape hatch)"),
        ] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            if q_organization_name:
                body["q_organization_name"] = q_organization_name
            if account_stage_ids:
                body["account_stage_ids"] = account_stage_ids
            if page is not None:
                body["page"] = page
            if per_page is not None:
                body["per_page"] = per_page
            if extra:
                body.update(extra)
            return _to_text(apollo.search_accounts(body))
        except ApolloError as exc:
            raise _wrap_error("searching accounts", exc) from exc

    @mcp.tool(
        name="apollo_search_deals",
        description=(
            "Search deals/opportunities. Filter by name keyword, owner, stage, "
            "and date ranges. Accepts pagination and a freeform 'extra' bag for "
            "less-common Apollo params."
        ),
    )
    def apollo_search_deals(
        q_name: Annotated[str | None, Field(default=None, description="Search deals by name keyword")] = None,
        owner_ids: Annotated[list[str] | None, Field(default=None)] = None,
        opportunity_stage_ids: Annotated[list[str] | None, Field(default=None)] = None,
        page: Annotated[int | None, Field(default=None)] = None,
        per_page: Annotated[int | None, Field(default=None)] = None,
        extra: Annotated[dict[str, Any] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            if q_name:
                body["q_name"] = q_name
            if owner_ids:
                body["owner_ids"] = owner_ids
            if opportunity_stage_ids:
                body["opportunity_stage_ids"] = opportunity_stage_ids
            if page is not None:
                body["page"] = page
            if per_page is not None:
                body["per_page"] = per_page
            if extra:
                body.update(extra)
            return _to_text(apollo.search_deals(body))
        except ApolloError as exc:
            raise _wrap_error("searching deals", exc) from exc

    @mcp.tool(
        name="apollo_create_deal",
        description=(
            "Create a new deal/opportunity in your Apollo account. "
            "Requires a master Apollo API key. 'name' is the only required field."
        ),
    )
    def apollo_create_deal(
        name: Annotated[str, Field(description="Name of the deal")],
        owner_id: Annotated[str | None, Field(default=None, description="Apollo user ID of the owner")] = None,
        account_id: Annotated[str | None, Field(default=None, description="Apollo account ID this deal is for")] = None,
        amount: Annotated[
            str | None,
            Field(default=None, description="Monetary amount (no commas/symbols, e.g. '55123478')"),
        ] = None,
        opportunity_stage_id: Annotated[str | None, Field(default=None, description="Apollo deal-stage ID")] = None,
        closed_date: Annotated[str | None, Field(default=None, description="Estimated close date YYYY-MM-DD")] = None,
        typed_custom_fields: Annotated[dict[str, Any] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {"name": name}
            if owner_id is not None:
                body["owner_id"] = owner_id
            if account_id is not None:
                body["account_id"] = account_id
            if amount is not None:
                body["amount"] = amount
            if opportunity_stage_id is not None:
                body["opportunity_stage_id"] = opportunity_stage_id
            if closed_date is not None:
                body["closed_date"] = closed_date
            if typed_custom_fields:
                body["typed_custom_fields"] = typed_custom_fields
            return _to_text(apollo.create_deal(body))
        except ApolloError as exc:
            raise _wrap_error("creating deal", exc) from exc

    @mcp.tool(
        name="apollo_update_deal",
        description=(
            "Update an existing deal/opportunity. Requires a master Apollo API key. "
            "Pass only the fields you want to change."
        ),
    )
    def apollo_update_deal(
        opportunity_id: Annotated[str, Field(description="The deal ID to update")],
        name: Annotated[str | None, Field(default=None)] = None,
        owner_id: Annotated[str | None, Field(default=None)] = None,
        amount: Annotated[str | None, Field(default=None)] = None,
        opportunity_stage_id: Annotated[str | None, Field(default=None)] = None,
        closed_date: Annotated[str | None, Field(default=None, description="YYYY-MM-DD")] = None,
        typed_custom_fields: Annotated[dict[str, Any] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            if name is not None:
                body["name"] = name
            if owner_id is not None:
                body["owner_id"] = owner_id
            if amount is not None:
                body["amount"] = amount
            if opportunity_stage_id is not None:
                body["opportunity_stage_id"] = opportunity_stage_id
            if closed_date is not None:
                body["closed_date"] = closed_date
            if typed_custom_fields:
                body["typed_custom_fields"] = typed_custom_fields
            if not body:
                raise ToolError("update_deal requires at least one field to update")
            return _to_text(apollo.update_deal(opportunity_id, body))
        except ToolError:
            raise
        except ApolloError as exc:
            raise _wrap_error("updating deal", exc) from exc

    @mcp.tool(
        name="apollo_list_deal_stages",
        description=(
            "List the deal stages configured for your Apollo team. Use the IDs "
            "returned here to set 'opportunity_stage_id' on create/update deal. "
            "Requires a master Apollo API key."
        ),
    )
    def apollo_list_deal_stages() -> str:
        try:
            return _to_text(apollo.list_deal_stages())
        except ApolloError as exc:
            raise _wrap_error("listing deal stages", exc) from exc

    @mcp.tool(
        name="apollo_list_users",
        description=(
            "List teammates (users) in your Apollo account. Returns IDs needed "
            "for owner_id/user_id on deals and tasks. Requires a master Apollo API key."
        ),
    )
    def apollo_list_users(
        page: Annotated[int | None, Field(default=None)] = None,
        per_page: Annotated[int | None, Field(default=None)] = None,
    ) -> str:
        try:
            return _to_text(apollo.list_users(page=page, per_page=per_page))
        except ApolloError as exc:
            raise _wrap_error("listing users", exc) from exc

    @mcp.tool(
        name="apollo_create_task",
        description=(
            "Create a task assigned to a teammate, scoped to one or more contacts. "
            "All six core fields (user_id, contact_ids, type, priority, status, due_at) "
            "are required by Apollo."
        ),
    )
    def apollo_create_task(
        user_id: Annotated[str, Field(description="Apollo user ID of the task owner")],
        contact_ids: Annotated[list[str], Field(description="Apollo contact IDs the task is for")],
        type: Annotated[
            str,
            Field(description="Task type (e.g. 'call', 'outreach_manual_email', 'action_item')"),
        ],
        priority: Annotated[str, Field(description="'high', 'medium', or 'low'")],
        status: Annotated[str, Field(description="Task status (e.g. 'scheduled')")],
        due_at: Annotated[str, Field(description="ISO 8601 due timestamp, e.g. '2025-02-15T10:00:00Z'")],
        note: Annotated[str | None, Field(default=None, description="Optional context note")] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {
                "user_id": user_id,
                "contact_ids": contact_ids,
                "type": type,
                "priority": priority,
                "status": status,
                "due_at": due_at,
            }
            if note is not None:
                body["note"] = note
            return _to_text(apollo.create_task(body))
        except ApolloError as exc:
            raise _wrap_error("creating task", exc) from exc

    @mcp.tool(
        name="apollo_search_tasks",
        description=(
            "Search tasks. Accepts open_factor (open_tasks/closed_tasks/etc.), "
            "due_at_range, owner_ids, pagination, and a freeform 'extra' bag."
        ),
    )
    def apollo_search_tasks(
        owner_ids: Annotated[list[str] | None, Field(default=None)] = None,
        contact_ids: Annotated[list[str] | None, Field(default=None)] = None,
        page: Annotated[int | None, Field(default=None)] = None,
        per_page: Annotated[int | None, Field(default=None)] = None,
        extra: Annotated[dict[str, Any] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            if owner_ids:
                body["owner_ids"] = owner_ids
            if contact_ids:
                body["contact_ids"] = contact_ids
            if page is not None:
                body["page"] = page
            if per_page is not None:
                body["per_page"] = per_page
            if extra:
                body.update(extra)
            return _to_text(apollo.search_tasks(body))
        except ApolloError as exc:
            raise _wrap_error("searching tasks", exc) from exc

    @mcp.tool(
        name="apollo_bulk_complete_tasks",
        description="Mark multiple tasks as complete by ID.",
    )
    def apollo_bulk_complete_tasks(
        task_ids: Annotated[list[str], Field(description="Task IDs to complete")],
    ) -> str:
        try:
            return _to_text(apollo.bulk_complete_tasks({"task_ids": task_ids}))
        except ApolloError as exc:
            raise _wrap_error("bulk completing tasks", exc) from exc

    @mcp.tool(
        name="apollo_search_sequences",
        description=(
            "Search emailer sequences (campaigns) in your Apollo account. "
            "Requires a master API key."
        ),
    )
    def apollo_search_sequences(
        q_name: Annotated[str | None, Field(default=None, description="Keyword filter on sequence name")] = None,
        page: Annotated[int | None, Field(default=None)] = None,
        per_page: Annotated[int | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            if q_name:
                body["q_name"] = q_name
            if page is not None:
                body["page"] = page
            if per_page is not None:
                body["per_page"] = per_page
            return _to_text(apollo.search_sequences(body))
        except ApolloError as exc:
            raise _wrap_error("searching sequences", exc) from exc

    @mcp.tool(
        name="apollo_add_contacts_to_sequence",
        description=(
            "Add one or more contact IDs to an emailer sequence. "
            "send_email_from_email_account_id is typically required by Apollo."
        ),
    )
    def apollo_add_contacts_to_sequence(
        sequence_id: Annotated[str, Field(description="Apollo emailer_campaign (sequence) ID")],
        contact_ids: Annotated[list[str], Field(description="Apollo contact IDs to add")],
        send_email_from_email_account_id: Annotated[
            str | None,
            Field(
                default=None,
                description="Apollo email-account ID to send from (look up via apollo MCP "
                "tooling on the email_accounts endpoint)",
            ),
        ] = None,
        sequence_no_email: Annotated[bool | None, Field(default=None)] = None,
        sequence_unverified_email: Annotated[bool | None, Field(default=None)] = None,
        sequence_job_change: Annotated[bool | None, Field(default=None)] = None,
        sequence_active_in_other_campaigns: Annotated[bool | None, Field(default=None)] = None,
        sequence_finished_in_other_campaigns: Annotated[bool | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {"contact_ids": contact_ids}
            for key, value in [
                ("send_email_from_email_account_id", send_email_from_email_account_id),
                ("sequence_no_email", sequence_no_email),
                ("sequence_unverified_email", sequence_unverified_email),
                ("sequence_job_change", sequence_job_change),
                ("sequence_active_in_other_campaigns", sequence_active_in_other_campaigns),
                ("sequence_finished_in_other_campaigns", sequence_finished_in_other_campaigns),
            ]:
                if value is not None:
                    body[key] = value
            return _to_text(apollo.add_contacts_to_sequence(sequence_id, body))
        except ApolloError as exc:
            raise _wrap_error("adding contacts to sequence", exc) from exc

    @mcp.tool(
        name="apollo_get_organization_top_people",
        description=(
            "Get the top contacts at a given organization (good for ICP mapping). "
            "Calls GET /mixed_people/organization_top_people?organization_id=…. "
            "This endpoint is not formally documented on docs.apollo.io but appears "
            "in your usage-stats catalog and is used in the wild."
        ),
    )
    def apollo_get_organization_top_people(
        organization_id: Annotated[str, Field(description="Apollo organization ID")],
    ) -> str:
        try:
            return _to_text(apollo.get_organization_top_people(organization_id))
        except ApolloError as exc:
            raise _wrap_error("getting top people", exc) from exc

    @mcp.tool(
        name="apollo_add_to_my_prospects",
        description=(
            "Save people from prospect search results to 'My Prospects'. "
            "Pass the entity (person) IDs returned from people search."
        ),
    )
    def apollo_add_to_my_prospects(
        entity_ids: Annotated[list[str], Field(description="Apollo person IDs from search results")],
        analytics_context: Annotated[str | None, Field(default=None)] = None,
        skip_fetching_people: Annotated[bool | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {"entity_ids": entity_ids}
            if analytics_context is not None:
                body["analytics_context"] = analytics_context
            if skip_fetching_people is not None:
                body["skip_fetching_people"] = skip_fetching_people
            return _to_text(apollo.add_to_my_prospects(body))
        except ApolloError as exc:
            raise _wrap_error("adding to my prospects", exc) from exc

    # ---- Tier 2 tools --------------------------------------------------

    # --- Contacts ---

    @mcp.tool(
        name="apollo_create_contact",
        description=(
            "Create a single CRM contact in your Apollo account. Apollo dedupes "
            "by email when run_dedupe is true. Common fields are explicit; pass "
            "anything else (custom phones, label_names, typed_custom_fields, etc.) "
            "via the 'extra' bag."
        ),
    )
    def apollo_create_contact(
        first_name: Annotated[str | None, Field(default=None)] = None,
        last_name: Annotated[str | None, Field(default=None)] = None,
        email: Annotated[str | None, Field(default=None)] = None,
        title: Annotated[str | None, Field(default=None)] = None,
        organization_name: Annotated[str | None, Field(default=None)] = None,
        account_id: Annotated[str | None, Field(default=None)] = None,
        linkedin_url: Annotated[str | None, Field(default=None)] = None,
        contact_stage_id: Annotated[str | None, Field(default=None)] = None,
        owner_id: Annotated[str | None, Field(default=None)] = None,
        present_raw_address: Annotated[str | None, Field(default=None)] = None,
        run_dedupe: Annotated[bool | None, Field(default=None)] = None,
        extra: Annotated[
            dict[str, Any] | None,
            Field(default=None, description="Additional Apollo body params (escape hatch)"),
        ] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            for key, value in [
                ("first_name", first_name), ("last_name", last_name), ("email", email),
                ("title", title), ("organization_name", organization_name),
                ("account_id", account_id), ("linkedin_url", linkedin_url),
                ("contact_stage_id", contact_stage_id), ("owner_id", owner_id),
                ("present_raw_address", present_raw_address), ("run_dedupe", run_dedupe),
            ]:
                if value is not None:
                    body[key] = value
            if extra:
                body.update(extra)
            return _to_text(apollo.create_contact(body))
        except ApolloError as exc:
            raise _wrap_error("creating contact", exc) from exc

    @mcp.tool(
        name="apollo_update_contact",
        description=(
            "Update a CRM contact via PATCH /contacts/{id}. Pass only the fields "
            "you want to change. label_names REPLACES the existing list."
        ),
    )
    def apollo_update_contact(
        contact_id: Annotated[str, Field(description="Apollo CRM contact ID")],
        first_name: Annotated[str | None, Field(default=None)] = None,
        last_name: Annotated[str | None, Field(default=None)] = None,
        email: Annotated[str | None, Field(default=None)] = None,
        title: Annotated[str | None, Field(default=None)] = None,
        organization_name: Annotated[str | None, Field(default=None)] = None,
        account_id: Annotated[str | None, Field(default=None)] = None,
        contact_stage_id: Annotated[str | None, Field(default=None)] = None,
        present_raw_address: Annotated[str | None, Field(default=None)] = None,
        direct_phone: Annotated[str | None, Field(default=None)] = None,
        mobile_phone: Annotated[str | None, Field(default=None)] = None,
        corporate_phone: Annotated[str | None, Field(default=None)] = None,
        home_phone: Annotated[str | None, Field(default=None)] = None,
        other_phone: Annotated[str | None, Field(default=None)] = None,
        label_names: Annotated[list[str] | None, Field(default=None)] = None,
        extra: Annotated[dict[str, Any] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            for key, value in [
                ("first_name", first_name), ("last_name", last_name), ("email", email),
                ("title", title), ("organization_name", organization_name),
                ("account_id", account_id), ("contact_stage_id", contact_stage_id),
                ("present_raw_address", present_raw_address),
                ("direct_phone", direct_phone), ("mobile_phone", mobile_phone),
                ("corporate_phone", corporate_phone), ("home_phone", home_phone),
                ("other_phone", other_phone), ("label_names", label_names),
            ]:
                if value is not None:
                    body[key] = value
            if extra:
                body.update(extra)
            if not body:
                raise ToolError("update_contact requires at least one field to update")
            return _to_text(apollo.update_contact(contact_id, body))
        except ToolError:
            raise
        except ApolloError as exc:
            raise _wrap_error("updating contact", exc) from exc

    @mcp.tool(
        name="apollo_bulk_create_contacts",
        description=(
            "Bulk-create CRM contacts (up to 100 per call). Pass 'contacts' as a "
            "list of dicts with first_name/last_name/email and any other fields "
            "(title, organization_name, linkedin_url, contact_stage_id, owner_id, "
            "phone_numbers, etc.). Apollo returns created_contacts and existing_contacts."
        ),
    )
    def apollo_bulk_create_contacts(
        contacts: Annotated[
            list[dict[str, Any]],
            Field(description="List of contact dicts to create"),
        ],
        run_dedupe: Annotated[bool | None, Field(default=None)] = None,
        append_label_names: Annotated[list[str] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {"contacts": contacts}
            if run_dedupe is not None:
                body["run_dedupe"] = run_dedupe
            if append_label_names:
                body["append_label_names"] = append_label_names
            return _to_text(apollo.bulk_create_contacts(body))
        except ApolloError as exc:
            raise _wrap_error("bulk creating contacts", exc) from exc

    @mcp.tool(
        name="apollo_match_contact",
        description=(
            "Match a CRM contact via POST /contacts/match. Like people/match but "
            "scoped to your CRM contacts. Provide email, linkedin_url, or name+org."
        ),
    )
    def apollo_match_contact(
        email: Annotated[str | None, Field(default=None)] = None,
        linkedin_url: Annotated[str | None, Field(default=None)] = None,
        first_name: Annotated[str | None, Field(default=None)] = None,
        last_name: Annotated[str | None, Field(default=None)] = None,
        organization_name: Annotated[str | None, Field(default=None)] = None,
        domain: Annotated[str | None, Field(default=None)] = None,
        extra: Annotated[dict[str, Any] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            for key, value in [
                ("email", email), ("linkedin_url", linkedin_url),
                ("first_name", first_name), ("last_name", last_name),
                ("organization_name", organization_name), ("domain", domain),
            ]:
                if value is not None:
                    body[key] = value
            if extra:
                body.update(extra)
            return _to_text(apollo.match_contact(body))
        except ApolloError as exc:
            raise _wrap_error("matching contact", exc) from exc

    @mcp.tool(
        name="apollo_bulk_match_contacts",
        description=(
            "Bulk-match CRM contacts. Pass 'details' as a list of dicts with "
            "email/linkedin_url/name+org. RATE LIMIT: 100/hour, 20/minute."
        ),
    )
    def apollo_bulk_match_contacts(
        details: Annotated[
            list[dict[str, Any]],
            Field(description="List of contact-identifier dicts to match"),
        ],
    ) -> str:
        try:
            return _to_text(apollo.bulk_match_contacts({"details": details}))
        except ApolloError as exc:
            raise _wrap_error("bulk matching contacts", exc) from exc

    @mcp.tool(
        name="apollo_list_contact_stages",
        description=(
            "List the contact stages configured for your team. Use the IDs to set "
            "contact_stage_id on create/update contact. Requires a master API key."
        ),
    )
    def apollo_list_contact_stages() -> str:
        try:
            return _to_text(apollo.list_contact_stages())
        except ApolloError as exc:
            raise _wrap_error("listing contact stages", exc) from exc

    # --- Accounts ---

    @mcp.tool(
        name="apollo_create_account",
        description=(
            "Create a single CRM account. Common fields are explicit; pass anything "
            "else (label_names, typed_custom_fields, etc.) via 'extra'."
        ),
    )
    def apollo_create_account(
        name: Annotated[str | None, Field(default=None)] = None,
        domain: Annotated[str | None, Field(default=None)] = None,
        phone: Annotated[str | None, Field(default=None)] = None,
        raw_address: Annotated[str | None, Field(default=None)] = None,
        account_stage_id: Annotated[str | None, Field(default=None)] = None,
        owner_id: Annotated[str | None, Field(default=None)] = None,
        run_dedupe: Annotated[bool | None, Field(default=None)] = None,
        extra: Annotated[dict[str, Any] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            for key, value in [
                ("name", name), ("domain", domain), ("phone", phone),
                ("raw_address", raw_address), ("account_stage_id", account_stage_id),
                ("owner_id", owner_id), ("run_dedupe", run_dedupe),
            ]:
                if value is not None:
                    body[key] = value
            if extra:
                body.update(extra)
            return _to_text(apollo.create_account(body))
        except ApolloError as exc:
            raise _wrap_error("creating account", exc) from exc

    @mcp.tool(
        name="apollo_update_account",
        description="Update a CRM account via PATCH /accounts/{id}.",
    )
    def apollo_update_account(
        account_id: Annotated[str, Field(description="Apollo CRM account ID")],
        name: Annotated[str | None, Field(default=None)] = None,
        domain: Annotated[str | None, Field(default=None)] = None,
        phone: Annotated[str | None, Field(default=None)] = None,
        raw_address: Annotated[str | None, Field(default=None)] = None,
        account_stage_id: Annotated[str | None, Field(default=None)] = None,
        owner_id: Annotated[str | None, Field(default=None)] = None,
        label_names: Annotated[list[str] | None, Field(default=None)] = None,
        extra: Annotated[dict[str, Any] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            for key, value in [
                ("name", name), ("domain", domain), ("phone", phone),
                ("raw_address", raw_address), ("account_stage_id", account_stage_id),
                ("owner_id", owner_id), ("label_names", label_names),
            ]:
                if value is not None:
                    body[key] = value
            if extra:
                body.update(extra)
            if not body:
                raise ToolError("update_account requires at least one field to update")
            return _to_text(apollo.update_account(account_id, body))
        except ToolError:
            raise
        except ApolloError as exc:
            raise _wrap_error("updating account", exc) from exc

    @mcp.tool(
        name="apollo_bulk_create_accounts",
        description=(
            "Bulk-create CRM accounts. Pass 'accounts' as a list of dicts with "
            "name/domain/phone/etc."
        ),
    )
    def apollo_bulk_create_accounts(
        accounts: Annotated[
            list[dict[str, Any]],
            Field(description="List of account dicts to create"),
        ],
        run_dedupe: Annotated[bool | None, Field(default=None)] = None,
        append_label_names: Annotated[list[str] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {"accounts": accounts}
            if run_dedupe is not None:
                body["run_dedupe"] = run_dedupe
            if append_label_names:
                body["append_label_names"] = append_label_names
            return _to_text(apollo.bulk_create_accounts(body))
        except ApolloError as exc:
            raise _wrap_error("bulk creating accounts", exc) from exc

    @mcp.tool(
        name="apollo_list_account_stages",
        description=(
            "List the account stages configured for your team. Use the IDs to set "
            "account_stage_id on create/update account. Requires a master API key."
        ),
    )
    def apollo_list_account_stages() -> str:
        try:
            return _to_text(apollo.list_account_stages())
        except ApolloError as exc:
            raise _wrap_error("listing account stages", exc) from exc

    # --- Tasks (bulk) ---

    @mcp.tool(
        name="apollo_bulk_create_tasks",
        description=(
            "Bulk-create tasks. One task is created per contact in contact_ids. "
            "Same required fields as create_task except note is optional."
        ),
    )
    def apollo_bulk_create_tasks(
        user_id: Annotated[str, Field(description="Apollo user ID of the task owner")],
        contact_ids: Annotated[list[str], Field(description="Apollo contact IDs (one task per contact)")],
        type: Annotated[str, Field(description="Task type")],
        status: Annotated[str, Field(description="Task status")],
        due_at: Annotated[str, Field(description="ISO 8601 due timestamp")],
        priority: Annotated[str | None, Field(default=None, description="'high'/'medium'/'low' (default medium)")] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {
                "user_id": user_id,
                "contact_ids": contact_ids,
                "type": type,
                "status": status,
                "due_at": due_at,
            }
            if priority is not None:
                body["priority"] = priority
            return _to_text(apollo.bulk_create_tasks(body))
        except ApolloError as exc:
            raise _wrap_error("bulk creating tasks", exc) from exc

    # --- Phone calls ---

    @mcp.tool(
        name="apollo_create_phone_call",
        description=(
            "Log a phone call in Apollo. Common fields are explicit; "
            "pass less-common fields (recording_url, etc.) via 'extra'."
        ),
    )
    def apollo_create_phone_call(
        contact_id: Annotated[str | None, Field(default=None)] = None,
        user_id: Annotated[str | None, Field(default=None)] = None,
        from_number: Annotated[str | None, Field(default=None)] = None,
        to_number: Annotated[str | None, Field(default=None)] = None,
        status: Annotated[str | None, Field(default=None, description="e.g. 'completed', 'no_answer'")] = None,
        duration: Annotated[int | None, Field(default=None, description="Seconds")] = None,
        note: Annotated[str | None, Field(default=None)] = None,
        called_at: Annotated[str | None, Field(default=None, description="ISO 8601")] = None,
        extra: Annotated[dict[str, Any] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            for key, value in [
                ("contact_id", contact_id), ("user_id", user_id),
                ("from_number", from_number), ("to_number", to_number),
                ("status", status), ("duration", duration),
                ("note", note), ("called_at", called_at),
            ]:
                if value is not None:
                    body[key] = value
            if extra:
                body.update(extra)
            return _to_text(apollo.create_phone_call(body))
        except ApolloError as exc:
            raise _wrap_error("creating phone call", exc) from exc

    @mcp.tool(
        name="apollo_update_phone_call",
        description="Update a phone-call record via PATCH /phone_calls/{id}.",
    )
    def apollo_update_phone_call(
        phone_call_id: Annotated[str, Field(description="Phone-call ID")],
        status: Annotated[str | None, Field(default=None)] = None,
        duration: Annotated[int | None, Field(default=None)] = None,
        note: Annotated[str | None, Field(default=None)] = None,
        extra: Annotated[dict[str, Any] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            for key, value in [("status", status), ("duration", duration), ("note", note)]:
                if value is not None:
                    body[key] = value
            if extra:
                body.update(extra)
            if not body:
                raise ToolError("update_phone_call requires at least one field to update")
            return _to_text(apollo.update_phone_call(phone_call_id, body))
        except ToolError:
            raise
        except ApolloError as exc:
            raise _wrap_error("updating phone call", exc) from exc

    # --- Sequence lifecycle ---

    @mcp.tool(
        name="apollo_remove_contacts_from_sequence",
        description=(
            "Remove or stop contacts in one or more sequences. mode is typically "
            "'mark_as_finished'. Returns a job descriptor (async on Apollo's side)."
        ),
    )
    def apollo_remove_contacts_from_sequence(
        contact_ids: Annotated[list[str], Field(description="Apollo contact IDs (entity_ids)")],
        sequence_ids: Annotated[list[str], Field(description="Sequence IDs to remove from")],
        mode: Annotated[
            str,
            Field(default="mark_as_finished", description="'mark_as_finished' or other documented mode"),
        ] = "mark_as_finished",
    ) -> str:
        try:
            body = {
                "entity_ids": contact_ids,
                "params": {
                    "sequence_ids": sequence_ids,
                    "mode": mode,
                },
            }
            return _to_text(apollo.remove_contacts_from_sequence(body))
        except ApolloError as exc:
            raise _wrap_error("removing contacts from sequence", exc) from exc

    @mcp.tool(
        name="apollo_approve_sequence",
        description="Approve a sequence so it can start sending.",
    )
    def apollo_approve_sequence(
        sequence_id: Annotated[str, Field(description="Apollo emailer_campaign (sequence) ID")],
    ) -> str:
        try:
            return _to_text(apollo.approve_sequence(sequence_id))
        except ApolloError as exc:
            raise _wrap_error("approving sequence", exc) from exc

    @mcp.tool(
        name="apollo_abort_sequence",
        description="Abort an active sequence (stops sending immediately).",
    )
    def apollo_abort_sequence(
        sequence_id: Annotated[str, Field(description="Apollo emailer_campaign (sequence) ID")],
    ) -> str:
        try:
            return _to_text(apollo.abort_sequence(sequence_id))
        except ApolloError as exc:
            raise _wrap_error("aborting sequence", exc) from exc

    @mcp.tool(
        name="apollo_archive_sequence",
        description="Archive a sequence (hide from active list).",
    )
    def apollo_archive_sequence(
        sequence_id: Annotated[str, Field(description="Apollo emailer_campaign (sequence) ID")],
    ) -> str:
        try:
            return _to_text(apollo.archive_sequence(sequence_id))
        except ApolloError as exc:
            raise _wrap_error("archiving sequence", exc) from exc

    # --- Emailer messages ---

    @mcp.tool(
        name="apollo_search_emailer_messages",
        description=(
            "Search sent emailer messages. Filter by sequence, recipient, status, "
            "date range, etc. via the freeform 'extra' bag."
        ),
    )
    def apollo_search_emailer_messages(
        sequence_ids: Annotated[list[str] | None, Field(default=None)] = None,
        contact_ids: Annotated[list[str] | None, Field(default=None)] = None,
        page: Annotated[int | None, Field(default=None)] = None,
        per_page: Annotated[int | None, Field(default=None)] = None,
        extra: Annotated[dict[str, Any] | None, Field(default=None)] = None,
    ) -> str:
        try:
            body: dict[str, Any] = {}
            if sequence_ids:
                body["emailer_campaign_ids"] = sequence_ids
            if contact_ids:
                body["contact_ids"] = contact_ids
            if page is not None:
                body["page"] = page
            if per_page is not None:
                body["per_page"] = per_page
            if extra:
                body.update(extra)
            return _to_text(apollo.search_emailer_messages(body))
        except ApolloError as exc:
            raise _wrap_error("searching emailer messages", exc) from exc

    @mcp.tool(
        name="apollo_get_emailer_message_activities",
        description=(
            "Get engagement activities (opens, clicks, replies, bounces) for a "
            "single emailer message ID."
        ),
    )
    def apollo_get_emailer_message_activities(
        message_id: Annotated[str, Field(description="Apollo emailer_message ID")],
    ) -> str:
        try:
            return _to_text(apollo.get_emailer_message_activities(message_id))
        except ApolloError as exc:
            raise _wrap_error("getting message activities", exc) from exc

    # ---- apollo_get_api_usage_stats ------------------------------------
    @mcp.tool(
        name="apollo_get_api_usage_stats",
        description=(
            "View your team's Apollo API usage and per-minute/hour/day rate "
            "limits across endpoints. Requires a master Apollo API key — "
            "non-master keys will receive HTTP 403."
        ),
    )
    def apollo_get_api_usage_stats() -> str:
        try:
            payload = apollo.get_api_usage_stats()
            return _to_text(payload)
        except ApolloError as exc:
            raise _wrap_error("getting API usage stats", exc) from exc

    return mcp, apollo


def _build_arg_parser() -> argparse.ArgumentParser:
    """argparse for the apollo-mcp console script."""
    parser = argparse.ArgumentParser(
        prog="apollo-mcp",
        description=(
            "Apollo.io MCP server. Defaults to stdio transport (used by "
            "Claude Desktop and most MCP clients). Pass --transport http "
            "to expose the same tools over a Streamable HTTP endpoint."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"apollo-mcp {__version__}",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "sse"),
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind for http/sse transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind for http/sse transports (default: 8000)",
    )
    parser.add_argument(
        "--path",
        default="/mcp/",
        help="HTTP path prefix for the MCP endpoint (default: /mcp/)",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args; isolated from main() for unit testing."""
    return _build_arg_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``apollo-mcp`` console script.

    Defaults to stdio transport. Use ``--transport http`` to expose
    the server over Streamable HTTP at ``http://<host>:<port><path>``
    (default ``http://127.0.0.1:8000/mcp/``).
    """
    args = parse_args(argv)

    mcp, _ = build_server()
    try:
        if args.transport == "stdio":
            mcp.run()
        else:
            # Both 'http' and 'sse' use host/port/path the same way.
            mcp.run(
                transport=args.transport,
                host=args.host,
                port=args.port,
                path=args.path,
            )
    except Exception as exc:  # noqa: BLE001 - top-level diagnostic
        print(f"Failed to start server: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
