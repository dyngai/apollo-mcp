"""Apollo.io command-line interface.

Python port of src/cli.ts. Mirrors the original argument-parsing semantics:

* ``--key value`` and ``--key=value`` formats both supported
* ``--json '{...}'`` merges a JSON object into the params
* Comma-separated values are split into arrays
* A whitelist of fields is auto-coerced to arrays even when given a
  single value (e.g. ``--person_titles "CTO"`` -> ``["CTO"]``)
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from dotenv import load_dotenv

from apollo_mcp import __version__
from apollo_mcp.apollo import ApolloClient, ApolloError

load_dotenv()

VERSION = __version__

COMMANDS: dict[str, str] = {
    "search-people": "Search for people",
    "search-companies": "Search for companies",
    "enrich-person": "Enrich person data by email/LinkedIn",
    "enrich-company": "Enrich company data by domain",
    "bulk-enrich-people": "Bulk enrich multiple people",
    "bulk-enrich-companies": "Bulk enrich multiple companies",
    "org-jobs": "Get job postings for an organization",
    "org-info": "Get complete organization information",
    "search-news": "Search for news articles",
}

# Fields that must always be arrays per Apollo API requirements.
ARRAY_FIELDS: frozenset[str] = frozenset({
    # People search array fields
    "person_titles", "person_seniorities", "seniority", "departments",
    "industries", "technologies", "company_domains", "q_organization_domains",
    "q_organization_domains_list", "person_locations",
    "contact_email_status", "years_of_experience",
    "education_degrees", "education_schools",

    # Company search array fields
    "organization_locations", "organization_not_locations",
    "q_organization_keyword_tags", "organization_num_employees_ranges",
    "currently_using_any_of_technology_uids", "organization_ids",
    "q_organization_job_titles",

    # Common array fields
    "locations",
})

# Fields where a single value contains a comma as part of its semantics —
# Apollo's employee-range format `"11,20"` means "11–20 employees" as ONE
# range, not two values. The default comma-splitting in `_parse_value`
# would corrupt these, so we keep the raw string and let the array-coercion
# step below wrap it in a list.
COMMA_SEMANTIC_FIELDS: frozenset[str] = frozenset({
    "organization_num_employees_ranges",
})

# Recognized parameter names for search-people (drives the unknown-param warning).
VALID_SEARCH_PEOPLE_PARAMS: frozenset[str] = frozenset({
    "person_titles", "person_locations", "person_seniorities", "seniority",
    "departments", "industries", "technologies", "company_domains",
    "q_organization_domains", "q_organization_domains_list",
    "contact_email_status", "years_of_experience",
    "education_degrees", "education_schools", "q", "q_keywords", "page", "per_page",
    "organization_locations", "organization_ids",
    "organization_num_employees_ranges", "revenue_range",
})


# ---- usage / help ----------------------------------------------------------


def print_usage() -> None:
    print(f"""
Apollo.io CLI Tool v{VERSION}

Usage: apollo-cli <command> [options]

Commands:
  search-people           Search for people
  search-companies        Search for companies
  enrich-person           Enrich person data by email/LinkedIn
  enrich-company          Enrich company data by domain
  bulk-enrich-people      Bulk enrich multiple people
  bulk-enrich-companies   Bulk enrich multiple companies
  org-jobs                Get job postings for an organization
  org-info                Get complete organization information
  search-news             Search for news articles

Environment Variables:
  APOLLO_API_KEY          Your Apollo.io API key (required)

Examples:
  # Search for people
  apollo-cli search-people --q "Software Engineer" --person_titles "CEO" --page 1

  # Enrich person by email
  apollo-cli enrich-person --email "tim@apollo.io"

  # Search companies
  apollo-cli search-companies --q "technology" --organization_num_employees_ranges "11,20"

  # Get organization jobs
  apollo-cli org-jobs --id "5f5e2b4b4f3f3d0001234567"

Options are passed as --key value or --key=value
For arrays, use comma-separated values: --titles "CEO,CTO,VP"
For JSON input, use --json '{{"key": "value"}}'

For command-specific help: apollo-cli <command> --help
""")


def print_command_help(command: str) -> None:
    if command == "search-people":
        print("""
apollo-cli search-people --person_titles <titles> [options]

Search for people in Apollo's database with advanced filtering.

