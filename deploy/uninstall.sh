#!/bin/sh
# Remove the GPIB-Remote server: stop and disable the service, delete /opt/gpibremote/app and
# the venv. Installed as /opt/gpibremote/uninstall.sh.
#
#   sudo /opt/gpibremote/uninstall.sh          keeps data, configuration and the built Python
#   sudo /opt/gpibremote/uninstall.sh --purge  also deletes /var/lib/gpibremote (device types and
#                                              instances history!) and /etc/gpibremote
set -eu

PURGE=0
case "${1:-}" in
    --purge) PURGE=1 ;;
    "") ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl stop gpibremote.service 2>/dev/null || true
    systemctl disable gpibremote.service 2>/dev/null || true
fi
rm -f /etc/systemd/system/gpibremote.service
command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload 2>/dev/null || true
rm -rf /opt/gpibremote/app /opt/gpibremote/app.new /opt/gpibremote/app.old /opt/gpibremote/venv \
    /opt/gpibremote/VERSION
echo "gpibremote: service removed; kept /opt/gpibremote/python"
if [ "$PURGE" -eq 1 ]; then
    rm -rf /var/lib/gpibremote /etc/gpibremote
    userdel gpibremote 2>/dev/null || true
    echo "gpibremote: deleted /var/lib/gpibremote and /etc/gpibremote"
else
    echo "gpibremote: kept /var/lib/gpibremote (data) and /etc/gpibremote (configuration)"
fi
rm -f /opt/gpibremote/uninstall.sh
