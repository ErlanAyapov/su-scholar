#!/bin/sh
set -eu

python - <<'PY'
import os
import socket
import sys
import time
from urllib.parse import urlparse

WAIT_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "90"))


def wait_for_tcp(host: str, port: int, name: str) -> None:
    deadline = time.time() + WAIT_TIMEOUT
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                print(f"{name} is available at {host}:{port}", flush=True)
                return
        except OSError:
            time.sleep(1)
    print(f"Timed out waiting for {name} at {host}:{port}", file=sys.stderr, flush=True)
    sys.exit(1)


db_engine = (os.getenv("DB_ENGINE") or "").strip().lower()
db_host = (os.getenv("DB_HOST") or os.getenv("POSTGRES_HOST") or "db").strip()
db_port = int((os.getenv("DB_PORT") or os.getenv("POSTGRES_PORT") or "5432").strip())
if db_engine in {"postgres", "postgresql"} or os.getenv("POSTGRES_DB") or os.getenv("DATABASE_URL"):
    wait_for_tcp(db_host, db_port, "postgres")

broker_url = (os.getenv("CELERY_BROKER_URL") or "").strip()
if broker_url.startswith(("redis://", "rediss://")):
    parsed = urlparse(broker_url)
    wait_for_tcp(parsed.hostname or "redis", parsed.port or 6379, "redis")
PY

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    python manage.py migrate --noinput
fi

if [ "${COLLECTSTATIC:-0}" = "1" ]; then
    python manage.py collectstatic --noinput
fi

exec "$@"
