#!/usr/bin/env bash
# Public bootstrap: bash <(curl -fsSL https://raw.githubusercontent.com/bazarghan/stunnel/main/install.sh)
set -Eeuo pipefail

REPO=${STUNNEL_REPO:-bazarghan/stunnel}
REF=${STUNNEL_REF:-main}
VERSION=latest
NO_MENU=0
BINARY=
CONFIG=
SOURCE=0
OBFUSCATION=
SCRIPT_DIR=$(
    if cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null; then
        pwd
    fi
)

usage() {
    cat <<'HELP'
Usage: install.sh [--no-menu] [--version vX.Y.Z] [--source] [--obfuscation yes|no]
                  [--binary FILE --config FILE]
Installs the relay and the stunnel management menu on a Linux systemd server.
Run as root. STUNNEL_REPO and STUNNEL_REF override the GitHub source repository/ref.
Obfuscation defaults to no on fresh installations; updates preserve the current choice.
HELP
}
while (($#)); do
    case "$1" in
        --no-menu) NO_MENU=1; shift ;;
        --source) SOURCE=1; shift ;;
        --version|--binary|--config|--obfuscation)
            (($# >= 2)) || { usage >&2; exit 1; }
            case "$1" in
                --version) VERSION=$2 ;;
                --binary) BINARY=$2 ;;
                --config) CONFIG=$2 ;;
                --obfuscation)
                    [[ $2 == yes || $2 == no ]] || { echo '--obfuscation must be yes or no.' >&2; exit 1; }
                    OBFUSCATION=$2 ;;
            esac
            shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
done
[[ $(id -u) == 0 ]] || { echo 'Run this installer as root (sudo bash ...).' >&2; exit 1; }
[[ $(uname -s) == Linux && -d /run/systemd/system ]] || {
    echo 'This installer requires Linux with systemd running.' >&2; exit 1;
}
[[ $REPO =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo 'Invalid GitHub repository.' >&2; exit 1; }
case "$(uname -m)" in
    x86_64|amd64) ARCH=x86_64 ;;
    aarch64|arm64) ARCH=aarch64 ;;
    *) echo 'Supported architectures: x86_64 and arm64.' >&2; exit 1 ;;
esac

# Refuse to hide a different program behind the requested management command.
if [[ -e /usr/local/bin/stunnel || -L /usr/local/bin/stunnel ]]; then
    if ! grep -q '^# stunnel-relay management command$' /usr/local/bin/stunnel 2>/dev/null; then
        echo '/usr/local/bin/stunnel already belongs to another program; move it before installing.' >&2
        exit 1
    fi
fi
if [[ -z $BINARY ]] && command -v stunnel >/dev/null 2>&1 && [[ ! -f /usr/local/lib/stunnel-relay/manage.py ]]; then
    echo 'An existing stunnel TLS-wrapper command was found. Remove/rename it before installing this relay.' >&2
    exit 1
fi

install_packages() {
    local purpose=$1
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update
        if [[ $purpose == runtime ]]; then
            apt-get install -y ca-certificates curl python3 tar coreutils
        else
            apt-get install -y build-essential
        fi
    elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
        local pm
        pm=$(command -v dnf || command -v yum)
        if [[ $purpose == runtime ]]; then
            "$pm" install -y ca-certificates curl python3 tar coreutils
        else
            "$pm" install -y gcc make
        fi
    else
        echo 'Install curl, Python 3.8+, tar, coreutils (and a C compiler for source builds), then retry.' >&2
        exit 1
    fi
}
for dependency in curl python3 tar sha256sum install; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        install_packages runtime
        break
    fi
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo 'Python 3.8 or newer is required.' >&2; exit 1; }
WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT
fetch() { curl --fail --show-error --silent --location --retry 3 --connect-timeout 15 "$1" -o "$2"; }
# Ask only in interactive installations. No answer enables anything on a fresh install.
if [[ -z $OBFUSCATION && $NO_MENU == 0 && -r /dev/tty ]]; then
    DEFAULT_OBFUSCATION=no
    CURRENT_CONFIG=${CONFIG:-/etc/stunnel-relay/config.json}
    if [[ -f $CURRENT_CONFIG ]] && python3 - "$CURRENT_CONFIG" <<'PY'
