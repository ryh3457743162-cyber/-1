#!/usr/bin/env bash
# Deploy code only. Existing data, uploads and /etc/photo-ring.env are untouched.
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi

for path in app.py oss_storage.py music_store.py requirements.txt public/index.html public/manage.html public/book-admin.html public/book-layouts.js public/music-admin.html public/music-player.js; do
  [[ -f "$path" ]] || { echo "Missing staged file: $path" >&2; exit 1; }
done

backup_dir="/root/photo-ring-backups"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$backup_dir/photo-ring-$stamp.tar.gz"
code_paths=(app.py oss_storage.py requirements.txt public)
had_music_store=0
if [[ -f /opt/photo-ring/music_store.py ]]; then
  code_paths+=(music_store.py)
  had_music_store=1
fi
tar -czf "$backup" -C /opt/photo-ring "${code_paths[@]}" data uploads
chmod 600 "$backup"
echo "Backup created: $backup"

rollback() {
  echo "Deployment failed; restoring code from $backup" >&2
  tar -xzf "$backup" -C /opt/photo-ring "${code_paths[@]}"
  if [[ "$had_music_store" -eq 0 ]]; then
    rm -f /opt/photo-ring/music_store.py
  fi
  chown -R photo-ring:photo-ring /opt/photo-ring/app.py /opt/photo-ring/oss_storage.py /opt/photo-ring/requirements.txt /opt/photo-ring/public
  if [[ "$had_music_store" -eq 1 ]]; then
    chown photo-ring:photo-ring /opt/photo-ring/music_store.py
  fi
  systemctl restart photo-ring
}
trap rollback ERR

/opt/photo-ring/venv/bin/pip install -r requirements.txt
install -o photo-ring -g photo-ring -m 0644 app.py oss_storage.py music_store.py requirements.txt /opt/photo-ring/
cp -a public/. /opt/photo-ring/public/
chown -R photo-ring:photo-ring /opt/photo-ring/public
systemctl restart photo-ring
ready=0
for attempt in {1..20}; do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8000/health >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" -eq 1 ]] || { echo "Health check failed after 20 attempts" >&2; exit 1; }
trap - ERR
echo "DEPLOYMENT_OK backup=$backup"
