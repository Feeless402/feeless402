#!/usr/bin/env bash
# Nightly off-box backup of the Feeless402 wallets/ledger (and Vane DBs).
# Destination is read from /root/.nano-pay/backup-dest (NOT in the repo):
#   HOST=user@host  PORT=22  DIR=~/f402-backup
# The tarball is encrypted with the passphrase in /root/.nano-pay/backup-pass
# (chmod 600; Glenn keeps a copy off-server — without it a restore is impossible).
set -euo pipefail

CONF=/root/.nano-pay/backup-dest
PASS=/root/.nano-pay/backup-pass
[ -r "$PASS" ] || { echo "backup: missing $PASS" >&2; exit 1; }

STAMP=$(date -u +%Y%m%d)
OUT=/root/backups
mkdir -p "$OUT"

# Wallets + ledger (the irreplaceable part), plus Vane live DBs while they
# exist, plus the Sourcer's Desk blog console (posts/subscribers DB, smtp
# creds, editor+templates), its console password file, the nginx vhosts
# (none of these live in any repo), and the live sites themselves.
tar czf - \
  --exclude='sourcerdesk-blog/.venv' \
  -C /root .nano-pay vane/logs/live sourcerdesk-blog sourcersdesk-launch \
  -C /etc nginx/sites-available nginx/.sourcerdesk_htpasswd \
  -C /var/www sourcerdesk feeless402 2>/dev/null \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:$PASS" \
  > "$OUT/f402-$STAMP.tar.gz.enc"

# Keep 14 local snapshots
ls -t "$OUT"/f402-*.tar.gz.enc | tail -n +15 | xargs -r rm --

# Ship off-box only when a push destination is configured; a pull-only
# setup (e.g. laptop fetches over scp) just uses the local snapshots.
if [ -r "$CONF" ]; then
  # shellcheck disable=SC1090
  . "$CONF"
  rsync -a -e "ssh -p ${PORT:-22} -i /root/.ssh/f402_backup_ed25519 -o StrictHostKeyChecking=accept-new" \
    "$OUT"/f402-*.tar.gz.enc "$HOST:${DIR:-~/f402-backup}/"
  ssh -p "${PORT:-22}" -i /root/.ssh/f402_backup_ed25519 "$HOST" \
    "find ${DIR:-~/f402-backup} -name 'f402-*.tar.gz.enc' -mtime +30 -delete" || true
  echo "backup ok (shipped): f402-$STAMP.tar.gz.enc"
else
  echo "backup ok (local only, no $CONF): f402-$STAMP.tar.gz.enc"
fi
