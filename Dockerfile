# Built for Cloud Run, which scales to zero: the container starts on the
# first request and stops when the traffic does, so a mostly idle API
# costs nothing. Everything here is in service of starting fast.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements first, as their own layer: application code changes on
# every push and the dependency install should not.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engine/ ./engine/
COPY web/ ./web/
COPY tools/ ./tools/

# Cloud Run sends SIGTERM and gives the process time to finish in-flight
# requests; running uvicorn as PID 1 means it actually receives it.
# Cloud Run also chooses the port and passes it in, so it is never hardcoded.
ENV PORT=8080
EXPOSE 8080
# The schema is checked before uvicorn starts, and migrations are never
# applied here -- see scripts/start.sh and supabase/MIGRATIONS.md.
COPY supabase/ ./supabase/
COPY scripts/ ./scripts/
CMD ["./scripts/start.sh"]
