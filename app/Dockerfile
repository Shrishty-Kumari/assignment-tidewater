ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY app/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

FROM ${PYTHON_IMAGE}
ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="settle" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="https://github.com/paylane/settle"

# security updates for the base layer; no compilers, no shells tools, no curl
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 settle \
 && useradd --uid 10001 --gid settle --no-create-home --shell /usr/sbin/nologin settle

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app/settle ./settle
COPY app/bankmock ./bankmock
COPY app/gunicorn.conf.py ./gunicorn.conf.py
COPY migrations ./migrations

ENV PATH=/opt/venv/bin:$PATH \
    SETTLE_VERSION=${VERSION} \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MIGRATIONS_DIR=/app/migrations

# no secrets here: DATABASE_URL etc. are injected at runtime from a Secret
USER 10001:10001
EXPOSE 8000
# exec form: gunicorn is PID 1 and receives SIGTERM directly
CMD ["gunicorn", "--config", "/app/gunicorn.conf.py", "settle.api:app"]
