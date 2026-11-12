#!/usr/bin/env bash
# Optional: run the Sonic bot server 24 hours a day on a small cloud server.
#
# 1. Create an Ubuntu 24.04 server (for example DigitalOcean, 1 GB). Pick a region that can
#    reach Binance (not the USA), for example Frankfurt or Singapore.
# 2. Open its console as root and run:
#      curl -fsSL https://raw.githubusercontent.com/yqwba0052-ship-it/sonic-bot/main/deploy/install_cloud.sh | bash
# 3. The script prints an https address and a token. Put them in the app's "Cloud" connection.
#
# What it sets up:
# - the bot in /opt/sonic as the unprivileged user "sonic", restarted by systemd if it stops;
# - Caddy in front with automatic HTTPS on <ip>.sslip.io (the token never travels in clear text);
# - a firewall that allows only SSH, HTTP (certificate check) and HTTPS.
# Running it again updates the bot to the latest code and keeps its data and token.
set -euo pipefail

REPO="https://github.com/yqwba0052-ship-it/sonic-bot"
HOME_DIR=/opt/sonic
APP_DIR="$HOME_DIR/app"
ENV_FILE="$APP_DIR/server/.env"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (sudo -i first)." >&2
  exit 1
fi

# Small servers (512 MB) need swap for pip and git; add 1 GB once.
if ! swapon --show | grep -q . && [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 1000000 ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3-venv git caddy ufw curl tesseract-ocr tesseract-ocr-ara

id -u sonic >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" sonic

if [ -d "$APP_DIR/.git" ]; then
  sudo -u sonic git -C "$APP_DIR" pull --ff-only
else
  sudo -u sonic git clone --depth 1 "$REPO" "$APP_DIR"
fi

sudo -u sonic python3 -m venv "$HOME_DIR/venv"
sudo -u sonic "$HOME_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u sonic "$HOME_DIR/venv/bin/pip" install -q fastapi "uvicorn[standard]" httpx

IP=$(curl -4 -fsS https://api.ipify.org || hostname -I | awk '{print $1}')
HOST="${IP//./-}.sslip.io"

touch "$ENV_FILE"
chown sonic:sonic "$ENV_FILE"
chmod 600 "$ENV_FILE"
grep -q '^SONIC_HOST=' "$ENV_FILE" || echo "SONIC_HOST=127.0.0.1" >> "$ENV_FILE"
sed -i '/^SONIC_PUBLIC_URL=/d' "$ENV_FILE"
echo "SONIC_PUBLIC_URL=https://$HOST" >> "$ENV_FILE"

cat > /etc/systemd/system/sonic.service <<UNIT
[Unit]
Description=Sonic trading bot server (paper/demo)
After=network-online.target
Wants=network-online.target

[Service]
User=sonic
WorkingDirectory=$APP_DIR/server
ExecStart=$HOME_DIR/venv/bin/python -m sonic
Restart=always
RestartSec=10
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
    reverse_proxy 127.0.0.1:8787
}
CADDY

ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

systemctl daemon-reload
systemctl enable --now sonic
systemctl restart sonic caddy

for _ in $(seq 1 30); do
  grep -q '^SONIC_TOKEN=' "$ENV_FILE" && break
  sleep 1
done

if curl -fsS -m 10 https://fapi.binance.com/fapi/v1/ping >/dev/null; then
  BINANCE="reachable"
else
  BINANCE="NOT reachable from this region: create the server in another region"
fi

echo
echo "Sonic bot is running (paper mode)."
echo "  Address for the app: https://$HOST"
echo "  Token for the app:   $(grep '^SONIC_TOKEN=' "$ENV_FILE" | cut -d= -f2)"
echo "  Binance:             $BINANCE"
echo "The first https visit can take a minute while the certificate is issued."
echo "The bot stays stopped until you press Start in the app."
echo "For a month-long run, also switch on 'resume after server restart' in the app's"
echo "Connection tab, and stop the bot on the laptop so only one bot trades."
