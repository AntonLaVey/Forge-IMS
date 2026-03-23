#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${APP_ROOT:-/opt/forge-ims}"
BACKEND_DIR="$APP_ROOT/backend"
FRONTEND_DIR="$APP_ROOT/frontend"
WEB_ROOT="${WEB_ROOT:-/var/www/forge-ims}"
VENV_DIR="$BACKEND_DIR/.venv"
SERVICE_NAME="${SERVICE_NAME:-forge-ims}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ROLLBACK_DIR="$APP_ROOT/.deploy-backups"
RELEASE_TAG="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$ROLLBACK_DIR" "$WEB_ROOT"

if [[ ! -d "$BACKEND_DIR/app" ]]; then
  echo "Backend directory not found: $BACKEND_DIR" >&2
  exit 1
fi

if [[ ! -f "$BACKEND_DIR/.env" ]]; then
  echo "Missing $BACKEND_DIR/.env. Copy .env.example and fill it out first." >&2
  exit 1
fi

if [[ -d "$WEB_ROOT" ]]; then
  rm -rf "$ROLLBACK_DIR/frontend-$RELEASE_TAG"
  mkdir -p "$ROLLBACK_DIR/frontend-$RELEASE_TAG"
  cp -a "$WEB_ROOT/." "$ROLLBACK_DIR/frontend-$RELEASE_TAG/" 2>/dev/null || true
fi

$PYTHON_BIN -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r "$APP_ROOT/backend/requirements.txt"

python -m compileall "$BACKEND_DIR/app"

rm -rf "$WEB_ROOT"
mkdir -p "$WEB_ROOT"
cp -a "$FRONTEND_DIR/." "$WEB_ROOT/"

sudo -n cp "$APP_ROOT/deploy/systemd/forge-ims.service" "/etc/systemd/system/$SERVICE_NAME.service"
sudo -n systemctl daemon-reload
sudo -n systemctl enable "$SERVICE_NAME"
sudo -n systemctl restart "$SERVICE_NAME"
sudo -n systemctl --no-pager --full status "$SERVICE_NAME"

echo "Deploy completed successfully."
