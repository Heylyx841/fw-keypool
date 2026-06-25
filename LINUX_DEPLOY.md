# Linux deployment

This project can run on Linux without PowerShell or Windows binaries.

## Prepare

```bash
cd /opt/fw-keypool
python3 -m venv .venv
. .venv/bin/activate
pip install -e registrar
python -m playwright install --with-deps chromium
bash scripts/install_newapi_linux.sh
cp .env.example .env
cp pool-gateway/newapi.env.example pool-gateway/newapi.env
```

Edit `.env`, `emails.csv`, and `pool-gateway/newapi.env` before running registration or channel sync.

## Start locally

```bash
cd /opt/fw-keypool
. .venv/bin/activate
python start.py --skip-register --skip-sync --skip-sticky
```

This starts New API on `127.0.0.1:3000` using SQLite in `pool-gateway/one-api.db`.

## Optional systemd services

```bash
cp deploy/linux/fw-keypool-newapi.service /etc/systemd/system/
cp deploy/linux/fw-keypool-sticky.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now fw-keypool-newapi
```

The New API binary listens on all interfaces. The provided service inserts
iptables/ip6tables rules before startup so ports `3000` and `3001` are only
reachable from localhost; keep those rules unless a trusted reverse proxy is in
front of the service.

Only enable sticky after `data/keys.json` exists:

```bash
systemctl enable --now fw-keypool-sticky
```

Keep these services bound to localhost unless you intentionally put them behind a reverse proxy with authentication.