IMPORTANT: --person_titles is REQUIRED. The Apollo API requires job titles to search.
This endpoint is designed for prospecting people with active employment.

To find a specific person by name without a job title, use enrichment:
  apollo-cli enrich-person --first_name "John" --last_name "Doe"

Required:
  --person_titles <titles>         Job titles (e.g., "CTO" or "CEO,CTO,VP Engineering")

Optional Filter Options:
  --person_locations <locations>   Locations (e.g., "San Francisco,CA" or "New York")
  --seniority <levels>            Seniority levels (e.g., "C-Level,VP,Director")
  --departments <depts>           Departments (e.g., "Engineering,Sales,Marketing")
  --industries <industries>       Industries (e.g., "Software,SaaS,Healthcare")
  --technologies <tech>           Technologies (e.g., "python,react,salesforce")
  --company_domains <domains>     Company domains (e.g., "google.com,microsoft.com")
  --q <query>                     Search query (combine with filters above)

Pagination:
  --page <number>                 Page number (default: 1)
  --per_page <number>             Results per page (default: 25, Apollo max: 100)

Examples:
  # Search for CTOs in engineering
  apollo-cli search-people --person_titles "CTO" --q "engineering"

  # Search for executives in San Francisco
  apollo-cli search-people --seniority "C-Level,VP" --person_locations "San Francisco,CA"

  # Search for people at specific companies
  apollo-cli search-people --company_domains "google.com,apple.com" --departments "Engineering"

All filter values are automatically converted to arrays (commas optional for single values).
""")
    elif command == "search-companies":
        print("""
apollo-cli search-companies [options]

Search for companies/organizations in Apollo's database.

Common Filter Options:
  --organization_locations <loc>           Company locations (e.g., "California,New York")
  --organization_not_locations <loc>       Exclude locations
  --q_organization_keyword_tags <tags>     Industry keywords (e.g., "edtech,saas,fintech")
  --organization_num_employees_ranges <r>  Employee ranges (e.g., "11,20" or "51,200,201,500")
  --currently_using_any_of_technology_uids Technologies used
  --q <query>                              Search query

Pagination:
  --page <number>                          Page number (default: 1)
  --per_page <number>                      Results per page (default: 25)

Employee Range Format:
  Use comma-separated min,max pairs: "11,20" means 11-20 employees
  Multiple ranges: "11,20,21,50,51,100" searches three ranges

Examples:
  # Search for tech companies in California
  apollo-cli search-companies --q_organization_keyword_tags "technology,saas" \\
    --organization_locations "California"

  # Search by employee count
  apollo-cli search-companies --organization_num_employees_ranges "51,200" \\
    --organization_locations "San Francisco,CA"

  # Search with keyword
  apollo-cli search-companies --q "education technology" \\
    --q_organization_keyword_tags "edtech"
""")
    elif command == "enrich-person":
        print("""
apollo-cli enrich-person [options]

Enrich person data by email, LinkedIn URL, or name/company.

Required (one of):
  --email <email>              Person's email address
  --linkedin_url <url>         LinkedIn profile URL
  --first_name <name>          First name (requires --last_name and --organization_name)
  --last_name <name>           Last name (requires --first_name)
  --organization_name <name>   Company name (for name-based matching)

Optional:
  --reveal_personal_emails <bool>  Reveal personal emails (uses credits)
  --reveal_phone_number <bool>     Reveal phone number (uses credits)

Examples:
  # Enrich by email
  apollo-cli enrich-person --email "tim@apollo.io"

  # Enrich by LinkedIn
  apollo-cli enrich-person --linkedin_url "https://www.linkedin.com/in/tim-zheng"

  # Enrich by name and company
  apollo-cli enrich-person --first_name "Tim" --last_name "Zheng" \\
    --organization_name "Apollo"

  # With personal contact reveal (uses credits)
  apollo-cli enrich-person --email "tim@apollo.io" \\
    --reveal_personal_emails true --reveal_phone_number true
""")
    elif command == "enrich-company":
        print("""
apollo-cli enrich-company [options]

Enrich company data by domain or name.

Required (one of):
  --domain <domain>    Company domain (e.g., "apollo.io")
  --name <name>        Company name (e.g., "Apollo.io")

