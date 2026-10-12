FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml .
COPY uv.lock .
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY . .
RUN chmod +x /app/scripts/startup.sh

CMD ["/app/scripts/startup.sh"]
