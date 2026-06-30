"""Apollo.io REST API client.

Python port of src/apollo.ts using httpx. Preserves the exact request
semantics of the TypeScript original — most notably the people-search
quirk where filters MUST be passed as URL query parameters (sending them
in the JSON body is silently ignored and returns random results).
"""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import quote

import httpx


def _enc(value: str) -> str:
    """URL-encode a single path or query-string segment.

    Used everywhere we interpolate caller-supplied IDs into a URL to
    prevent path traversal and query-string injection. Encodes ``/``,
    ``?``, ``#``, ``&``, ``=`` and other reserved characters.
    """
    return quote(str(value), safe="")

DEFAULT_BASE_URL = "https://api.apollo.io/api/v1"


class ApolloError(Exception):
    """Raised for any non-2xx Apollo API response."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# Params that Apollo expects as a plain string, not array-bracket notation.
# If an array is passed for these, join with newline (Apollo convention).
_STRING_PARAMS: frozenset[str] = frozenset({
    "q_keywords",
})

# Documented People API Search array params. These should use bracket
# notation even if a caller accidentally supplies a scalar.
_ARRAY_PARAMS: frozenset[str] = frozenset({
    "person_titles",
    "person_locations",
    "person_seniorities",
    "organization_locations",
    "q_organization_domains_list",
    "contact_email_status",
    "organization_ids",
    "organization_num_employees_ranges",
    "currently_using_all_of_technology_uids",
    "currently_using_any_of_technology_uids",
    "currently_not_using_any_of_technology_uids",
    "q_organization_job_titles",
    "organization_job_locations",
})

_PEOPLE_SEARCH_PARAM_ALIASES: dict[str, str] = {
    # Backwards-compatible aliases used by older versions of this package,
    # the CLI help, and the original TypeScript port.
    "q": "q_keywords",
    "company_domains": "q_organization_domains_list",
    "q_organization_domains": "q_organization_domains_list",
    "seniority": "person_seniorities",
    "locations": "person_locations",
    "technologies": "currently_using_any_of_technology_uids",
}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _merge_param(
    params: dict[str, Any],
    key: str,
    value: Any,
    *,
    overwrite_scalar: bool = True,
) -> None:
    if value is None:
        return
    if key not in params:
        params[key] = value
        return
    if isinstance(params[key], (list, tuple)) or isinstance(value, (list, tuple)):
        params[key] = _as_list(params[key]) + _as_list(value)
        return
    if overwrite_scalar:
        params[key] = value


def _normalize_people_search_params(params: dict[str, Any]) -> dict[str, Any]:
    """Map legacy/user-facing People Search aliases to Apollo's current API."""
    normalized: dict[str, Any] = {}
    for key, value in params.items():
        base_key = key[:-2] if key.endswith("[]") else key
        canonical_key = _PEOPLE_SEARCH_PARAM_ALIASES.get(base_key, base_key)
        _merge_param(
            normalized,
            canonical_key,
            value,
            overwrite_scalar=canonical_key == base_key,
        )
    return normalized