import json, sys
sys.exit(json.load(open(sys.argv[1])).get('settings', {}).get('obfuscation') != 'iperf3')
PY
    then
        DEFAULT_OBFUSCATION=yes
    fi
    while true; do
        read -r -p "Enable iperf3 obfuscation? Both servers must match. [$DEFAULT_OBFUSCATION]: " ANSWER </dev/tty || ANSWER=
        case "$ANSWER" in
            '')
                if [[ ! -f $CURRENT_CONFIG ]]; then OBFUSCATION=no; fi
                break ;;
            [yY]|[yY][eE][sS]) OBFUSCATION=yes; break ;;
            [nN]|[nN][oO]) OBFUSCATION=no; break ;;
            *) echo 'Enter yes or no.' ;;
        esac
    done
fi
BUNDLE=
PREBUILT=0
build_source() {
    echo 'Building from source. The first installation may take several minutes.'
    SOURCE_REF=$REF
    [[ $VERSION == latest ]] || SOURCE_REF=$VERSION
    fetch "https://codeload.github.com/$REPO/tar.gz/$SOURCE_REF" "$WORK/source.tar.gz"
    mkdir "$WORK/source"
    tar -xzf "$WORK/source.tar.gz" -C "$WORK/source" --strip-components=1 --no-same-owner
    BUNDLE=$WORK/source
    if ! command -v cc >/dev/null 2>&1; then install_packages build; fi
    if ! command -v cargo >/dev/null 2>&1 || ! command -v rustup >/dev/null 2>&1; then
        fetch https://sh.rustup.rs "$WORK/rustup.sh"
        export RUSTUP_HOME="$WORK/rustup" CARGO_HOME="$WORK/cargo"
        bash "$WORK/rustup.sh" -y --profile minimal --default-toolchain none --no-modify-path
        export PATH="$CARGO_HOME/bin:$PATH"
    fi
    (cd "$BUNDLE" && cargo build --release --locked)
    BINARY=$BUNDLE/target/release/stunnel-relay
}
check_bundle() {
    [[ -f "$BUNDLE/scripts/manage.py" && -f "$BUNDLE/deploy/stunnel-relay.service" && -f $BINARY ]] || {
        echo 'The downloaded bundle is incomplete.' >&2; exit 1;
    }
    chmod 0755 "$BINARY"
    "$BINARY" --version
}
if [[ -n $BINARY ]]; then
    [[ -f $BINARY && -f "$SCRIPT_DIR/scripts/manage.py" ]] || {
        echo '--binary requires a local checkout/release bundle and an existing binary.' >&2; exit 1;
    }
    BINARY=$(readlink -f -- "$BINARY")
    BUNDLE=$SCRIPT_DIR
