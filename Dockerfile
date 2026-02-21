# Pin to bookworm (Debian 12 stable) — avoids trixie/testing apt slowness on ARM
FROM python:3.11-slim-bookworm

# ── System packages ───────────────────────────────────────────────────────────
# Use bookworm mirror directly and install only what's needed.
# postgresql-client-15 is pinned to match the postgres:15-alpine service.
# sshpass is in bookworm's non-free-firmware but the regular repo has it too.
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    sshpass \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY backend/ .

# ── Runtime directories ───────────────────────────────────────────────────────
RUN mkdir -p /ansible /backups

# ── Port ──────────────────────────────────────────────────────────────────────
EXPOSE 8000

# ── Entrypoint ────────────────────────────────────────────────────────────────
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
