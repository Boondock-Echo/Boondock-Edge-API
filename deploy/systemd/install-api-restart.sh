#!/bin/bash
# One-time, root-run setup for the appliance's fixed API restart command.
set -euo pipefail

fail() { echo "Error: $*" >&2; exit 1; }
[[ $(id -u) == 0 ]] || fail "Run this setup as root."
[[ $# == 1 ]] || fail "Usage: $0 EXISTING_API_UNIT.service"
api_unit=$1
[[ $api_unit =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@:-]*\.service$ ]] || fail "Invalid API service unit name."

for dependency in systemctl visudo install mktemp sed id; do
    command -v "$dependency" >/dev/null || fail "Required command is missing: $dependency"
done
[[ $(systemctl show --property=LoadState --value "$api_unit") == loaded ]] || fail "API unit is not loaded: $api_unit"
[[ $(systemctl show --property=DynamicUser --value "$api_unit") == no ]] || fail "The API requires a persistent service account (DynamicUser=no)."
api_user=$(systemctl show --property=User --value "$api_unit")
[[ $api_user =~ ^[a-zA-Z_][a-zA-Z0-9_.-]*\$?$ ]] || fail "Configure a named, unprivileged User= account in $api_unit."
api_uid=$(id -u "$api_user") || fail "The API service account does not exist: $api_user"
[[ $api_uid != 0 ]] || fail "The API must not run as root."

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -f $script_dir/boondock-edge-api-restart ]] || fail "Missing restart helper beside the setup script."
[[ -f $script_dir/boondock-edge-api-restart.service.in ]] || fail "Missing restart unit template beside the setup script."
staging=$(mktemp -d)
trap 'rm -rf -- "$staging"' EXIT

sed "s/@API_UNIT@/$api_unit/g" "$script_dir/boondock-edge-api-restart.service.in" > "$staging/restart.service"
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/boondock-edge-api-restart ""\n' "$api_user" > "$staging/sudoers"
visudo -cf "$staging/sudoers"

install -d -o root -g root -m 0755 /usr/local/sbin /etc/systemd/system /etc/sudoers.d
install -o root -g root -m 0755 "$script_dir/boondock-edge-api-restart" /usr/local/sbin/boondock-edge-api-restart
install -o root -g root -m 0644 "$staging/restart.service" /etc/systemd/system/boondock-edge-api-restart.service
install -o root -g root -m 0440 "$staging/sudoers" /etc/sudoers.d/boondock-edge-api-restart
systemctl daemon-reload
echo "Installed API restart support for $api_unit (account: $api_user)."