Examples:
  # Enrich by domain
  apollo-cli enrich-company --domain "apollo.io"

  # Enrich by name
  apollo-cli enrich-company --name "Apollo.io"
""")
    elif command == "org-jobs":
        print("""
apollo-cli org-jobs --id <organization_id> [options]

Get job postings for a specific organization.

Required:
  --id <id>           Apollo organization ID

Optional:
  --page <number>     Page number (default: 1)
  --per_page <number> Results per page (default: 10)

Example:
  apollo-cli org-jobs --id "5e66b6381e05b4008c8331b8" --per_page 20
""")
    elif command == "org-info":
        print("""
apollo-cli org-info --id <organization_id>

Get complete information for a specific organization.

Required:
  --id <id>    Apollo organization ID

Example:
  apollo-cli org-info --id "5e66b6381e05b4008c8331b8"
""")
    elif command == "bulk-enrich-people":
        print("""
apollo-cli bulk-enrich-people --json <json_data>

Bulk enrich multiple people at once.

Required:
  --json <json>    JSON object with "details" array

JSON Format:
  {
    "details": [
      {"email": "person1@example.com"},
      {"first_name": "John", "last_name": "Doe", "organization_name": "Company"}
    ]
  }

Example:
  apollo-cli bulk-enrich-people --json '{
    "details": [
      {"email": "tim@apollo.io"},
      {"first_name": "Roy", "last_name": "Chung", "organization_name": "Apollo"}
    ]
  }'
""")
    elif command == "bulk-enrich-companies":
        print("""
apollo-cli bulk-enrich-companies --json <json_data>

Bulk enrich multiple companies at once.

Required:
  --json <json>    JSON object with "domains" array

JSON Format:
  {
    "domains": ["company1.com", "company2.com", "company3.com"]
  }

Example:
  apollo-cli bulk-enrich-companies --json '{
    "domains": ["apollo.io", "salesforce.com", "hubspot.com"]
  }'
""")
    elif command == "search-news":
        print("""
apollo-cli search-news [options]

Search for news articles.

Options:
  --q <query>                  Search query
  --news_event_types <types>   Event types (e.g., "funding,acquisition")
  --organization_ids <ids>     Organization IDs
  --page <number>              Page number (default: 1)
  --per_page <number>          Results per page (default: 10)

Example:
  apollo-cli search-news --q "Series A" --news_event_types "funding"
