#!/bin/bash
# restart.sh - Restart Boondock Edge services (from install.conf)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/load_install_config.sh
source "$SCRIPT_DIR/lib/load_install_config.sh"
load_install_config "$SCRIPT_DIR"

_restart_unit() {
    local unit="$1"
    echo "Restarting $unit..."
    sudo systemctl restart "$unit"
    if systemctl is-active --quiet "$unit"; then
        echo "✓ $unit is running"
        systemctl status "$unit" --no-pager -l | head -n 5
    else
        echo "✗ $unit failed to restart"
        systemctl status "$unit" --no-pager -l
        return 1
    fi
    echo ""
}

FAILED=0
_restart_unit "$SERVICE_NAME" || FAILED=1

if _is_truthy "$MANAGE_UDP_SERVICE" && [ -f "$SYSTEMD_DIR/$UDP_SERVICE_NAME" ]; then
    _restart_unit "$UDP_SERVICE_NAME" || FAILED=1
fi

exit "$FAILED"
