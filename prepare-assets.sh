#!/usr/bin/env bash
set -euo pipefail

BASE_URL='https://fanfanwithhanhan.hanbaobuyaocai.chatgpt.site'
mkdir -p public/photos uploads data

python3 - "$BASE_URL/ring" "public/index.html" <<'PY'
import sys, urllib.request
request = urllib.request.Request(sys.argv[1], headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(request, timeout=60) as response:
    data = response.read()
if len(data) < 10000:
    raise SystemExit(f"download too small: {sys.argv[1]}")
open(sys.argv[2], "wb").write(data)
PY

python3 - "$BASE_URL/api/photos" "data/photos.json" <<'PY'
import json, sys, urllib.request
request = urllib.request.Request(sys.argv[1], headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(request, timeout=60) as response:
    data = response.read()
json.loads(data)
open(sys.argv[2], "wb").write(data)
PY

for index in $(seq -w 1 31); do
  case "$index" in
    14|16|23) continue ;;
  esac
  python3 - "$BASE_URL/photos/photo-$index.jpg" "public/photos/photo-$index.jpg" <<'PY'
import sys, urllib.request
request = urllib.request.Request(sys.argv[1], headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(request, timeout=60) as response:
    data = response.read()
if len(data) < 1000:
    raise SystemExit(f"download too small: {sys.argv[1]}")
open(sys.argv[2], "wb").write(data)
PY
done

python3 - "$BASE_URL/api/images/1789639507780-5b71ee10-1877-4ab0-acae-24330da3b8c4.png" "uploads/1789639507780-5b71ee10-1877-4ab0-acae-24330da3b8c4.png" <<'PY'
import sys, urllib.request
request = urllib.request.Request(sys.argv[1], headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(request, timeout=60) as response:
    data = response.read()
if len(data) < 1000:
    raise SystemExit(f"download too small: {sys.argv[1]}")
open(sys.argv[2], "wb").write(data)
PY

echo "ASSETS_OK"
