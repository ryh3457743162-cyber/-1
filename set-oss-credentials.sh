#!/usr/bin/env bash
# Run interactively on the server. Credential values are never echoed.
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo "$0" "$@"
fi

env_file=/etc/photo-ring.env
[[ -f "$env_file" ]] || { echo "Missing $env_file" >&2; exit 1; }
umask 077

read -r -s -p "OSS AccessKey ID (hidden): " oss_key_id </dev/tty
printf '\n' >/dev/tty
read -r -s -p "OSS AccessKey Secret (hidden): " oss_key_secret </dev/tty
printf '\n' >/dev/tty

if [[ -z "$oss_key_id" || -z "$oss_key_secret" ||
      ! "$oss_key_id" =~ ^[A-Za-z0-9_./+=-]+$ ||
      ! "$oss_key_secret" =~ ^[A-Za-z0-9_./+=-]+$ ]]; then
  echo "Credentials were empty or contained unsupported characters; nothing changed." >&2
  exit 1
fi

temp_file=$(mktemp /etc/photo-ring.env.XXXXXX)
trap 'rm -f "$temp_file"' EXIT
awk '!/^OSS_(REGION|BUCKET|ENDPOINT|ACCESS_KEY_ID|ACCESS_KEY_SECRET|UPLOAD_MODE|TEST_UPLOAD_ENABLED)=/' "$env_file" > "$temp_file"
printf '%s\n' \
  'OSS_REGION=cn-shanghai' \
  'OSS_BUCKET=fanfan-hanhan-photos-1061513880' \
  'OSS_ENDPOINT=https://oss-cn-shanghai-internal.aliyuncs.com' \
  "OSS_ACCESS_KEY_ID=$oss_key_id" \
  "OSS_ACCESS_KEY_SECRET=$oss_key_secret" \
  'OSS_UPLOAD_MODE=local' \
  'OSS_TEST_UPLOAD_ENABLED=1' >> "$temp_file"
chown root:root "$temp_file"
chmod 600 "$temp_file"
mv -f "$temp_file" "$env_file"
unset oss_key_id oss_key_secret
echo "OSS credentials saved. Production upload mode remains local."
