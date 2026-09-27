#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "请使用 root 执行此脚本。" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx python3 python3-venv python3-pip

id -u photo-ring >/dev/null 2>&1 || useradd --system --home /opt/photo-ring --shell /usr/sbin/nologin photo-ring
mkdir -p /opt/photo-ring /opt/photo-ring/data /opt/photo-ring/uploads
cp -a app.py oss_storage.py music_store.py requirements.txt public /opt/photo-ring/
if [[ ! -f /opt/photo-ring/data/photos.json ]]; then
  cp -a data/photos.json /opt/photo-ring/data/photos.json
fi
cp -a uploads/. /opt/photo-ring/uploads/

python3 -m venv /opt/photo-ring/venv
/opt/photo-ring/venv/bin/pip install --upgrade pip
/opt/photo-ring/venv/bin/pip install -r /opt/photo-ring/requirements.txt

if [[ ! -f /etc/photo-ring.env ]]; then
  ADMIN_PASSWORD="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(12))
PY
)"
  PASSWORD_HASH="$(/opt/photo-ring/venv/bin/python - "$ADMIN_PASSWORD" <<'PY'
import sys
from werkzeug.security import generate_password_hash
print(generate_password_hash(sys.argv[1]))
PY
)"
  SECRET="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"
  cat >/etc/photo-ring.env <<EOF
PHOTO_RING_SECRET=$SECRET
PHOTO_RING_PASSWORD_HASH=$PASSWORD_HASH
EOF
  chmod 600 /etc/photo-ring.env
  printf '%s\n' "$ADMIN_PASSWORD" >/root/photo-ring-admin-password.txt
  chmod 600 /root/photo-ring-admin-password.txt
fi

chown -R photo-ring:photo-ring /opt/photo-ring
chmod -R u=rwX,g=rX,o=rX /opt/photo-ring
chmod 750 /opt/photo-ring/data /opt/photo-ring/uploads

cp photo-ring.service /etc/systemd/system/photo-ring.service
cp nginx-photo-ring.conf /etc/nginx/sites-available/photo-ring
ln -sfn /etc/nginx/sites-available/photo-ring /etc/nginx/sites-enabled/photo-ring
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl daemon-reload
systemctl enable --now photo-ring nginx
systemctl restart photo-ring nginx

echo "DEPLOYMENT_OK"
echo "管理密码保存在 /root/photo-ring-admin-password.txt"
