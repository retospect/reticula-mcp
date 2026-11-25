# Runs a released reticula-mcp from PyPI. Build through compose.yaml.
FROM python:3.13-slim

ARG RETICULA_VERSION
RUN test -n "$RETICULA_VERSION" \
    && pip install --no-cache-dir "reticula-mcp[fastembed]==${RETICULA_VERSION}" \
    && useradd --create-home --uid 10001 reticula

USER reticula
WORKDIR /home/reticula
EXPOSE 8787
# --migrate applies pending schema migrations before the server starts.
CMD ["reticula-mcp", "serve", "--migrate"]
