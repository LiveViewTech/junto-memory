#!/usr/bin/env bash
#
# Install/refresh the junto-backup-verify monitor on sage.
# Idempotent: copies artifacts into place, reloads systemd, enables the timer.
# Run as a user with sudo on sage (the check + alert run as tlemmons).
#
# Alerts go to Home Assistant via the webhook in ~/.config/sage-diskwatch/config
# (HA_WEBHOOK) — shared with sage-diskwatch, not installed by this script.
#
# Ref: infra/sage/junto-backup/README.md
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SBIN=/usr/local/sbin
UNITDIR=/etc/systemd/system

echo "[deploy] installing check + alert scripts -> ${SBIN}/"
sudo install -m 0755 -o root -g root "${HERE}/junto-backup-verify.sh" "${SBIN}/junto-backup-verify.sh"
sudo install -m 0755 -o root -g root "${HERE}/junto-backup-verify-alert.sh" "${SBIN}/junto-backup-verify-alert.sh"

echo "[deploy] installing systemd units -> ${UNITDIR}"
for u in junto-backup-verify.service junto-backup-verify.timer junto-backup-verify-alert.service; do
    sudo install -m 0644 -o root -g root "${HERE}/${u}" "${UNITDIR}/${u}"
done

echo "[deploy] reloading systemd + enabling timer"
sudo systemctl daemon-reload
sudo systemctl enable --now junto-backup-verify.timer

echo "[deploy] alert-channel check"
if [ -f /home/tlemmons/.config/sage-diskwatch/config ] && grep -q '^HA_WEBHOOK=' /home/tlemmons/.config/sage-diskwatch/config; then
    echo "  ok: HA_WEBHOOK present (alert channel — HA webhook, tag=junto-backup)"
else
    echo "  !! HA_WEBHOOK missing in ~/.config/sage-diskwatch/config — alerts will be SKIPPED. Fix before relying on this monitor."
fi

echo "[deploy] status:"
systemctl status junto-backup-verify.timer --no-pager || true
echo
echo "[deploy] done. Dry-run the check now with:  sudo systemctl start junto-backup-verify.service && journalctl -u junto-backup-verify.service -n 40 --no-pager"
echo "[deploy] fail-test the alert path: add a drop-in with Environment=CHROMA_DIR=/tmp/empty, 'systemctl start junto-backup-verify.service' (fails), confirm the HA notification lands on the phone, then remove the drop-in + 'systemctl reset-failed'."
