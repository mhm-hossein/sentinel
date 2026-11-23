# Deployment (Ubuntu + systemd)

Production deployment of the Sentinel Platform. See the top-level `README.md`
for the full architecture and environment-variable reference.

## Install

```bash
sudo useradd --system --create-home --home-dir /opt/sentinel sentinel   # optional dedicated user
sudo -u sentinel git clone <your-repo-url> /opt/sentinel
cd /opt/sentinel
sudo -u sentinel python3 -m venv .venv
sudo -u sentinel .venv/bin/pip install -r requirements.txt
sudo -u sentinel cp .env.example .env
sudo -u sentinel nano .env      # set SENTINEL_API_ID/HASH, CONTROL_BOT_TOKEN, SENTINEL_OWNER_ID
```

## Run as a service

```bash
sudo cp deploy/sentinel.service /etc/systemd/system/sentinel.service
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel     # start now + on every reboot
sudo systemctl status sentinel
journalctl -u sentinel -f                # live logs / startup diagnostics
```

- **Restart after crash** — `Restart=always`, `RestartSec=5`.
- **Restart after reboot** — `WantedBy=multi-user.target` + `systemctl enable`.
- **Graceful stop** — `SIGTERM` → `start.py` stops all workers, then exits;
  `TimeoutStopSec=30` bounds the wait.
- **UTF-8** — `PYTHONUTF8=1` guarantees the `✓/✗` diagnostics render on minimal
  locales.

## First boot

`python start.py` creates `data/central.db`, seeds the feature registry + RBAC
roles + default plan→feature mappings, bootstraps the owner, and prints a
diagnostic checklist ending in `🛰 Platform Ready`. If required env vars are
missing it prints a `✗` line and exits non-zero (systemd will retry).

## Backup

```bash
# Central DB (WAL — copy all three) + per-user tree.
systemctl stop sentinel
cp data/central.db data/central.db-wal data/central.db-shm /backups/ 2>/dev/null || true
tar czf /backups/sentinel-users-$(date +%F).tgz data/users
systemctl start sentinel
```

## Upgrade

```bash
cd /opt/sentinel
sudo systemctl stop sentinel
sudo -u sentinel git pull
sudo -u sentinel .venv/bin/pip install -r requirements.txt
sudo systemctl start sentinel   # additive migrations apply automatically on boot
```

Or simply re-run the installer — it stops, updates and restarts for you:

```bash
curl -fsSL https://raw.githubusercontent.com/mhm-hossein/sentinel/main/install.sh | sudo bash
```

## Health check (optional)

```bash
# Verify the process is up and the central DB is reachable.
systemctl is-active sentinel && \
  .venv/bin/python -c "import asyncio,os; \
from core.central import CentralDatabase; from sentinel.paths import central_db_path; \
print(asyncio.run((lambda: (lambda d: (d.connect().__await__(), print('DB OK')))(CentralDatabase(central_db_path())))()))" 2>/dev/null || echo "DB check skipped"
```
