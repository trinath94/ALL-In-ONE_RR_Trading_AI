#!/usr/bin/env bash
# RR Trading Engine — Ubuntu 24.04 installer.
# Usage: sudo ./scripts/install.sh --domain trading.example.com [--email ops@example.com] [--self-signed]
set -euo pipefail
cd "$(dirname "$0")/.."

DOMAIN=""; EMAIL=""; SELF_SIGNED=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --self-signed) SELF_SIGNED=1; shift ;;
    *) echo "unknown arg $1"; exit 2 ;;
  esac
done
[[ -n "$DOMAIN" ]] || { echo "--domain is required"; exit 2; }
. /etc/os-release
[[ "${VERSION_ID:-}" == "24.04" ]] || echo "WARNING: tested on Ubuntu 24.04, found ${PRETTY_NAME:-unknown}"

echo "==> Installing Docker Engine + Compose plugin"
if ! command -v docker >/dev/null; then
  apt-get update
  apt-get install -y ca-certificates curl gnupg ufw openssl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

echo "==> Firewall: allow SSH, HTTP, HTTPS only"
ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "==> Generating .env with fresh secrets (existing .env is kept)"
if [[ ! -f .env ]]; then
  gen() { openssl rand -base64 48 | tr -d '\n/+=' | cut -c1-48; }
  FERNET=$(python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
  sed -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(gen)|" \
      -e "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$(gen)|" \
      -e "s|^RR_JWT_SECRET=.*|RR_JWT_SECRET=$(gen)|" \
      -e "s|^RR_ENCRYPTION_KEY=.*|RR_ENCRYPTION_KEY=${FERNET}|" \
      -e "s|^RR_BRIDGE_SHARED_SECRET=.*|RR_BRIDGE_SHARED_SECRET=$(gen)|" \
      -e "s|^RR_PUBLIC_HOST=.*|RR_PUBLIC_HOST=${DOMAIN}|" \
      -e "s|^RR_CORS_ORIGINS=.*|RR_CORS_ORIGINS=https://${DOMAIN}|" .env.example > .env
  chmod 600 .env
  echo "    .env created. Copy RR_BRIDGE_SHARED_SECRET to the Windows connector, then set RR_BRIDGE_URL and RR_ANTHROPIC_API_KEY."
fi
mkdir -p secrets nginx/certs nginx/acme backups
chmod 700 secrets backups

echo "==> TLS certificate"
if [[ ! -f nginx/certs/fullchain.pem ]]; then
  if [[ $SELF_SIGNED -eq 1 || -z "$EMAIL" ]]; then
    openssl req -x509 -nodes -newkey rsa:3072 -days 365 -subj "/CN=${DOMAIN}" \
      -addext "subjectAltName=DNS:${DOMAIN}" -keyout nginx/certs/privkey.pem -out nginx/certs/fullchain.pem
    echo "    Self-signed certificate created (replace with a CA-issued one for production)."
  else
    apt-get install -y certbot
    certbot certonly --standalone --non-interactive --agree-tos -m "$EMAIL" -d "$DOMAIN"
    ln -sf "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" nginx/certs/fullchain.pem
    ln -sf "/etc/letsencrypt/live/${DOMAIN}/privkey.pem" nginx/certs/privkey.pem
    echo "0 3 * * * root certbot renew --webroot -w $(pwd)/nginx/acme --quiet && docker compose -f $(pwd)/docker-compose.yml exec -T nginx nginx -s reload" > /etc/cron.d/rr-certbot
  fi
  chmod 600 nginx/certs/privkey.pem || true
fi

echo "==> Building and starting the stack"
docker compose build
docker compose up -d
./scripts/healthcheck.sh || true

echo "==> Daily backups at 02:15"
echo "15 2 * * * root $(pwd)/scripts/backup.sh >> /var/log/rr-backup.log 2>&1" > /etc/cron.d/rr-backup

cat <<EOF

Installation complete.
Create the first administrator:
  read -rs RR_ADMIN_PASSWORD; export RR_ADMIN_PASSWORD
  docker compose run --rm -e RR_ADMIN_PASSWORD api python -m app.cli create-admin --username admin
Then open https://${DOMAIN}, add the DEMO account (Settings), verify it, and run symbol discovery.
EOF
