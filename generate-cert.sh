#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# PatchPilot — generate-cert.sh
# macOS (LibreSSL) and Linux (OpenSSL) compatible
# ─────────────────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CERTS_DIR="$SCRIPT_DIR/certs"
mkdir -p "$CERTS_DIR"

# Load .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

HOSTNAME="${PATCHPILOT_HOSTNAME:-patchpilot.lan}"
HOST_IP="${PATCHPILOT_IP:-$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")}"
DAYS="${CERT_DAYS:-3650}"

echo "──────────────────────────────────────────────"
echo "  PatchPilot certificate generator"
echo "  Hostname : $HOSTNAME"
echo "  IP       : $HOST_IP"
echo "  Valid    : $DAYS days"
echo "  Output   : $CERTS_DIR/"
echo "──────────────────────────────────────────────"

# ── Write a single self-contained openssl config ──────────────────────────────
# Using one config file avoids all LibreSSL vs OpenSSL flag differences
CFG="$CERTS_DIR/patchpilot-openssl.cnf"
cat > "$CFG" << SSLCNF
[ req ]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn
x509_extensions    = v3_ca

[ dn ]
CN = PatchPilot Local CA
O  = PatchPilot
C  = US

[ v3_ca ]
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints       = critical, CA:true
keyUsage               = critical, keyCertSign, cRLSign

[ server_cert ]
basicConstraints       = CA:FALSE
keyUsage               = critical, digitalSignature, keyEncipherment
extendedKeyUsage       = serverAuth
subjectAltName         = @alt_names

[ alt_names ]
DNS.1 = $HOSTNAME
DNS.2 = localhost
IP.1  = $HOST_IP
IP.2  = 127.0.0.1
SSLCNF

# ── Step 1: CA key + self-signed CA cert ──────────────────────────────────────
echo "[1/3] Generating CA key and certificate..."
openssl genrsa -out "$CERTS_DIR/patchpilot-ca.key" 4096 2>/dev/null
openssl req -new -x509 \
    -key     "$CERTS_DIR/patchpilot-ca.key" \
    -out     "$CERTS_DIR/patchpilot-ca.crt" \
    -days    "$DAYS" \
    -config  "$CFG" 2>/dev/null
echo "    CA cert: $CERTS_DIR/patchpilot-ca.crt"

# ── Step 2: Server key + CSR ──────────────────────────────────────────────────
echo "[2/3] Generating server key and CSR..."
openssl genrsa -out "$CERTS_DIR/patchpilot.key" 2048 2>/dev/null

# CSR uses minimal config — SANs are applied at signing time
openssl req -new \
    -key  "$CERTS_DIR/patchpilot.key" \
    -out  "$CERTS_DIR/patchpilot.csr" \
    -subj "/CN=$HOSTNAME/O=PatchPilot/C=US" 2>/dev/null
echo "    Server key: $CERTS_DIR/patchpilot.key"

# ── Step 3: Sign with CA, embedding SANs via -extensions ──────────────────────
# This form works on both macOS LibreSSL and Linux OpenSSL
echo "[3/3] Signing server certificate..."
openssl x509 -req \
    -in            "$CERTS_DIR/patchpilot.csr" \
    -CA            "$CERTS_DIR/patchpilot-ca.crt" \
    -CAkey         "$CERTS_DIR/patchpilot-ca.key" \
    -CAcreateserial \
    -out           "$CERTS_DIR/patchpilot.crt" \
    -days          "$DAYS" \
    -extfile       "$CFG" \
    -extensions    server_cert 2>/dev/null
echo "    Server cert: $CERTS_DIR/patchpilot.crt"

# Cleanup CSR (not needed after signing)
rm -f "$CERTS_DIR/patchpilot.csr"

# ── Verify ────────────────────────────────────────────────────────────────────
echo ""
echo "── Certificate details ───────────────────────"
openssl x509 -in "$CERTS_DIR/patchpilot.crt" -noout -subject -issuer -dates -ext subjectAltName 2>/dev/null \
    || openssl x509 -in "$CERTS_DIR/patchpilot.crt" -noout -subject -issuer -dates
echo ""
echo "✅  Done!  Files in certs/:"
ls -la "$CERTS_DIR/"
echo ""
echo "Next steps:"
echo "  1. docker compose down && docker compose up -d"
echo "  2. Install certs/patchpilot-ca.crt on your devices:"
echo "     macOS:  open certs/patchpilot-ca.crt  → Keychain → Always Trust"
echo "     iOS:    AirDrop certs/patchpilot-ca.crt → Settings → install → trust"
