# PermitFlow, the API and the staff screens in one image.
#
# One image serves both processes. The mock county SOAP service is the same code with a
# different command, because it is part of the demo environment and shipping it separately
# would mean two build pipelines for one repository.
#
# Multi-stage so the wheels are built with a compiler present and the runtime image does not
# carry one. psycopg[binary] and lxml both have wheels for this platform, so the build stage
# is mostly about keeping pip's cache and build deps out of the final layer.

FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim AS runtime

# The SLA clock casts timestamps to dates, so the container's calendar has to be the
# department's. Same reason docker-compose sets TZ on the database.
ENV TZ=America/New_York \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY --from=build /opt/venv /opt/venv

WORKDIR /app

# Only what the application reads at runtime. The tests, the docs, and the Java module are
# not in the image: a smaller image is a smaller thing to audit, and the corpus and the SQL
# are here because the AI layer reads one and the deploy applies the other.
COPY permitflow/ ./permitflow/
COPY corpus/ ./corpus/
COPY sql/ ./sql/
COPY scripts/ ./scripts/

# Runs as nobody. The application never writes to disk, so it needs nothing it owns.
RUN useradd --create-home --uid 10001 permitflow \
    && chown -R permitflow:permitflow /app
USER permitflow

EXPOSE 8000

# /health reports the database and the ordinance corpus separately, so a container that is
# up but cannot reach its database fails the check rather than serving errors.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health | grep -q '"status":"ok"' || exit 1

CMD ["uvicorn", "permitflow.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
