FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
RUN python -m livekit.agents download-files
RUN chmod +x /app/scripts/startup.sh

CMD ["/app/scripts/startup.sh"]
