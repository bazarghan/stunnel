#!/usr/bin/env python3
"""Interactive management for stunnel-relay; uses only Python's standard library."""
import argparse
import copy
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager

CONFIG_DIR = Path('/etc/stunnel-relay')
CONFIG = CONFIG_DIR / 'config.json'
BINARY = Path('/usr/local/bin/stunnel-relay')
COMMAND = Path('/usr/local/bin/stunnel')
LIB_DIR = Path('/usr/local/lib/stunnel-relay')
UNIT = Path('/etc/systemd/system/stunnel-relay.service')
SERVICE = 'stunnel-relay.service'


class ManagementError(Exception):
    pass


def run(args, check=True, quiet=False):
    result = subprocess.run([str(arg) for arg in args], text=True,
                            stdout=subprocess.PIPE if quiet else None,
                            stderr=subprocess.PIPE if quiet else None)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or '').strip()
        raise ManagementError(detail or 'Command failed: ' + ' '.join(map(str, args)))
    return result


def systemctl(*args, **kwargs):
    return run(['systemctl', *args, SERVICE], **kwargs)


def load_config():
    try:
        data = json.loads(CONFIG.read_text())
        if not isinstance(data, dict) or not isinstance(data.get('routes'), list):
            raise ValueError('expected a routes array')
        return data
    except (OSError, ValueError) as error:
        raise ManagementError('Cannot read configuration: ' + str(error)) from error


