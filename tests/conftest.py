"""Shared pytest config — disable .env loading for the test suite.

The CLI and server modules call ``dotenv.load_dotenv()`` at import time;
in tests we want a clean environment so we monkeypatch it out before any
``apollo_mcp`` module loads.
"""

from __future__ import annotations

import dotenv

# No-op so importing apollo_mcp.cli / apollo_mcp.server during tests does
# not pull in a developer's local .env file.
dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]
