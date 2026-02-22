# Deploying Esporf 24/7

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

---

## Linux VPS (DigitalOcean, Hetzner, etc.)

### 1. Clone and install

```bash
git clone https://github.com/coolmint65/Esporf.git /opt/esporf
cd /opt/esporf
python3 -m venv .venv
.venv/bin/pip install -e .
```

### 2. Configure

```bash
cp .env.example .env   # or copy your existing .env
nano .env              # fill in your API keys
```

### 3. Install the systemd service

```bash
sudo useradd -r -s /usr/sbin/nologin esporf
sudo chown -R esporf:esporf /opt/esporf
sudo cp deploy/esporf.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable esporf   # start on boot
sudo systemctl start esporf    # start now
```

### 4. Verify

```bash
sudo systemctl status esporf   # check it's running
sudo journalctl -u esporf -f   # live logs
```

## What you get

- **Auto-restart**: if the bot crashes, systemd restarts it in 10 seconds
- **Boot survival**: starts automatically after a server reboot
- **Logs**: `journalctl -u esporf` for full history

## Managing the service

```bash
sudo systemctl stop esporf      # stop
sudo systemctl restart esporf   # restart (e.g. after config change)
sudo systemctl status esporf    # check status
```
