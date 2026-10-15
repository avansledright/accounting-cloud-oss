FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

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
