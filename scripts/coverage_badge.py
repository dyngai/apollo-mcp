#!/usr/bin/env python3
"""Keep the README's numeric claims in sync with reality.

The test counts and coverage percentage used to be hand-typed, so they
silently drifted whenever the suite changed. This script makes them derived
data:

    python scripts/coverage_badge.py --write   # measure, then update README
    python scripts/coverage_badge.py --check   # fail if anything is stale

``--check`` is what CI runs, so stale numbers turn the build red instead of
quietly misreporting.

Coverage is measured over the **unit** suite only (``-m "not integration"``).
That is the figure anyone can reproduce without an Apollo API key, and it is
what CI can run. Test counts come from collection, so counting the integration
tests needs no key either.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

# shields.io's conventional thresholds, high to low.
_COLORS: list[tuple[int, str]] = [
    (90, "brightgreen"),
    (80, "green"),
    (70, "yellowgreen"),
    (60, "yellow"),
    (50, "orange"),
    (0, "red"),
]


def color_for(percent: int) -> str:
    for floor, color in _COLORS:
        if percent >= floor:
            return color
    return "red"


@dataclass(frozen=True)
class Metrics:
    unit: int
    integration: int
    coverage: int

    @property
    def total(self) -> int:
        return self.unit + self.integration


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def count_tests(marker: str) -> int:
    """Number of tests matching a marker expression, via collection only."""
    proc = _run(["-q", "--collect-only", "-m", marker])
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f"collection failed for marker {marker!r}")
    # With a marker filter pytest reports "255/288 tests collected (33
    # deselected)" — the selected count is the numerator. This form must be
    # tried first: the bare pattern would otherwise match the total after
    # the slash and silently report every test as selected.
    match = re.search(r"(\d+)/\d+\s+tests? collected", proc.stdout)
    if match is None:
        match = re.search(r"(\d+)\s+tests? collected", proc.stdout)
    if match is None:
        raise SystemExit(f"could not parse collected count for {marker!r}")
    return int(match.group(1))


def measure_coverage() -> int:
    """Run the unit suite under coverage and return the total percent."""
    proc = _run([
        "-q", "-m", "not integration",
        "--cov=apollo_mcp", "--cov-report=", "--no-header",
    ])
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit("tests failed; refusing to report a coverage number")
    return read_measured_total()


def read_measured_total() -> int:
    """Total percent from the coverage data file, floored to an int."""
    import coverage

    cov = coverage.Coverage(data_file=str(REPO_ROOT / ".coverage"))
    cov.load()
    total = cov.report(file=io.StringIO())
    # Floor rather than round: never claim coverage we do not have.
    return int(total)


def collect_metrics(*, no_run: bool) -> Metrics:
    unit = count_tests("not integration")
    integration = count_tests("integration")
    coverage = read_measured_total() if no_run else measure_coverage()
    return Metrics(unit=unit, integration=integration, coverage=coverage)


def substitutions(m: Metrics) -> list[tuple[re.Pattern[str], str, str]]:
    """(pattern, replacement, human label).

    Every pattern must have exactly three groups, and groups 1 and 3 must
    contain *all* the surrounding literal text — anything left outside a group
    is dropped on substitution.
    """
    return [
        (
            re.compile(r"(!\[Tests\]\(https://img\.shields\.io/badge/tests-)(\d+)(%20passing-)"),
            str(m.total),
            "tests badge",
        ),
        (
            re.compile(r"(!\[Coverage\]\(https://img\.shields\.io/badge/coverage-)(\d+)(%25-)"),
            str(m.coverage),
            "coverage badge",
        ),
        (
            re.compile(r"(\*\*)(\d+)( tests\*\* — )"),
            str(m.total),
            "highlights total",
        ),
        (
            re.compile(r"(— )(\d+)( unit plus )"),
            str(m.unit),
            "highlights unit count",
        ),
        (
            re.compile(r"( unit plus )(\d+)( live integration)"),
            str(m.integration),
            "highlights integration count",
        ),
        (
            re.compile(r"(\| Unit \| )(\d+)( \|)"),
            str(m.unit),
            "testing table unit",
        ),
        (
            re.compile(r"(\| Integration \| )(\d+)( \|)"),
            str(m.integration),
            "testing table integration",
        ),
        (
            re.compile(r"(\| \*\*Total\*\* \| \*\*)(\d+)(\*\* \|)"),
            str(m.total),
            "testing table total",
        ),
        (
            re.compile(r"(unit suite covers )(\d+)(% of)"),
            str(m.coverage),
            "testing prose coverage",
        ),
    ]


def apply(text: str, m: Metrics) -> tuple[str, list[str]]:
    """Rewrite every tracked number. Returns (new_text, stale_labels)."""
    stale: list[str] = []
    for pattern, value, label in substitutions(m):
        match = pattern.search(text)
        if match is None:
            raise SystemExit(f"README marker not found: {label} ({pattern.pattern})")
        # Guard against a pattern whose groups do not cover the whole match:
        # substituting one would silently delete the uncaptured text.
        if match.group(1) + match.group(2) + match.group(3) != match.group(0):
            raise SystemExit(
                f"pattern for {label!r} has text outside its groups; "
                f"groups 1 and 3 must cover the full match"
            )
        if match.group(2) != value:
            stale.append(f"{label}: README says {match.group(2)}, measured {value}")
        text = pattern.sub(lambda mo: f"{mo.group(1)}{value}{mo.group(3)}", text, count=1)

    # The coverage badge colour tracks the percentage.
    text = re.sub(
        r"(!\[Coverage\]\(https://img\.shields\.io/badge/coverage-\d+%25-)([a-zA-Z]+)(\.svg\))",
        lambda mo: f"{mo.group(1)}{color_for(m.coverage)}{mo.group(3)}",
        text,
        count=1,
    )
    return text, stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="update README.md")
    mode.add_argument("--check", action="store_true", help="exit non-zero if stale")
    parser.add_argument(
        "--no-run",
        action="store_true",
        help="reuse the existing .coverage data instead of re-running the suite",
    )
    args = parser.parse_args(argv)

    metrics = collect_metrics(no_run=args.no_run)
    original = README.read_text()
    updated, stale = apply(original, metrics)

    summary = (
        f"{metrics.unit} unit + {metrics.integration} integration "
        f"= {metrics.total} tests, {metrics.coverage}% coverage"
    )

    if args.check:
        if stale:
            print("README is out of date:", file=sys.stderr)
            for line in stale:
                print(f"  - {line}", file=sys.stderr)
            print(
                "\nRun: python scripts/coverage_badge.py --write",
                file=sys.stderr,
            )
            return 1
        print(f"README numbers are up to date ({summary})")
        return 0

    if updated == original:
        print(f"README already correct ({summary})")
        return 0
    README.write_text(updated)
    print(f"README updated -> {summary}")
    for line in stale:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
