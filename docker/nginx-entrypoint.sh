#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# PatchPilot — nginx entrypoint helper
# Runs as part of nginx:alpine's /docker-entrypoint.d/ sequence.
#
# Substitutes $NGINX_BACKEND_HOST into the nginx config template so the same
# image works for both Docker Compose (backend) and Kubernetes (patchpilot-backend).
# ─────────────────────────────────────────────────────────────────────────────
set -e

TMPL="/etc/nginx/conf.d/default.conf.tmpl"
DEST="/etc/nginx/conf.d/default.conf"

if [ -f "$TMPL" ]; then
    echo "[patchpilot] Rendering nginx config: NGINX_BACKEND_HOST=${NGINX_BACKEND_HOST:-backend}"
    envsubst '$NGINX_BACKEND_HOST' < "$TMPL" > "$DEST"
else
    echo "[patchpilot] WARNING: nginx template not found at $TMPL, using existing config"
fi
