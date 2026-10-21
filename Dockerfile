FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /bin/

WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY ads_actions ./ads_actions
RUN uv pip install --system --no-cache .

# Fail the build if the bundled guardrails don't load.
RUN python -c "from ads_actions.guardrails import Guardrails; Guardrails.load()"

EXPOSE 8080
CMD ["google-ads-actions"]