""")
    else:
        print_usage()


# ---- arg parsing -----------------------------------------------------------


# Only canonical integers coerce to int. A leading zero means the digits are
# an identifier, not a number (zip codes, some numeric IDs) — "01234" must
# stay "01234" rather than silently becoming 1234.
_INT_RE = re.compile(r"^(?:0|[1-9]\d*)$")


def _parse_value(value: str) -> Any:
    if _INT_RE.match(value):
        return int(value)
    if value == "true":
        return True
    if value == "false":
        return False
    if "," in value:
        return [v.strip() for v in value.split(",")]
    return value


def parse_args(args: list[str]) -> tuple[str | None, dict[str, Any]]:
    if args and args[0] in ("--version", "-v"):
        print(VERSION)
        sys.exit(0)

    if not args or args[0] in ("--help", "-h"):
        print_usage()
        sys.exit(0)

    command = args[0]

    # Command-specific help
    if len(args) > 1 and args[1] in ("--help", "-h"):
        if command in COMMANDS:
            print_command_help(command)
            sys.exit(0)

    params: dict[str, Any] = {}

    i = 1
    while i < len(args):
        arg = args[i]

        if arg == "--json" and i + 1 < len(args):
            try:
                merged = json.loads(args[i + 1])
            except json.JSONDecodeError as exc:
                print(f"Error parsing JSON: {exc}", file=sys.stderr)
                sys.exit(1)
            if isinstance(merged, dict):
                params.update(merged)
            else:
                print("Error parsing JSON: top-level value must be an object", file=sys.stderr)
                sys.exit(1)
            i += 2
            continue

        if arg.startswith("--") and "=" in arg:
            key, _, value = arg[2:].partition("=")
            if key in COMMA_SEMANTIC_FIELDS:
                params[key] = value
            else:
                params[key] = _parse_value(value)
            i += 1
            continue

        if arg.startswith("--"):
            key = arg[2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                if key in COMMA_SEMANTIC_FIELDS:
                    params[key] = args[i + 1]
                else:
                    params[key] = _parse_value(args[i + 1])
                i += 2
            else:
                params[key] = True
                i += 1
            continue

        i += 1

    # Auto-coerce known array fields to lists if a scalar was provided.
    for key in list(params.keys()):
        if key in ARRAY_FIELDS and not isinstance(params[key], list):
            params[key] = [params[key]]

    return command, params


# ---- command runner --------------------------------------------------------


def _run_command(client: ApolloClient, command: str, params: dict[str, Any]) -> Any:
    if command == "search-people":
        if not params.get("person_titles"):
            print("Error: --person_titles is required for search-people", file=sys.stderr)
            print("", file=sys.stderr)
            print("The Apollo API requires job titles to search for people.", file=sys.stderr)
            print("", file=sys.stderr)
            print("Examples:", file=sys.stderr)
            print('  apollo-cli search-people --person_titles "CEO,CTO"', file=sys.stderr)
            print('  apollo-cli search-people --person_titles "Manager" --person_locations "Virginia"', file=sys.stderr)
            print("", file=sys.stderr)
            print("To find a specific person by name, use enrichment instead:", file=sys.stderr)
            print('  apollo-cli enrich-person --first_name "John" --last_name "Doe" --organization_name "Company"', file=sys.stderr)
            print("", file=sys.stderr)
            print("For help: apollo-cli search-people --help", file=sys.stderr)
            sys.exit(1)

        unrecognized = [k for k in params if k not in VALID_SEARCH_PEOPLE_PARAMS]
        if unrecognized:
            joined = ", ".join("--" + p for p in unrecognized)
            print(f"Warning: Unrecognized parameter(s): {joined}", file=sys.stderr)
            print(
                "These are still sent to Apollo, which may ignore them or reject "
                "the request. Run --help to see valid parameters.",
                file=sys.stderr,
            )
            print("", file=sys.stderr)

        return client.search_people(params)

    if command == "search-companies":
        return client.search_companies(params)

    if command == "enrich-person":
        reveal_emails = params.get("reveal_personal_emails") in (True, "true")
        reveal_phone = params.get("reveal_phone_number") in (True, "true")
        params.pop("reveal_personal_emails", None)
        params.pop("reveal_phone_number", None)
        return client.match_person(params, reveal_emails, reveal_phone)

    if command == "enrich-company":
        return client.match_company(params)

    if command == "bulk-enrich-people":
        reveal_emails = params.get("reveal_personal_emails") in (True, "true")
        reveal_phone = params.get("reveal_phone_number") in (True, "true")
        params.pop("reveal_personal_emails", None)
        params.pop("reveal_phone_number", None)
        return client.bulk_enrich_people(params, reveal_emails, reveal_phone)

    if command == "bulk-enrich-companies":
        return client.bulk_enrich_organizations(params)

    if command == "org-jobs":
        if not params.get("id"):
            print("Error: --id is required for org-jobs command", file=sys.stderr)
            sys.exit(1)
        org_id = params.pop("id")
        return client.get_organization_job_postings(org_id, params)

    if command == "org-info":
        if not params.get("id"):
            print("Error: --id is required for org-info command", file=sys.stderr)
            sys.exit(1)
        return client.get_complete_organization_info(params["id"])

    if command == "search-news":
        return client.search_news_articles(params)

    print(f"Unknown command: {command}", file=sys.stderr)
    print_usage()
    sys.exit(1)


def run_command(client: ApolloClient, command: str, params: dict[str, Any]) -> None:
    try:
        result = _run_command(client, command, params)
        print(json.dumps(result, indent=2, default=str))
    except ApolloError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    command, params = parse_args(argv)

    if not command or command not in COMMANDS:
        print(f'Error: Invalid command "{command}"', file=sys.stderr)
        print_usage()
        sys.exit(1)

    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        print("Error: APOLLO_API_KEY environment variable is required", file=sys.stderr)
        print("Set it in a .env file or export APOLLO_API_KEY=your_key_here", file=sys.stderr)
        sys.exit(1)

    client = ApolloClient(api_key=api_key, base_url=os.environ.get("APOLLO_BASE_URL"))
    try:
        run_command(client, command, params)
    finally:
        client.close()


if __name__ == "__main__":
    main()
