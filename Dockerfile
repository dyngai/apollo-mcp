FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./

RUN uv sync --frozen --no-dev

COPY apollo_mcp/ apollo_mcp/

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

CMD ["uv", "run", "apollo-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8080", "--path", "/mcp/"]
