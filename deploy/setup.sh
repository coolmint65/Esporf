#!/usr/bin/env bash
# Esporf VPS provisioning script
# Run as root on a fresh Ubuntu 22.04+ / Debian 12+ VPS:
#   curl -fsSL <raw-url>/deploy/setup.sh | bash
# Or clone first and run:
#   sudo bash deploy/setup.sh

set -euo pipefail

INSTALL_DIR="/opt/esporf"
REPO_URL="https://github.com/coolmint65/Esporf.git"

echo "==> Updating system packages"
apt-get update -qq
apt-get upgrade -y -qq

echo "==> Installing Docker (if not present)"
if ! command -v docker &>/dev/null; then
    apt-get install -y -qq ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    # Detect distro (works for Ubuntu and Debian)
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
    echo "    Docker installed."
else
    echo "    Docker already installed, skipping."
fi

echo "==> Cloning Esporf"
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "    Repo already exists at $INSTALL_DIR, pulling latest"
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

echo "==> Setting up .env"
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    echo "    Created .env from template — edit it before starting:"
    echo "    nano $INSTALL_DIR/.env"
else
    echo "    .env already exists, keeping it."
fi

echo "==> Installing systemd service"
cp "$INSTALL_DIR/deploy/esporf.service" /etc/systemd/system/esporf.service
systemctl daemon-reload
systemctl enable esporf
echo "    Service enabled (will start on boot)."

echo "==> Installing esporf command"
cp "$INSTALL_DIR/deploy/esporf" /usr/local/bin/esporf
chmod +x /usr/local/bin/esporf
echo "    'esporf' command installed."

echo ""
echo "============================================"
echo "  Esporf is ready!"
echo "============================================"
echo ""
echo "  esporf start     Start the bot"
echo "  esporf stop      Stop the bot"
echo "  esporf restart   Restart the bot"
echo "  esporf status    Check if it's running"
echo "  esporf logs      Tail live logs"
echo "  esporf update    Pull latest code and restart"
echo "  esporf config    Edit .env settings"
echo ""
