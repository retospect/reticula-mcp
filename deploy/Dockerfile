# syntax=docker/dockerfile:1
# The Reticula MCP server image. Two ways to fill it:
#
#   release (the default target): the pinned release from PyPI.
#       Build through compose.yaml; the build context is this directory.
#   source: a wheel built from the working tree.
#       Build through compose.dev.yaml; the build context is the repository root.
#
# Both targets share the runtime stage, so user, paths, entry point and health
# probe are identical. Only the origin of the package differs.

FROM python:3.13-slim AS runtime

# The model cache directory exists in the image and belongs to the service
# user. A named volume mounted there inherits that owner on first use;
# without it docker creates the mount point for root and the model download
# fails. fastembed keeps its models under the system temp directory unless
# FASTEMBED_CACHE_PATH says otherwise, so point it at the volume.
RUN useradd --create-home --uid 10001 reticula \
    && install -d -o reticula -g reticula \
        /home/reticula/.cache /home/reticula/.cache/fastembed
ENV FASTEMBED_CACHE_PATH=/home/reticula/.cache/fastembed \
    PYTHONUNBUFFERED=1

# Wait for the database before the server starts. Compose orders the first
# start with depends_on, but after a host reboot docker restarts both
# containers at once, and `serve` exits when the database refuses the
# connection. Waiting here keeps the restart counter at zero.
COPY --chmod=755 <<'EOF' /usr/local/bin/reticula-entrypoint
#!/bin/sh
set -eu
if [ "${1:-}" = "reticula-mcp" ] && [ "${2:-}" = "serve" ]; then
    python - <<'PY'
import os
import sys
import time

import psycopg

url = os.environ.get("RETICULA_DATABASE_URL", "")
limit = float(os.environ.get("RETICULA_DB_WAIT", "120"))
if not url:
    sys.exit(0)  # the server prints the proper error
deadline = time.monotonic() + limit
announced = False
while True:
    try:
        psycopg.connect(url, connect_timeout=3).close()
        break
    except psycopg.OperationalError:
        if time.monotonic() >= deadline:
            print(f"database not reachable after {limit:.0f}s", flush=True)
            sys.exit(1)
        if not announced:
            print("waiting for the database", flush=True)
            announced = True
        time.sleep(1)
PY
fi
exec "$@"
EOF

# Used by the compose healthcheck. The image has no curl.
COPY --chmod=755 <<'EOF' /usr/local/bin/reticula-healthcheck
#!/bin/sh
exec python -c "
import os, sys, urllib.request
port = os.environ.get('RETICULA_PORT') or '8787'
try:
    r = urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
"
EOF

WORKDIR /home/reticula
EXPOSE 8787
ENTRYPOINT ["reticula-entrypoint"]
# --migrate applies pending schema migrations before the server starts.
CMD ["reticula-mcp", "serve", "--migrate"]


# --- a wheel from the working tree (context: repository root) ----------------
FROM python:3.13-slim AS wheel
RUN pip install --no-cache-dir build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist .


FROM runtime AS source
RUN --mount=type=bind,from=wheel,source=/dist,target=/dist \
    pip install --no-cache-dir "$(ls /dist/*.whl)[fastembed]"
USER reticula


# --- the release from PyPI (context: this directory). Last, so it is the
# default target and `docker build deploy/` keeps meaning "the release". -------
FROM runtime AS release
ARG RETICULA_VERSION
RUN test -n "$RETICULA_VERSION" \
    && pip install --no-cache-dir "reticula-mcp[fastembed]==${RETICULA_VERSION}"
USER reticula
