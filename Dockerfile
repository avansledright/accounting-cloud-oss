FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# tzdata so TZ is honoured: the slim image has no zoneinfo database, and
# without it any TZ other than UTC silently falls back to UTC. The 17:00
# price refresh, the report scheduler and every date.today() run on local
# time. Override TZ at runtime (e.g. TZ=America/New_York) for your zone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user. /app/data holds uploaded credit-card statements
# (CC_STATEMENT_DIR); mount a volume there to keep them.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data/cc_statements \
    && chown -R app:app /app/data
USER app

EXPOSE 5000

# One worker on purpose: the app keeps a price cache, report-job state and
# background scheduler threads in process memory. Threads give concurrency.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "120", "--access-logfile", "-", "app:app"]