def _qs_value(value: Any) -> str:
    """Render a scalar for the query string.

    ``bool`` needs Apollo's lowercase ``true``/``false``; a bare ``str(True)``
    yields ``"True"``, which Apollo does not recognize.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _build_query_string(filters: dict[str, Any]) -> str:
    """Serialize filters into Apollo's required query-string shape.

    - Array params use bracket notation: ``person_titles[]=VP&person_titles[]=CTO``
    - Mappings use sub-key brackets: ``revenue_range[min]=100``
    - Members of ``_STRING_PARAMS`` are plain strings; lists join with ``\\n``
    - Booleans render lowercase; ``None`` values are dropped
    """
    parts: list[str] = []
    for key, value in filters.items():
        if value is None:
            continue
        base_key = key[:-2] if key.endswith("[]") else key
        enc_key = quote(base_key, safe="")
        if base_key in _STRING_PARAMS:
            if isinstance(value, (list, tuple)):
                str_val = "\n".join(_qs_value(v) for v in value)
            else:
                str_val = _qs_value(value)
            parts.append(f"{enc_key}={quote(str_val, safe='')}")
        elif isinstance(value, dict):
            # Range filters (revenue_range, organization_num_jobs_range, ...)
            # need bracketed sub-keys. Without this branch they fall through
            # to str() and Apollo receives a Python repr like "{'min': 100}".
            for sub_key, sub_value in value.items():
                if sub_value is None:
                    continue
                parts.append(
                    f"{enc_key}[{quote(str(sub_key), safe='')}]"
                    f"={quote(_qs_value(sub_value), safe='')}"
                )
        elif base_key in _ARRAY_PARAMS or isinstance(value, (list, tuple)):
            for item in _as_list(value):
                parts.append(f"{enc_key}[]={quote(_qs_value(item), safe='')}")
        else:
            parts.append(f"{enc_key}={quote(_qs_value(value), safe='')}")
    return "?" + "&".join(parts) if parts else ""


class ApolloClient:
    """Thin wrapper over Apollo.io's REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._closed = False
        # Guard lazy client init from concurrent FastMCP tool calls.
        self._client_lock = threading.Lock()

    def _get_client(self) -> httpx.Client:
        if self._closed:
            raise RuntimeError(
                "ApolloClient is closed; create a new one instead of reusing it."
            )
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        """Close the underlying transport. Idempotent; the client is unusable after."""
        self._closed = True
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "ApolloClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- internal request helpers ---------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status == 401:
            raise ApolloError(
                "Unauthorized: invalid or missing APOLLO_API_KEY",
                status=status,
            )
        if status == 429:
            retry_after = response.headers.get("retry-after")
            suffix = f" Retry after {retry_after}s." if retry_after else ""
            raise ApolloError(
                f"Rate limited by Apollo (429).{suffix}",
                status=status,
            )
        if 300 <= status < 400:
            location = response.headers.get("location", "<no Location header>")
            raise ApolloError(
                f"Apollo returned an unexpected redirect ({status}) to "
                f"{location}. The httpx client does not follow redirects "
                f"by default; check the endpoint path or base URL.",
                status=status,
            )
        if status >= 400:
            text = response.text
            raise ApolloError(f"Apollo error {status}: {text}", status=status)

    def _decode_json(self, response: httpx.Response) -> Any:
        """Decode a 2xx body, tolerating the empty ones Apollo really sends.

        Several action endpoints (sequence approve/abort/archive, bulk task
        completion) answer ``204 No Content``, and an upstream outage can
        return an HTML error page with a 200. Calling ``.json()`` blindly
        raises ``JSONDecodeError``, which is not an :class:`ApolloError` and
        so escapes every caller's error handling as a bare
        "Expecting value: line 1 column 1".
        """
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "<none>")
            snippet = response.text[:200]
            raise ApolloError(
                f"Apollo returned a non-JSON body (HTTP {response.status_code}, "
                f"content-type {content_type}): {snippet!r}",
                status=response.status_code,
            ) from exc

    def _post(self, path: str, body: Any) -> Any:
        url = f"{self.base_url}{path}"
        response = self._get_client().post(url, headers=self._headers, json=body)
        self._raise_for_status(response)
        return self._decode_json(response)

    def _get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        response = self._get_client().get(url, headers=self._headers)
        self._raise_for_status(response)
        return self._decode_json(response)

    # ---- Apollo endpoints -----------------------------------------------

    def search_people(self, params: dict[str, Any]) -> Any:
        """Search people via ``/mixed_people/api_search``.

        Apollo requires filters as URL query parameters; only ``page`` and
        ``per_page`` are accepted in the JSON body. See the TS port for
        details on why this is necessary.
        """
        normalized = _normalize_people_search_params(params)
        page = normalized.get("page")
        per_page = normalized.get("per_page")
        filters = {
            k: v for k, v in normalized.items() if k not in ("page", "per_page")
        }

        body: dict[str, Any] = {}
        if page is not None:
            body["page"] = page
        if per_page is not None:
            body["per_page"] = per_page

        query_string = _build_query_string(filters)
        return self._post(f"/mixed_people/api_search{query_string}", body)

    def search_companies(self, params: dict[str, Any]) -> Any:
        """Search companies via ``/mixed_companies/search``."""
        return self._post("/mixed_companies/search", params)

    def match_person(
        self,
        params: dict[str, Any],
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> Any:
        """Enrich a single person via ``/people/match``."""
        qs = (
            f"?reveal_personal_emails={str(reveal_personal_emails).lower()}"
            f"&reveal_phone_number={str(reveal_phone_number).lower()}"
        )
        return self._post(f"/people/match{qs}", params)

    def match_company(self, params: dict[str, Any]) -> Any:
        """Enrich a single company via ``/organizations/enrich``."""
        return self._post("/organizations/enrich", params)

    def bulk_enrich_people(
        self,
        params: dict[str, Any],
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> Any:
        """Bulk-enrich people via ``/people/bulk_match``."""
        qs = (
            f"?reveal_personal_emails={str(reveal_personal_emails).lower()}"
            f"&reveal_phone_number={str(reveal_phone_number).lower()}"
        )
        return self._post(f"/people/bulk_match{qs}", params)

    def bulk_enrich_organizations(self, params: dict[str, Any]) -> Any:
        """Bulk-enrich organizations via ``/organizations/bulk_enrich``."""
        return self._post("/organizations/bulk_enrich", params)

    def get_organization_job_postings(
        self,
        organization_id: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Get job postings for a specific organization.

        ``params`` (e.g. ``page`` / ``per_page``) is appended as a query
        string; the CLI documents both flags.
        """
        query_string = _build_query_string(params or {})
        return self._get(
            f"/organizations/{_enc(organization_id)}/job_postings{query_string}"
        )

    def get_complete_organization_info(self, organization_id: str) -> Any:
        """Get complete organization info."""
        return self._get(f"/organizations/{_enc(organization_id)}")

    def search_news_articles(self, params: dict[str, Any]) -> Any:
        """Search news articles via ``/news_articles/search``."""
        return self._post("/news_articles/search", params)

    def get_api_usage_stats(self) -> Any:
        """View team API usage statistics and rate limits.

        Calls ``POST /usage_stats/api_usage_stats``. Requires a *master*
        Apollo API key — non-master keys receive HTTP 403.
        """
        return self._post("/usage_stats/api_usage_stats", {})

    # ---- Tier 1 endpoints -------------------------------------------------

    def get_person(self, person_id: str) -> Any:
        """Look up a single person by Apollo ID via ``GET /people/{id}``."""
        return self._get(f"/people/{_enc(person_id)}")

    def search_contacts(self, params: dict[str, Any]) -> Any:
        """Search your CRM contacts via ``POST /contacts/search``."""
        return self._post("/contacts/search", params)

    def search_accounts(self, params: dict[str, Any]) -> Any:
        """Search your CRM accounts via ``POST /accounts/search``."""
        return self._post("/accounts/search", params)

    def search_deals(self, params: dict[str, Any]) -> Any:
        """Search deals/opportunities via ``POST /opportunities/search``."""
        return self._post("/opportunities/search", params)

    def create_deal(self, params: dict[str, Any]) -> Any:
        """Create a new deal via ``POST /opportunities`` (master key required)."""
        return self._post("/opportunities", params)

    def update_deal(self, opportunity_id: str, params: dict[str, Any]) -> Any:
        """Update an existing deal via ``PATCH /opportunities/{id}``."""
        return self._patch(f"/opportunities/{_enc(opportunity_id)}", params)

    def list_deal_stages(self) -> Any:
        """List deal stages via ``GET /opportunity_stages`` (master key required)."""
        return self._get("/opportunity_stages")

    def list_users(self, page: int | None = None, per_page: int | None = None) -> Any:
        """List teammates via ``GET /users/search`` (master key required)."""
        suffix = _build_query_string({"page": page, "per_page": per_page})
        return self._get(f"/users/search{suffix}")

    def create_task(self, params: dict[str, Any]) -> Any:
        """Create a task via ``POST /tasks``."""
        return self._post("/tasks", params)

    def search_tasks(self, params: dict[str, Any]) -> Any:
        """Search tasks via ``POST /tasks/search``."""
        return self._post("/tasks/search", params)

    def bulk_complete_tasks(self, params: dict[str, Any]) -> Any:
        """Bulk-complete tasks via ``POST /tasks/bulk_complete``."""
        return self._post("/tasks/bulk_complete", params)

    def search_sequences(self, params: dict[str, Any]) -> Any:
        """Search emailer sequences via ``POST /emailer_campaigns/search``."""
        return self._post("/emailer_campaigns/search", params)

    def add_contacts_to_sequence(
        self,
        sequence_id: str,
        params: dict[str, Any],
    ) -> Any:
        """Add contacts to an emailer sequence.

        Calls ``POST /emailer_campaigns/{id}/add_contact_ids``.
        """
        return self._post(
            f"/emailer_campaigns/{_enc(sequence_id)}/add_contact_ids",
            params,
        )

    def get_organization_top_people(self, organization_id: str) -> Any:
        """Get top contacts at an organization.

        Calls ``GET /mixed_people/organization_top_people?organization_id=...``.
        Endpoint is undocumented on docs.apollo.io but listed in
        ``/usage_stats/api_usage_stats`` and used in the wild via GET.
        """
        return self._get(
            f"/mixed_people/organization_top_people?organization_id={_enc(organization_id)}"
        )

    def add_to_my_prospects(self, params: dict[str, Any]) -> Any:
        """Save people to "My Prospects" via ``POST /mixed_people/add_to_my_prospects``."""
        return self._post("/mixed_people/add_to_my_prospects", params)

    # ---- Tier 2 endpoints -------------------------------------------------

    def _patch(self, path: str, body: Any) -> Any:
        """Internal PATCH helper (Apollo's update verb)."""
        url = f"{self.base_url}{path}"
        response = self._get_client().patch(url, headers=self._headers, json=body)
        self._raise_for_status(response)
        return self._decode_json(response)

    # --- Contacts ---
    def create_contact(self, params: dict[str, Any]) -> Any:
        """Create a single CRM contact via ``POST /contacts``."""
        return self._post("/contacts", params)

    def update_contact(self, contact_id: str, params: dict[str, Any]) -> Any:
        """Update a CRM contact via ``PATCH /contacts/{id}``."""
        return self._patch(f"/contacts/{_enc(contact_id)}", params)

    def bulk_create_contacts(self, params: dict[str, Any]) -> Any:
        """Bulk-create CRM contacts via ``POST /contacts/bulk_create``."""
        return self._post("/contacts/bulk_create", params)

    def match_contact(self, params: dict[str, Any]) -> Any:
        """Match a CRM contact via ``POST /contacts/match``."""
        return self._post("/contacts/match", params)

    def bulk_match_contacts(self, params: dict[str, Any]) -> Any:
        """Bulk-match CRM contacts via ``POST /contacts/bulk_match``."""
        return self._post("/contacts/bulk_match", params)

    def list_contact_stages(self) -> Any:
        """List contact stages via ``GET /contact_stages`` (master key required)."""
        return self._get("/contact_stages")

    # --- Accounts ---
    def create_account(self, params: dict[str, Any]) -> Any:
        """Create a single CRM account via ``POST /accounts``."""
        return self._post("/accounts", params)

    def update_account(self, account_id: str, params: dict[str, Any]) -> Any:
        """Update a CRM account via ``PATCH /accounts/{id}``."""
        return self._patch(f"/accounts/{_enc(account_id)}", params)

    def bulk_create_accounts(self, params: dict[str, Any]) -> Any:
        """Bulk-create CRM accounts via ``POST /accounts/bulk_create``."""
        return self._post("/accounts/bulk_create", params)

    def list_account_stages(self) -> Any:
        """List account stages via ``GET /account_stages`` (master key required)."""
        return self._get("/account_stages")

    # --- Tasks ---
    def bulk_create_tasks(self, params: dict[str, Any]) -> Any:
        """Bulk-create tasks via ``POST /tasks/bulk_create``."""
        return self._post("/tasks/bulk_create", params)

    # --- Phone calls ---
    def create_phone_call(self, params: dict[str, Any]) -> Any:
        """Log a phone call via ``POST /phone_calls``."""
        return self._post("/phone_calls", params)

    def update_phone_call(self, phone_call_id: str, params: dict[str, Any]) -> Any:
        """Update a phone-call record via ``PATCH /phone_calls/{id}``."""
        return self._patch(f"/phone_calls/{_enc(phone_call_id)}", params)

    # --- Sequence lifecycle ---
    def remove_contacts_from_sequence(self, params: dict[str, Any]) -> Any:
        """Remove or stop contacts in sequences.

        Calls ``POST /emailer_campaigns/remove_or_stop_contact_ids``. Body
        carries ``entity_ids`` (contact IDs) and a nested ``params`` object
        with ``sequence_ids`` and ``mode`` (e.g. ``"mark_as_finished"``).
        """
        return self._post("/emailer_campaigns/remove_or_stop_contact_ids", params)

    def approve_sequence(self, sequence_id: str) -> Any:
        """Approve a sequence via ``POST /emailer_campaigns/{id}/approve``."""
        return self._post(f"/emailer_campaigns/{_enc(sequence_id)}/approve", {})

    def abort_sequence(self, sequence_id: str) -> Any:
        """Abort a sequence via ``POST /emailer_campaigns/{id}/abort``."""
        return self._post(f"/emailer_campaigns/{_enc(sequence_id)}/abort", {})

    def archive_sequence(self, sequence_id: str) -> Any:
        """Archive a sequence via ``POST /emailer_campaigns/{id}/archive``."""
        return self._post(f"/emailer_campaigns/{_enc(sequence_id)}/archive", {})

    # --- Emailer messages ---
    def search_emailer_messages(self, params: dict[str, Any]) -> Any:
        """Search sent emailer messages via ``POST /emailer_messages/search``."""
        return self._post("/emailer_messages/search", params)

    def get_emailer_message_activities(self, message_id: str) -> Any:
        """Get engagement activities for a message.

        Calls ``GET /emailer_messages/{id}/activities``.
        """
        return self._get(f"/emailer_messages/{_enc(message_id)}/activities")
