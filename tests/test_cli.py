"""CLI tests (port of src/cli.test.ts).

Spawns the CLI via subprocess to assert on usage/help, error paths, and
argument parsing. Real API calls are gated behind the integration mark.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from apollo_mcp import __version__
from apollo_mcp import cli
from apollo_mcp.apollo import ApolloError
from apollo_mcp.cli import _parse_value, parse_args


# ---- in-process arg-parsing tests ------------------------------------------


def test_parse_value_int():
    assert _parse_value("42") == 42


def test_parse_value_bool():
    assert _parse_value("true") is True
    assert _parse_value("false") is False


def test_parse_value_list():
    assert _parse_value("a,b,c") == ["a", "b", "c"]


def test_parse_value_string():
    assert _parse_value("hello") == "hello"


def test_parse_args_key_value(monkeypatch):
    cmd, params = parse_args(["search-people", "--q", "Software Engineer", "--page", "1"])
    assert cmd == "search-people"
    assert params["q"] == "Software Engineer"
    assert params["page"] == 1


def test_parse_args_key_equals_value():
    cmd, params = parse_args(["search-people", "--q=Software Engineer", "--page=2"])
    assert cmd == "search-people"
    assert params["q"] == "Software Engineer"
    assert params["page"] == 2


def test_parse_args_array_field_auto_coerced():
    _, params = parse_args(["search-people", "--person_titles", "CTO"])
    assert params["person_titles"] == ["CTO"]


def test_parse_args_comma_array():
    _, params = parse_args(["search-people", "--person_titles", "CEO,CTO,VP"])
    assert params["person_titles"] == ["CEO", "CTO", "VP"]


def test_parse_args_employee_range_keeps_comma_intact():
    """`organization_num_employees_ranges "11,20"` is ONE range, not two values."""
    _, params = parse_args(
        ["search-companies", "--organization_num_employees_ranges", "11,20"]
    )
    assert params["organization_num_employees_ranges"] == ["11,20"]


def test_parse_args_employee_range_keeps_comma_intact_with_equals():
    _, params = parse_args(
        ["search-companies", "--organization_num_employees_ranges=51,200"]
    )
    assert params["organization_num_employees_ranges"] == ["51,200"]


def test_parse_args_json_merges_object():
    _, params = parse_args(
        ["search-people", "--json", '{"q": "test", "page": 1}']
    )
    assert params["q"] == "test"
    assert params["page"] == 1


def test_parse_args_invalid_json_exits(capsys):
    with pytest.raises(SystemExit):
        parse_args(["search-people", "--json", "not json"])
    err = capsys.readouterr().err
    assert "Error parsing JSON" in err


def test_parse_args_help_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["--help"])
    assert exc.value.code == 0
    assert "Apollo.io CLI Tool" in capsys.readouterr().out


def test_parse_args_version_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_parse_args_command_help_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["search-people", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "search-people --person_titles" in out
    assert "REQUIRED" in out


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("search-companies", "Employee Range Format"),
        ("enrich-person", "--reveal_personal_emails"),
        ("enrich-company", "enrich-company [options]"),
        ("org-jobs", "org-jobs --id"),
        ("org-info", "org-info --id"),
        ("bulk-enrich-people", "details"),
        ("bulk-enrich-companies", "domains"),
        ("search-news", "news_event_types"),
        ("unknown", "Apollo.io CLI Tool"),
    ],
)
def test_print_command_help_all_branches(command, expected, capsys):
    cli.print_command_help(command)
    assert expected in capsys.readouterr().out


def test_parse_args_json_non_object_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["search-companies", "--json", '["not", "object"]'])
    assert exc.value.code == 1
    assert "top-level value must be an object" in capsys.readouterr().err


def test_parse_args_flag_without_value_and_ignored_positional():
    cmd, params = parse_args(["search-companies", "ignored-positional", "--debug"])
    assert cmd == "search-companies"
    assert params == {"debug": True}


def test_run_command_search_people_requires_titles(capsys):
    with pytest.raises(SystemExit) as exc:
        cli._run_command(MagicMock(), "search-people", {"person_locations": ["Virginia"]})
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "person_titles is required" in err
    assert "enrich-person" in err


def test_run_command_search_people_warns_for_unknown_params(capsys):
    client = MagicMock()
    client.search_people.return_value = {"people": []}

    result = cli._run_command(
        client,
        "search-people",
        {"person_titles": ["CEO"], "person_location": "Virginia"},
    )

    assert result == {"people": []}
    client.search_people.assert_called_once_with({
        "person_titles": ["CEO"],
        "person_location": "Virginia",
    })
    err = capsys.readouterr().err
    assert "Unrecognized parameter" in err
    assert "person_location" in err


def test_run_command_dispatches_success_branches():
    client = MagicMock()
    client.search_companies.return_value = {"companies": []}
    client.match_person.return_value = {"person": {}}
    client.match_company.return_value = {"organization": {}}
    client.bulk_enrich_people.return_value = {"matches": []}
    client.bulk_enrich_organizations.return_value = {"organizations": []}
    client.get_organization_job_postings.return_value = {"job_postings": []}
    client.get_complete_organization_info.return_value = {"organization": {}}
    client.search_news_articles.return_value = {"news_articles": []}

    assert cli._run_command(client, "search-companies", {"q": "x"}) == {"companies": []}
    client.search_companies.assert_called_once_with({"q": "x"})

    enrich_params = {
        "email": "a@example.com",
        "reveal_personal_emails": True,
        "reveal_phone_number": "true",
    }
    assert cli._run_command(client, "enrich-person", enrich_params) == {"person": {}}
    client.match_person.assert_called_once_with({"email": "a@example.com"}, True, True)
    assert enrich_params == {"email": "a@example.com"}

    assert cli._run_command(client, "enrich-company", {"domain": "apollo.io"}) == {
        "organization": {}
    }
    client.match_company.assert_called_once_with({"domain": "apollo.io"})

    bulk_people_params = {
        "details": [{"email": "a@example.com"}],
        "reveal_personal_emails": "true",
        "reveal_phone_number": False,
    }
    assert cli._run_command(client, "bulk-enrich-people", bulk_people_params) == {
        "matches": []
    }
    client.bulk_enrich_people.assert_called_once_with(
        {"details": [{"email": "a@example.com"}]},
        True,
        False,
    )

    assert cli._run_command(client, "bulk-enrich-companies", {"domains": ["a.com"]}) == {
        "organizations": []
    }
    client.bulk_enrich_organizations.assert_called_once_with({"domains": ["a.com"]})

    assert cli._run_command(client, "org-jobs", {"id": "org1", "page": 2}) == {
        "job_postings": []
    }
    client.get_organization_job_postings.assert_called_once_with("org1", {"page": 2})

    assert cli._run_command(client, "org-info", {"id": "org1"}) == {"organization": {}}
    client.get_complete_organization_info.assert_called_once_with("org1")

    assert cli._run_command(client, "search-news", {"organization_ids": ["org1"]}) == {
        "news_articles": []
    }
    client.search_news_articles.assert_called_once_with({"organization_ids": ["org1"]})


@pytest.mark.parametrize(
    ("command", "params", "message"),
    [
        ("org-jobs", {}, "--id is required"),
        ("org-info", {}, "--id is required"),
        ("nope", {}, "Unknown command"),
    ],
)
def test_run_command_error_branches(command, params, message, capsys):
    with pytest.raises(SystemExit) as exc:
        cli._run_command(MagicMock(), command, params)
    assert exc.value.code == 1
    assert message in capsys.readouterr().err


def test_run_command_prints_json(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_run_command", lambda _client, _command, _params: {"ok": True})

    cli.run_command(MagicMock(), "search-companies", {})

    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_run_command_apollo_error_exits(capsys, monkeypatch):
    def boom(_client, _command, _params):
        raise ApolloError("bad key", status=401)

    monkeypatch.setattr(cli, "_run_command", boom)

    with pytest.raises(SystemExit) as exc:
        cli.run_command(MagicMock(), "search-companies", {})
    assert exc.value.code == 1
    assert "bad key" in capsys.readouterr().err


def test_run_command_generic_error_exits(capsys, monkeypatch):
    def boom(_client, _command, _params):
        raise RuntimeError("broken")

    monkeypatch.setattr(cli, "_run_command", boom)

    with pytest.raises(SystemExit) as exc:
        cli.run_command(MagicMock(), "search-companies", {})
    assert exc.value.code == 1
    assert "broken" in capsys.readouterr().err


def test_run_command_reraises_system_exit(monkeypatch):
    def boom(_client, _command, _params):
        raise SystemExit(7)

    monkeypatch.setattr(cli, "_run_command", boom)

    with pytest.raises(SystemExit) as exc:
        cli.run_command(MagicMock(), "search-companies", {})
    assert exc.value.code == 7


def test_main_invalid_command_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["invalid-command"])
    assert exc.value.code == 1
    assert "Invalid command" in capsys.readouterr().err


def test_main_missing_api_key_exits(monkeypatch, capsys):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)

    with pytest.raises(SystemExit) as exc:
        cli.main(["search-companies"])
    assert exc.value.code == 1
    assert "APOLLO_API_KEY" in capsys.readouterr().err


def test_main_uses_sys_argv_and_closes_client(monkeypatch):
    created = []

    class FakeClient:
        def __init__(self, api_key, base_url=None):
            self.api_key = api_key
            self.base_url = base_url
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    calls = []
    monkeypatch.setenv("APOLLO_API_KEY", "key")
    monkeypatch.setenv("APOLLO_BASE_URL", "https://example.test")
    monkeypatch.setattr(sys, "argv", ["apollo-cli", "search-companies", "--q", "apollo"])
    monkeypatch.setattr(cli, "ApolloClient", FakeClient)
    monkeypatch.setattr(
        cli,
        "run_command",
        lambda client, command, params: calls.append((client, command, params)),
    )

    cli.main()

    assert len(created) == 1
    assert created[0].api_key == "key"
    assert created[0].base_url == "https://example.test"
    assert created[0].closed is True
    assert calls == [(created[0], "search-companies", {"q": "apollo"})]


# ---- subprocess-driven tests (mirror cli.test.ts) --------------------------


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_cli(args: list[str], env: dict | None = None, timeout: int = 10) -> subprocess.CompletedProcess:
    base_env = os.environ.copy()
    base_env.pop("APOLLO_API_KEY", None)
    if env:
        base_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "apollo_mcp.cli", *args],
        cwd=REPO_ROOT,
        env=base_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_cli_version_flag():
    result = _run_cli(["--version"])
    assert result.returncode == 0
    # semver-like
    parts = result.stdout.strip().split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_cli_short_version_flag():
    result = _run_cli(["-v"])
    assert result.returncode == 0


def test_cli_help_flag_lists_commands():
    result = _run_cli(["--help"])
    assert result.returncode == 0
    assert "Apollo.io CLI Tool" in result.stdout
    assert "search-people" in result.stdout
    assert "enrich-person" in result.stdout


def test_cli_no_args_shows_usage():
    result = _run_cli([])
    assert "Apollo.io CLI Tool" in result.stdout


def test_cli_search_people_help():
    result = _run_cli(["search-people", "--help"])
    assert "REQUIRED" in result.stdout
    assert "--person_titles" in result.stdout
    assert "enrich-person" in result.stdout  # suggests enrichment alternative


def test_cli_search_companies_help():
    result = _run_cli(["search-companies", "--help"])
    assert "search-companies [options]" in result.stdout
    assert "--organization_locations" in result.stdout
    assert "Employee Range Format" in result.stdout


def test_cli_enrich_person_help():
    result = _run_cli(["enrich-person", "--help"])
    assert "enrich-person [options]" in result.stdout
    assert "--email" in result.stdout
    assert "--linkedin_url" in result.stdout
    assert "--reveal_personal_emails" in result.stdout


def test_cli_missing_api_key_errors():
    result = _run_cli(["search-people", "--person_titles", "Manager", "--q", "test"])
    assert result.returncode != 0
    assert "APOLLO_API_KEY" in (result.stderr + result.stdout)


def test_cli_invalid_command_errors():
    result = _run_cli(["invalid-command"], env={"APOLLO_API_KEY": "test-key"})
    assert result.returncode != 0
    assert "Invalid command" in (result.stderr + result.stdout)


def test_cli_search_people_requires_titles():
    result = _run_cli(
        ["search-people", "--person_locations", "Virginia"],
        env={"APOLLO_API_KEY": "test-key"},
    )
    assert result.returncode != 0
    out = result.stderr + result.stdout
    assert "person_titles is required" in out
    assert "enrich-person" in out


def test_cli_unrecognized_param_warning():
    # The param is unrecognized but person_titles is provided so the command
    # progresses to the API call (which then fails on bad key — we only
    # check that the warning appeared).
    result = _run_cli(
        [
            "search-people",
            "--person_titles", "Manager",
            "--person_location", "Virginia",
        ],
        env={"APOLLO_API_KEY": "test-key"},
        timeout=15,
    )
    out = result.stderr + result.stdout
    assert "Unrecognized parameter" in out
    assert "person_location" in out


def test_cli_org_jobs_requires_id():
    result = _run_cli(["org-jobs"], env={"APOLLO_API_KEY": "test-key"})
    assert result.returncode != 0
    assert "--id is required" in (result.stderr + result.stdout)


def test_cli_org_info_requires_id():
    result = _run_cli(["org-info"], env={"APOLLO_API_KEY": "test-key"})
    assert result.returncode != 0
    assert "--id is required" in (result.stderr + result.stdout)


def test_cli_invalid_json_errors():
    result = _run_cli(
        ["search-people", "--json", "invalid json"],
        env={"APOLLO_API_KEY": "test-key"},
    )
    assert result.returncode != 0
    assert "Error parsing JSON" in (result.stderr + result.stdout)


# ---- Regression: numeric coercion must not eat leading zeros ------------------


def test_parse_value_preserves_leading_zeros():
    """"01234" is an identifier (zip code, numeric ID), not the number 1234."""
    assert _parse_value("01234") == "01234"
    assert _parse_value("007") == "007"


def test_parse_value_still_coerces_canonical_integers():
    assert _parse_value("0") == 0
    assert _parse_value("42") == 42
    assert _parse_value("1000") == 1000


def test_parse_args_keeps_leading_zero_identifier():
    _cmd, params = parse_args(
        ["search-people", "--person_titles", "CTO", "--postal_code", "02134"]
    )
    assert params["postal_code"] == "02134"
