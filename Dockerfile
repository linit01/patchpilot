# Pin to bookworm (Debian 12 stable) — avoids trixie/testing apt slowness on ARM
FROM python:3.11-slim-bookworm

# ── System packages ───────────────────────────────────────────────────────────
# apt-get upgrade runs first to pick up all Debian security backports available
# for bookworm at build time. This addresses Category 2 CVEs where Debian HAS
# issued a patched package (gnutls28, tar, curl, sqlite3, coreutils):
#   CVE-2025-14831, CVE-2025-9820  — gnutls28
#   CVE-2025-45582                 — tar
#   CVE-2025-10966, CVE-2025-15079, CVE-2025-0725,
#   CVE-2024-2379,  CVE-2025-15224, CVE-2025-14017 — curl
#   CVE-2025-52099, CVE-2025-29088 — sqlite3
#   CVE-2025-5278                  — coreutils
#
# NOTE: 41 other CVEs (openssh, glibc, openldap, systemd, krb5, etc.) are
# Debian 'ignored' / won't-fix entries — they appear in scanners forever
# because Debian Security Team has explicitly decided not to patch them.
# Switching to Alpine is the only way to eliminate those from scan results.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        openssh-client \
        sshpass \
        postgresql-client \
        curl \
        ca-certificates \
        gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
         | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
         https://download.docker.com/linux/debian bookworm stable" \
         > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && KUBECTL_VERSION=$(curl -fsSL https://dl.k8s.io/release/stable.txt) \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
         -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python toolchain — pin patched versions before installing anything ────────
# Addresses all three PyPI CVEs:
#   CVE-2026-24049  wheel 0.45.1 → ≥0.46.2  (path traversal / chmod exploit)
#   CVE-2025-8869   pip 24.0    → ≥25.3     (symlink escape in tar fallback)
#   CVE-2026-1703   pip 24.0    → ≥26.0.1   (wheel path traversal via commonprefix)
# Pinning pip to the specific latest stable (26.0.1) rather than just "upgrade"
# so the version is deterministic and auditable.
RUN pip install --no-cache-dir \
        "pip==26.0.1" \
        "wheel>=0.46.2" \
        "setuptools"

# ── Application dependencies ──────────────────────────────────────────────────
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY backend/ .
COPY ansible/ /ansible-src/

# ── Runtime directories ───────────────────────────────────────────────────────
RUN mkdir -p /ansible /backups

# ── Port ──────────────────────────────────────────────────────────────────────
EXPOSE 8000

# ── Entrypoint ────────────────────────────────────────────────────────────────
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
