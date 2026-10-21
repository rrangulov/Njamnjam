#!/usr/bin/env bash
# Pull the promo panel from GitHub and restart it. Run as root on prod.
set -euo pipefail

REPO=https://github.com/rrangulov/Njamnjam.git
BRANCH=promo
SRC=/opt/nyamnyam-promo-src
DST=/opt/nyamnyam-promo
DB=/var/lib/nyamnyam-promo/promo.db

if [ -d "$SRC/.git" ]; then
    git -C "$SRC" fetch --depth 1 origin "$BRANCH"
    git -C "$SRC" reset --hard "origin/$BRANCH"
else
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$SRC"
fi
echo "commit: $(git -C "$SRC" log -1 --format='%h %s')"

python3 -m py_compile "$SRC"/promo/*.py

old_req=$(sha256sum "$DST/requirements.txt" 2>/dev/null | cut -d' ' -f1 || true)
cp -r "$SRC"/promo/. "$DST"/
rm -rf "$DST/__pycache__"
new_req=$(sha256sum "$DST/requirements.txt" | cut -d' ' -f1)
if [ "$old_req" != "$new_req" ]; then
    systemd-run --scope -p MemoryMax=400M "$DST/venv/bin/pip" install -q -r "$DST/requirements.txt"
fi

if [ -f "$DB" ]; then
    cp "$DB" "$DB.bak-$(date +%Y%m%d-%H%M%S)"
    ls -1t "$DB".bak-* | tail -n +6 | xargs -r rm --   # keep the 5 newest copies
fi

if ! cmp -s "$DST/nyamnyam-promo.service" /etc/systemd/system/nyamnyam-promo.service; then
    cp "$DST/nyamnyam-promo.service" /etc/systemd/system/
    systemctl daemon-reload
fi
systemctl restart nyamnyam-promo
sleep 3
systemctl is-active nyamnyam-promo
curl -s -o /dev/null -w 'login page: %{http_code}\n' http://127.0.0.1:3003/login
