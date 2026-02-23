# Deploying Esporf 24/7

## VPS Recommendations

Any cheap Linux VPS will work — Esporf is lightweight (< 100 MB RAM, negligible CPU).

| Provider | Cheapest plan | Notes |
|----------|---------------|-------|
| **Hetzner** | ~$4/mo (CX22) | Best value, EU & US datacenters |
| **DigitalOcean** | $6/mo (Basic) | Simple UI, good docs |
| **Linode (Akamai)** | $5/mo (Nanode) | Solid, straightforward |
| **Vultr** | $5/mo (Cloud) | Many datacenter locations |
| **Oracle Cloud** | Free tier (ARM) | Free forever, 1 GB RAM is plenty |

Pick **Ubuntu 22.04+** or **Debian 12+** as the OS.

---

## Linux VPS (Docker — Recommended)

### Automated setup

SSH into your VPS and run:

```bash
git clone https://github.com/coolmint65/Esporf.git /opt/esporf
sudo bash /opt/esporf/deploy/setup.sh
```

This installs Docker, clones the repo, sets up the systemd service, and creates your `.env` file.

Then configure and start:

```bash
sudo nano /opt/esporf/.env          # fill in your API keys
sudo systemctl start esporf         # start the bot
```

### Manual setup

```bash
# 1. Install Docker
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker

# 2. Clone
git clone https://github.com/coolmint65/Esporf.git /opt/esporf
cd /opt/esporf

# 3. Configure
cp .env.example .env
nano .env

# 4. Start with Docker Compose
sudo docker compose up -d --build

# 5. (Optional) Install systemd service for auto-start on boot
sudo cp deploy/esporf.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now esporf
```

### Managing the service

```bash
sudo systemctl status esporf       # check status
sudo systemctl restart esporf      # restart (e.g. after config change)
sudo systemctl stop esporf         # stop

# Logs
sudo docker compose -f /opt/esporf/docker-compose.yml logs -f
sudo journalctl -u esporf -f
```

### Updating

```bash
cd /opt/esporf
git pull
sudo systemctl restart esporf      # rebuilds the container automatically
```

### Running the Discord bot instead

Edit `docker-compose.yml` and uncomment the `esporf-discord` service, then comment out or remove the default `esporf` service. Restart:

```bash
sudo systemctl restart esporf
```

---

## Windows 10/11

### 1. Clone and install

```
git clone https://github.com/coolmint65/Esporf.git C:\Esporf
cd C:\Esporf
pip install -e .
```

### 2. Configure

Copy your `.env` file into `C:\Esporf`.

### 3. Install as a startup task

Right-click `deploy\install-task.bat` and select **Run as administrator**. This registers Esporf to start automatically every time you log in.

### 4. Start it now

Either reboot/re-login, or run:
```
schtasks /run /tn "Esporf Bot"
```

### Managing on Windows

```
schtasks /run /tn "Esporf Bot"       :: start
schtasks /end /tn "Esporf Bot"       :: stop
schtasks /delete /tn "Esporf Bot" /f :: remove
```

You can also manage it in **Task Scheduler** (search for it in the Start menu, look for "Esporf Bot" in the task list).
