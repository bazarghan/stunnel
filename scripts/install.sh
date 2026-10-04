#!/bin/sh
# Installs only this application's binary, configuration, and systemd service.
set -eu
if [ "$(id -u)" -ne 0 ]; then echo "Run as root." >&2; exit 1; fi
if [ "$#" -ne 2 ]; then echo "Usage: install.sh RELEASE_BINARY CONFIG_JSON" >&2; exit 1; fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
service_file="$script_dir/../deploy/stunnel-relay.service"
"$1" --check --config "$2"
install -d -m 0755 /etc/stunnel-relay
install -o root -g root -m 0755 "$1" /usr/local/bin/stunnel-relay.new
mv /usr/local/bin/stunnel-relay.new /usr/local/bin/stunnel-relay
install -o root -g root -m 0644 "$2" /etc/stunnel-relay/config.json.new
mv /etc/stunnel-relay/config.json.new /etc/stunnel-relay/config.json
install -o root -g root -m 0644 "$service_file" /etc/systemd/system/stunnel-relay.service
systemctl daemon-reload
systemctl enable stunnel-relay.service
systemctl restart stunnel-relay.service
