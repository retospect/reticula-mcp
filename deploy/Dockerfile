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

FROM python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50 AS runtime

# The service user owns its home directory. The embedding model is not here:
# it is built into the image under /opt/reticula/models (the `model` stages
# below), owned by root, and the server never downloads anything.
RUN useradd --create-home --uid 10001 reticula \
    && install -d -o reticula -g reticula /home/reticula/.cache
ENV PYTHONUNBUFFERED=1

# Quiet logs: no progress bars, and nothing but errors from huggingface_hub.
ENV TQDM_DISABLE=1 \
    HF_HUB_VERBOSITY=error

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
FROM python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50 AS wheel
RUN pip install --no-cache-dir build==1.6.1
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist .


# Both targets install the dependencies from requirements.txt, an export of
# uv.lock with a hash for every file, and then the package itself without its
# dependencies; `pip check` fails the build if the two do not fit.
#
# The embedding model is part of the image. A `model` stage fetches it at a
# pinned revision and checks every file against a pinned sha256
# (fetch-model.py), using huggingface_hub from the dependencies. It depends on
# the requirements and the script, not on the package, so a change of the code
# does not fetch the model again. Each context has its own paths, so each
# target has its own stages; they differ only in those paths.
# RETICULA_EMBED_MODEL must be one of the models fetch-model.py lists; compose
# passes the value the server is told to use. The final images set
# FASTEMBED_CACHE_PATH to the model directory and HF_HUB_OFFLINE=1, so the
# server finds the model there and never looks for one on the network.


FROM runtime AS deps-source
COPY deploy/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

FROM deps-source AS model-source
ARG RETICULA_EMBED_MODEL=BAAI/bge-base-en-v1.5
COPY deploy/fetch-model.py /tmp/fetch-model.py
RUN python /tmp/fetch-model.py "$RETICULA_EMBED_MODEL" /opt/reticula/models

FROM deps-source AS source
COPY --from=model-source --chown=root:root /opt/reticula/models /opt/reticula/models
RUN --mount=type=bind,from=wheel,source=/dist,target=/dist \
    pip install --no-cache-dir --no-deps "$(ls /dist/*.whl)" \
    && pip check
ENV FASTEMBED_CACHE_PATH=/opt/reticula/models \
    HF_HUB_OFFLINE=1
USER reticula


# --- the release from PyPI (context: this directory). Last, so it is the
# default target and `docker build deploy/` keeps meaning "the release". -------
FROM runtime AS deps-release
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

FROM deps-release AS model-release
ARG RETICULA_EMBED_MODEL=BAAI/bge-base-en-v1.5
COPY fetch-model.py /tmp/fetch-model.py
RUN python /tmp/fetch-model.py "$RETICULA_EMBED_MODEL" /opt/reticula/models

FROM deps-release AS release
ARG RETICULA_VERSION
COPY --from=model-release --chown=root:root /opt/reticula/models /opt/reticula/models
RUN test -n "$RETICULA_VERSION" \
    && pip install --no-cache-dir --no-deps "reticula-mcp==${RETICULA_VERSION}" \
    && pip check
ENV FASTEMBED_CACHE_PATH=/opt/reticula/models \
    HF_HUB_OFFLINE=1
USER reticula
