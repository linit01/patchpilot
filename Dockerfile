FROM python:3.11-slim

# ── System packages ──────────────────────────────────────────────────────────
# ansible removed from apt — installed via pip (ansible-runner/ansible-core)
# which avoids pulling gcc-14 + full compiler toolchain (~500MB on ARM).
# postgresql-client provides pg_dump, pg_restore, psql, dropdb, createdb.
RUN apt-get update && apt-get install -y \
    openssh-client \
    sshpass \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ──────────────────────────────────────────────────────
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ─────────────────────────────────────────────────────────
COPY backend/ .

# ── Runtime directories ──────────────────────────────────────────────────────
RUN mkdir -p /ansible /backups

# ── Port ─────────────────────────────────────────────────────────────────────
EXPOSE 8000

# ── Entrypoint ───────────────────────────────────────────────────────────────
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