@contextmanager
def config_lock():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with (CONFIG_DIR / '.manage.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def write_config(data):
    fd, name = tempfile.mkstemp(prefix='.config-', suffix='.json', dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        if data['routes']:
            run([BINARY, '--check', '--config', name], quiet=True)
        os.replace(name, CONFIG)
    finally:
        Path(name).unlink(missing_ok=True)


def apply_change(transform):
    """Serialize edits, validate before replacement, and restore on service failure."""
    with config_lock():
        previous = load_config()
        updated = copy.deepcopy(previous)
        transform(updated)
        was_active = systemctl('is-active', check=False, quiet=True).returncode == 0
        was_enabled = systemctl('is-enabled', check=False, quiet=True).returncode == 0
        write_config(updated)
        try:
            if updated['routes']:
                systemctl('enable', quiet=True)
                systemctl('restart', quiet=True)
            else:
                systemctl('disable', '--now', quiet=True)
        except ManagementError as error:
            write_config(previous)
            systemctl('enable' if was_enabled else 'disable', check=False, quiet=True)
            restored = systemctl('restart' if was_active else 'stop', check=False, quiet=True)
            detail = 'Previous configuration restored.'
            if restored.returncode:
                detail += ' Service recovery failed; run stunnel logs.'
            raise ManagementError(str(error) + '\n' + detail) from error


def ask(label, default=None, validator=None):
    while True:
        value = input(label + (f' [{default}]' if default is not None else '') + ': ').strip()
        if not value and default is not None:
            value = str(default)
        try:
            return validator(value) if validator else value
        except ValueError as error:
            print('Invalid value: ' + str(error))


def valid_name(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise ValueError('use 1–64 letters, digits, dots, underscores or hyphens')
    return value


def valid_ip(value, target=False):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError('enter an IPv4 or IPv6 address') from error
    if ip.is_multicast or (target and ip.is_unspecified):
        raise ValueError('enter a usable unicast IP address')
    if getattr(ip, 'scope_id', None):
        raise ValueError('IPv6 scope identifiers are not supported')
    return str(ip)


def valid_port(value):
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise ValueError('port must be between 1 and 65535')
    return int(value)


def valid_allowlist(value):
    if not value:
        raise ValueError('enter at least one Iran server IP')
    return list(dict.fromkeys(valid_ip(part.strip(), target=True) for part in value.split(',')))


def address(ip, port):
    return f'[{ip}]:{port}' if ':' in ip else f'{ip}:{port}'


def split_address(value):
    ip, port = value.rsplit(':', 1)
    return ip.strip('[]'), port


def configure_route(existing=None, obfuscation='none'):
    existing = existing or {}
    if existing.get('obfuscation_role'):
        default_role = '2' if existing['obfuscation_role'] == 'server' else '1'
    else:
        default_role = '2' if existing.get('allowed_ips') else '1'
    print('\n1) Iran server    2) Kharej server')
    role = ask('This server', default_role,
               lambda value: value if value in ('1', '2') else invalid('choose 1 or 2'))
    name = ask('Tunnel name (e.g. sweden-vpn)', existing.get('name'), valid_name)
    old_listen = split_address(existing['listen']) if 'listen' in existing else (None, None)
    old_target = split_address(existing['target']) if 'target' in existing else (None, None)
    print('Use 0.0.0.0 to listen on all IPv4 interfaces, or :: for IPv6.')
    listen_ip = ask('Listen IP on this server', old_listen[0] or '0.0.0.0', valid_ip)
    if role == '1':
        listen_port = ask('Public entry port for your clients', old_listen[1], valid_port)
        target_ip = ask('Kharej server IP', old_target[0], lambda value: valid_ip(value, target=True))
        target_port = ask('Tunnel port on the Kharej server', old_target[1], valid_port)
        allowed_ips = []
    else:
        listen_port = ask('Tunnel port (must match Iran target port)', old_listen[1], valid_port)
        target_ip = ask('Destination service IP', old_target[0] or '127.0.0.1',
                        lambda value: valid_ip(value, target=True))
        target_port = ask('Destination service port (Xray, etc.)', old_target[1], valid_port)
        allowed_ips = ask('Allowed Iran server IPs (comma separated)',
                          ','.join(existing.get('allowed_ips', [])) or None, valid_allowlist)
    route = {'name': name, 'listen': address(listen_ip, listen_port),
             'target': address(target_ip, target_port)}
    if allowed_ips:
        route['allowed_ips'] = allowed_ips
    if obfuscation == 'iperf3':
        route['obfuscation_role'] = 'client' if role == '1' else 'server'
        print('iperf3 obfuscation enabled. Enable it on the matching server too.')
    print(f"\n{name}: {route['listen']} → {route['target']}")
    if allowed_ips:
        print('Allowed sources: ' + ', '.join(allowed_ips))
    print('Saving restarts the relay and may briefly interrupt existing connections.')
    return route if confirm('Save and start this tunnel?') else None


def invalid(message):
    raise ValueError(message)


def confirm(label):
    return input(label + ' [y/N]: ').strip().lower() in ('y', 'yes')


def list_tunnels():
    data = load_config()
    routes = data['routes']
    mode = data.get('settings', {}).get('obfuscation', 'none')
    if not routes:
        print('No tunnels configured. Choose Add tunnel to set up this server.')
    for index, route in enumerate(routes, 1):
        print(f"{index}) {route['name']}: {route['listen']} → {route['target']}")
        print('   Obfuscation: ' + mode)
        if route.get('allowed_ips'):
            print('   Allowed Iran IPs: ' + ', '.join(route['allowed_ips']))
    return routes


def select_route(name=None):
    routes = list_tunnels()
    if not routes:
        return None
    if name is not None:
        for route in routes:
            if route['name'] == name:
                return route
        raise ManagementError('Tunnel not found: ' + name)
    choice = ask('Tunnel number (0 cancels)', '0',
                 lambda value: int(value) if value.isdecimal() and 0 <= int(value) <= len(routes)
                 else invalid('choose a listed tunnel number'))
    return routes[choice - 1] if choice else None


def ensure_route_fits(data, route, replacing=None):
    new_ip, new_port = split_address(route['listen'])
    target_ip, target_port = split_address(route['target'])
    if new_port == target_port and (new_ip == target_ip or
            ipaddress.ip_address(new_ip).is_unspecified and ipaddress.ip_address(target_ip).is_loopback):
        raise ManagementError('A tunnel cannot forward back to its own listener.')
    for other in data['routes']:
        if replacing is not None and other['name'] == replacing:
            continue
        if other['name'] == route['name']:
            raise ManagementError('A tunnel with that name already exists.')
        old_ip, old_port = split_address(other['listen'])
        same_family = ipaddress.ip_address(new_ip).version == ipaddress.ip_address(old_ip).version
        wildcard = ipaddress.ip_address(new_ip).is_unspecified or ipaddress.ip_address(old_ip).is_unspecified
        # The relay explicitly uses IPv6-only sockets.
        if new_port == old_port and same_family and (wildcard or new_ip == old_ip):
            raise ManagementError('This listen port conflicts with tunnel ' + other['name'])


def add_tunnel():
    route = configure_route(obfuscation=load_config().get('settings', {}).get('obfuscation', 'none'))
    if route is None:
        return
    def transform(data):
        ensure_route_fits(data, route)
        data['routes'].append(route)
    apply_change(transform)
    print('Tunnel added and service started. Configure the matching tunnel on the other server.')
    print('Allow the entry/tunnel TCP port in your server or provider firewall if needed.')


def edit_tunnel(name=None):
    original = select_route(name)
    if original is None:
        return
    route = configure_route(original, obfuscation=load_config().get('settings', {}).get('obfuscation', 'none'))
    if route is None:
        return
    def transform(data):
        ensure_route_fits(data, route, replacing=original['name'])
        for index, current in enumerate(data['routes']):
            if current['name'] == original['name']:
                if current != original:
                    raise ManagementError('This tunnel changed in another session. Try again.')
                data['routes'][index] = route
                return
        raise ManagementError('This tunnel was removed in another session.')
    apply_change(transform)
    print('Tunnel updated.')


def remove_tunnel(name=None):
    route = select_route(name)
    if route is None or not confirm('Remove tunnel ' + route['name'] + '?'):
        return
    def transform(data):
        for current in data['routes']:
            if current['name'] == route['name']:
                if current != route:
                    raise ManagementError('This tunnel changed in another session. Try again.')
                data['routes'].remove(current)
                return
        raise ManagementError('This tunnel was already removed in another session.')
    apply_change(transform)
    print('Tunnel removed. The service stops automatically when no tunnels remain.')


def remove_all():
    if confirm('Remove ALL tunnels on this server?'):
        apply_change(lambda data: data.update(routes=[]))
        print('All tunnels removed; service stopped and disabled.')


def start():
    if not load_config()['routes']:
        raise ManagementError('Add a tunnel before starting the service.')
    run([BINARY, '--check', '--config', CONFIG], quiet=True)
    systemctl('enable', quiet=True)
    systemctl('start', quiet=True)
    print('Service started; enabled at boot.')


def restart():
    if not load_config()['routes']:
        raise ManagementError('No tunnels configured.')
    run([BINARY, '--check', '--config', CONFIG], quiet=True)
    systemctl('restart')
    print('Service restarted.')


def stop():
    systemctl('disable', '--now')
    print('Service stopped; automatic startup disabled.')


def status():
    list_tunnels()
    systemctl('status', '--no-pager', '--full', check=False)


def logs():
    run(['journalctl', '-u', SERVICE, '-n', '60', '--no-pager'], check=False)


def uninstall():
    print('This removes every tunnel, the relay service, its configuration and the stunnel menu on THIS server.')
    if input('Type UNINSTALL to confirm: ').strip() != 'UNINSTALL':
        print('Cancelled.')
        return False
    with config_lock():
        systemctl('disable', '--now', quiet=True)
        UNIT.unlink(missing_ok=True)
        run(['systemctl', 'daemon-reload'])
        systemctl('reset-failed', check=False, quiet=True)
        for path in (BINARY, COMMAND, LIB_DIR / 'manage.py', CONFIG):
            path.unlink(missing_ok=True)
        # Delete only files this application owns; preserve unrelated files.
        (CONFIG_DIR / '.manage.lock').unlink(missing_ok=True)
        for directory in (LIB_DIR, CONFIG_DIR):
            try:
                directory.rmdir()
            except OSError:
                pass
    print('stunnel-relay uninstalled from this server.')
    return True


def menu():
    actions = {'1': add_tunnel, '2': list_tunnels, '3': edit_tunnel, '4': remove_tunnel,
               '5': status, '6': start, '7': restart, '8': stop, '9': logs,
               '10': remove_all, '11': uninstall}
    while True:
        print('\n── stunnel ──')
        active = systemctl('is-active', check=False, quiet=True).returncode == 0
        print('Service: ' + ('running' if active else 'stopped'))
        print('1) Add tunnel (Iran / Kharej)\n2) List tunnels\n3) Edit tunnel\n4) Remove tunnel')
        print('5) Status\n6) Start\n7) Restart\n8) Stop\n9) Logs')
        print('10) Remove all tunnels\n11) Completely uninstall\n0) Exit')
        choice = input('Choose: ').strip()
        if choice == '0':
            return
        action = actions.get(choice)
        if action is None:
            print('Choose a listed option.')
            continue
        try:
            result = action()
            if choice == '11' and result:
                return
        except ManagementError as error:
            print('Error: ' + str(error), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description='Manage tunnels on this server. Run without arguments for the menu.')
    parser.add_argument('action', nargs='?', default='menu', choices=(
        'menu', 'add', 'list', 'edit', 'remove', 'remove-all', 'status',
        'start', 'stop', 'restart', 'logs', 'uninstall'))
    parser.add_argument('name', nargs='?', help='tunnel name for edit/remove')
    args = parser.parse_args()
    if args.name is not None and args.action not in ('edit', 'remove'):
        parser.error('a tunnel name is only accepted with edit or remove')
    if os.geteuid() != 0:
        parser.exit(1, 'Run as root: sudo stunnel\n')
    interactive = args.action in ('menu', 'add', 'edit', 'remove', 'remove-all', 'uninstall')
    if interactive and not sys.stdin.isatty():
        try:
            sys.stdin = open('/dev/tty')
        except OSError:
            parser.exit(1, 'This action requires an interactive terminal.\n')
    actions = {'menu': menu, 'add': add_tunnel, 'list': list_tunnels, 'edit': edit_tunnel,
               'remove': remove_tunnel, 'remove-all': remove_all, 'status': status,
               'start': start, 'stop': stop, 'restart': restart, 'logs': logs, 'uninstall': uninstall}
    try:
        if args.action in ('edit', 'remove'):
            actions[args.action](args.name)
        else:
            actions[args.action]()
    except (EOFError, KeyboardInterrupt):
        print('\nCancelled.')
    except (ManagementError, OSError) as error:
        parser.exit(1, 'Error: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