else
    ASSET="stunnel-relay-linux-$ARCH.tar.gz"
    if [[ $VERSION == latest ]]; then
        RELEASE_URL="https://github.com/$REPO/releases/latest/download"
    else
        [[ $VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] || { echo 'Use a release version such as v0.1.0.' >&2; exit 1; }
        RELEASE_URL="https://github.com/$REPO/releases/download/$VERSION"
    fi
    if [[ $SOURCE == 0 ]] && fetch "$RELEASE_URL/$ASSET" "$WORK/$ASSET"; then
        fetch "$RELEASE_URL/$ASSET.sha256" "$WORK/$ASSET.sha256"
        (cd "$WORK" && sha256sum --check "$ASSET.sha256")
        mkdir "$WORK/bundle"
        tar -xzf "$WORK/$ASSET" -C "$WORK/bundle" --no-same-owner
        BUNDLE=$WORK/bundle
        BINARY=$BUNDLE/stunnel-relay
        PREBUILT=1
    else
        if [[ $VERSION != latest && $SOURCE == 0 ]]; then
            echo "Release $VERSION could not be downloaded." >&2; exit 1
        fi
        build_source
    fi
fi
check_bundle
[[ -z $CONFIG || -f $CONFIG ]] || { echo 'The supplied configuration file does not exist.' >&2; exit 1; }
# Prepare the selected mode before touching installed files. Route roles use the
# same Iran/Kharej convention as the management menu; explicit roles win.
if [[ -n $OBFUSCATION ]]; then
    python3 - "${CONFIG:-/etc/stunnel-relay/config.json}" "$WORK/config.json" "$OBFUSCATION" <<'PY'
import json, pathlib, sys
source = pathlib.Path(sys.argv[1])
data = json.loads(source.read_text()) if source.exists() else {'settings': {}, 'routes': []}
if sys.argv[3] == 'yes':
    data.setdefault('settings', {})['obfuscation'] = 'iperf3'
    for route in data['routes']:
        route.setdefault('obfuscation_role', 'server' if route.get('allowed_ips') else 'client')
else:
    data.setdefault('settings', {}).pop('obfuscation', None)
    for route in data['routes']:
        route.pop('obfuscation_role', None)
pathlib.Path(sys.argv[2]).write_text(json.dumps(data, indent=2) + '\n')
PY
    CONFIG=$WORK/config.json
fi
# Latest releases may predate this feature. Probe a minimal valid configuration
# so unrelated configuration errors do not trigger an unnecessary source build.
SELECTED_CONFIG=${CONFIG:-/etc/stunnel-relay/config.json}
if [[ -f $SELECTED_CONFIG ]] && python3 - "$SELECTED_CONFIG" <<'PY'
import json, sys
sys.exit(json.load(open(sys.argv[1])).get('settings', {}).get('obfuscation') != 'iperf3')
PY
then
    cat > "$WORK/iperf3-check.json" <<'JSON'
{"settings":{"obfuscation":"iperf3"},"routes":[{"name":"probe","listen":"127.0.0.1:5500","target":"127.0.0.1:5501","obfuscation_role":"client"}]}
JSON
    if ! "$BINARY" --check --config "$WORK/iperf3-check.json" >/dev/null 2>&1; then
        if [[ $PREBUILT == 1 && $VERSION == latest ]]; then
            echo 'The latest prebuilt release lacks iperf3 support; building current source.'
            build_source
            check_bundle
        fi
        "$BINARY" --check --config "$WORK/iperf3-check.json" || {
            echo 'This binary does not support iperf3 obfuscation. Use current source or a newer release.' >&2
            exit 1
        }
    fi
fi
# Keep the current configuration on reinstall/update unless one was supplied explicitly.
if [[ -n $CONFIG ]]; then
    # Even an installation with no routes must check support for the new mode.
    python3 - "$CONFIG" "$WORK/check.json" <<'PY'
import json, pathlib, sys
data = json.loads(pathlib.Path(sys.argv[1]).read_text())
if not data['routes']:
    data['routes'] = [{'name': 'install-check', 'listen': '127.0.0.1:5500',
                      'target': '127.0.0.1:5501', 'obfuscation_role': 'client'}]
    if data.get('settings', {}).get('obfuscation') != 'iperf3':
        data['routes'][0].pop('obfuscation_role')
pathlib.Path(sys.argv[2]).write_text(json.dumps(data))
PY
    "$BINARY" --check --config "$WORK/check.json" || {
        echo 'Configuration check failed. For iperf3 mode, use a release that supports it or retry with --source.' >&2
        exit 1
    }
elif [[ -f /etc/stunnel-relay/config.json ]]; then
    if python3 -c 'import json, sys; sys.exit(bool(json.load(open("/etc/stunnel-relay/config.json"))["routes"]))' 2>/dev/null; then
        : # An installation with no tunnels remains stopped.
    else
        "$BINARY" --check --config /etc/stunnel-relay/config.json
    fi
fi

# Save the installed files and service state so a failed upgrade can be undone.
MANAGED_FILES=(/usr/local/bin/stunnel-relay /usr/local/bin/stunnel
    /usr/local/lib/stunnel-relay/manage.py /etc/systemd/system/stunnel-relay.service
    /etc/stunnel-relay/config.json)
mkdir "$WORK/backup"
for index in "${!MANAGED_FILES[@]}"; do
    if [[ -e ${MANAGED_FILES[$index]} || -L ${MANAGED_FILES[$index]} ]]; then
        cp -a -- "${MANAGED_FILES[$index]}" "$WORK/backup/$index"
    fi
done
HAD_SERVICE=0
WAS_ACTIVE=0
WAS_ENABLED=0
[[ ! -f /etc/systemd/system/stunnel-relay.service ]] || HAD_SERVICE=1
if systemctl is-active --quiet stunnel-relay.service; then WAS_ACTIVE=1; fi
if systemctl is-enabled --quiet stunnel-relay.service; then WAS_ENABLED=1; fi
rollback() {
    local result=$?
    trap - ERR
    echo 'Installation failed; restoring the previous installation.' >&2
    systemctl disable --now stunnel-relay.service >/dev/null 2>&1 || true
    for index in "${!MANAGED_FILES[@]}"; do
        rm -f -- "${MANAGED_FILES[$index]}.new"
        if [[ -e "$WORK/backup/$index" || -L "$WORK/backup/$index" ]]; then
            cp -a -- "$WORK/backup/$index" "${MANAGED_FILES[$index]}"
        else
            rm -f -- "${MANAGED_FILES[$index]}"
        fi
    done
    systemctl daemon-reload || true
    if [[ $WAS_ENABLED == 1 ]]; then systemctl enable stunnel-relay.service || true; fi
    if [[ $WAS_ACTIVE == 1 ]]; then
        systemctl start stunnel-relay.service || echo 'Service recovery failed; check journalctl -u stunnel-relay.' >&2
    fi
    exit "$result"
}
trap rollback ERR
install -d -m 0755 /usr/local/bin /usr/local/lib/stunnel-relay /etc/stunnel-relay /etc/systemd/system
install -m 0755 "$BINARY" /usr/local/bin/stunnel-relay.new
mv /usr/local/bin/stunnel-relay.new /usr/local/bin/stunnel-relay
install -m 0644 "$BUNDLE/scripts/manage.py" /usr/local/lib/stunnel-relay/manage.py.new
mv /usr/local/lib/stunnel-relay/manage.py.new /usr/local/lib/stunnel-relay/manage.py
cat > "$WORK/stunnel" <<'WRAPPER'
#!/usr/bin/env bash
# stunnel-relay management command
exec python3 /usr/local/lib/stunnel-relay/manage.py "$@"
WRAPPER
install -m 0755 "$WORK/stunnel" /usr/local/bin/stunnel
install -m 0644 "$BUNDLE/deploy/stunnel-relay.service" /etc/systemd/system/stunnel-relay.service
if [[ -n $CONFIG ]]; then
    install -m 0600 "$CONFIG" /etc/stunnel-relay/config.json.new
    mv /etc/stunnel-relay/config.json.new /etc/stunnel-relay/config.json
elif [[ ! -f /etc/stunnel-relay/config.json ]]; then
    printf '%s\n' '{"settings": {}, "routes": []}' > /etc/stunnel-relay/config.json
    chmod 0600 /etc/stunnel-relay/config.json
fi
systemctl daemon-reload
if python3 -c 'import json, sys; sys.exit(not bool(json.load(open("/etc/stunnel-relay/config.json"))["routes"]))'; then
    if [[ -n $CONFIG || $HAD_SERVICE == 0 ]]; then
        systemctl enable stunnel-relay.service
        systemctl restart stunnel-relay.service
    elif [[ $WAS_ACTIVE == 1 ]]; then
        systemctl restart stunnel-relay.service
    fi
fi
trap - ERR
echo 'Installed. Run: sudo stunnel'
if [[ $NO_MENU == 0 && -r /dev/tty ]]; then
    python3 /usr/local/lib/stunnel-relay/manage.py </dev/tty
fi
