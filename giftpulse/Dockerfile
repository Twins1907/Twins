FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY giftpulse ./giftpulse
RUN pip install --no-cache-dir ".[postgres]"

# No shell, no root. A process that routes trades has no business running as root.
RUN useradd --create-home --shell /usr/sbin/nologin giftpulse \
    && mkdir -p /data \
    && chown giftpulse:giftpulse /data
USER giftpulse

# Where a SQLite database lives if one is used. Mount a volume here, or point
# GIFTPULSE_DATABASE_URL at Postgres and ignore it.
VOLUME ["/data"]

EXPOSE 8080

# Liveness only: "the web process is accepting connections". Deliberately not a
# request to /api/health — that endpoint answers 503 when the *indexer* is stalled,
# and a stalled indexer is not fixed by killing the container. Wiring it in here
# would turn a degraded-but-serving deployment into a restart loop. Indexer health
# belongs to an external monitor polling /api/health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import socket;socket.create_connection(('127.0.0.1',8080),4).close()" \
    || exit 1

# Exec form, so giftpulse is PID 1 and receives SIGTERM directly — the shutdown
# handler needs the signal to land on the Python process, not on a shell.
CMD ["giftpulse", "all"]
