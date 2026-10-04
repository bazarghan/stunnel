"""Exercise configuration transactions without touching host services or files."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('manager', ROOT / 'scripts/manage.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


def route(name='sweden', port=5500, ip='0.0.0.0'):
    return {'name': name, 'listen': manager.address(ip, port), 'target': '198.51.100.20:55000'}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.paths = {'CONFIG_DIR': root / 'etc', 'CONFIG': root / 'etc/config.json',
                      'BINARY': root / 'bin/stunnel-relay', 'COMMAND': root / 'bin/stunnel',
                      'LIB_DIR': root / 'lib', 'UNIT': root / 'unit/stunnel-relay.service'}
        for name, value in self.paths.items():
            patcher = patch.object(manager, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        manager.CONFIG_DIR.mkdir()
        self.initial = {'settings': {'max_connections': 512}, 'routes': [route()]}
        manager.CONFIG.write_text(json.dumps(self.initial))
        self.active = True
        self.enabled = True
        self.fail_restart = False
        self.calls = []
        patcher = patch.object(manager, 'systemctl', side_effect=self.systemctl)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(manager, 'run', side_effect=self.run_command)
        patcher.start()
        self.addCleanup(patcher.stop)

    def systemctl(self, *args, **kwargs):
        self.calls.append(args)
        status = 0
        if args[0] == 'is-active':
            status = 0 if self.active else 3
        elif args[0] == 'is-enabled':
            status = 0 if self.enabled else 1
        elif args[0] == 'restart' and self.fail_restart:
            self.fail_restart = False
            self.active = False
            raise manager.ManagementError('Port already in use')
        elif args[0] in ('start', 'restart'):
            self.active = True
        elif args[0] == 'stop':
            self.active = False
        elif args[0] == 'enable':
            self.enabled = True
        elif args[0] == 'disable':
            self.enabled = False
            if '--now' in args:
                self.active = False
        return subprocess.CompletedProcess(args, status, '', '')

    def run_command(self, args, **kwargs):
        if '--check' in args:
            data = json.loads(Path(args[-1]).read_text())
            self.assertTrue(data['routes'])
            self.assertEqual(len({r['name'] for r in data['routes']}), len(data['routes']))
        return subprocess.CompletedProcess(args, 0, '', '')

    def test_multiple_servers_and_settings_survive_edit_and_removal(self):
        manager.apply_change(lambda data: data['routes'].append(route('germany', 5501)))
        data = manager.load_config()
        self.assertEqual(len(data['routes']), 2)
        self.assertEqual(data['settings'], self.initial['settings'])
        manager.apply_change(lambda data: data['routes'][1].update(target='203.0.113.20:443'))
        manager.apply_change(lambda data: data['routes'].pop(0))
        self.assertEqual(manager.load_config()['routes'][0]['name'], 'germany')
        self.assertTrue(self.active and self.enabled)
        self.assertEqual(manager.CONFIG.stat().st_mode & 0o777, 0o600)

    def test_last_removal_stops_and_disables_service(self):
        manager.apply_change(lambda data: data.update(routes=[]))
        self.assertEqual(manager.load_config()['routes'], [])
        self.assertFalse(self.active or self.enabled)
        with self.assertRaises(manager.ManagementError):
            manager.start()

    def test_failed_restart_restores_config_and_running_state(self):
        self.fail_restart = True
        with self.assertRaisesRegex(manager.ManagementError, 'Previous configuration restored'):
            manager.apply_change(lambda data: data['routes'].append(route('germany', 5501)))
        self.assertEqual(manager.load_config(), self.initial)
        self.assertTrue(self.active and self.enabled)

    def test_failed_restart_preserves_previously_stopped_state(self):
        self.active = self.enabled = False
        self.fail_restart = True
        with self.assertRaises(manager.ManagementError):
            manager.apply_change(lambda data: data['routes'].append(route('germany', 5501)))
        self.assertEqual(manager.load_config(), self.initial)
        self.assertFalse(self.active or self.enabled)

    def test_validator_failure_never_replaces_configuration(self):
        with patch.object(manager, 'run', side_effect=manager.ManagementError('Invalid config')):
            with self.assertRaises(manager.ManagementError):
                manager.apply_change(lambda data: data['routes'].append(route('bad', 5501)))
        self.assertEqual(manager.load_config(), self.initial)
        self.assertTrue(self.active)
        self.assertFalse(list(manager.CONFIG_DIR.glob('.config-*')))

    def test_conflicts_wildcards_and_local_loops_rejected(self):
        for new in (route('sweden', 5501), route('germany', 5500, '127.0.0.1')):
            with self.assertRaises(manager.ManagementError):
                manager.ensure_route_fits(self.initial, new)
        local_loop = {'name': 'loop', 'listen': '0.0.0.0:443', 'target': '127.0.0.1:443'}
        with self.assertRaises(manager.ManagementError):
            manager.ensure_route_fits(self.initial, local_loop)
        manager.ensure_route_fits(self.initial, route('ipv6', 5500, '::'))
        manager.ensure_route_fits(self.initial, route(), replacing='sweden')

    def test_ipv6_ports_and_required_allowlist(self):
        self.assertEqual(manager.address('2001:db8::1', 443), '[2001:db8::1]:443')
        self.assertEqual(manager.split_address('[::]:443'), ('::', '443'))
        self.assertEqual(manager.valid_allowlist('192.0.2.1, 2001:db8::1,192.0.2.1'),
                         ['192.0.2.1', '2001:db8::1'])
        for port in ('0', '65536', '-1', 'abc'):
            with self.assertRaises(ValueError):
                manager.valid_port(port)
        for ips in ('', 'bad-ip', '0.0.0.0'):
            with self.assertRaises(ValueError):
                manager.valid_allowlist(ips)

    def test_iran_and_kharej_interactive_configuration(self):
        for answers, expected in (
            (['1', 'iran', '', '5500', '198.51.100.20', '55000', 'y'],
             {'name': 'iran', 'listen': '0.0.0.0:5500', 'target': '198.51.100.20:55000'}),
            (['2', 'kharej', '::', '55000', '::1', '55601', '192.0.2.10', 'y'],
             {'name': 'kharej', 'listen': '[::]:55000', 'target': '[::1]:55601',
              'allowed_ips': ['192.0.2.10']}),
        ):
            with patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(manager.configure_route(), expected)

    def test_stale_edit_does_not_overwrite_a_newer_change(self):
        original = route()
        manager.CONFIG.write_text(json.dumps({'routes': [dict(original, target='203.0.113.2:443')]}))
        with patch.object(manager, 'select_route', return_value=original), \
                patch.object(manager, 'configure_route', return_value=route('updated')):
            with self.assertRaisesRegex(manager.ManagementError, 'another session'):
                manager.edit_tunnel()
        self.assertEqual(manager.load_config()['routes'][0]['target'], '203.0.113.2:443')

    def test_uninstall_confirmation_and_complete_cleanup_preserve_other_files(self):
        for path in (manager.BINARY, manager.COMMAND, manager.UNIT, manager.LIB_DIR / 'manage.py'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('owned')
        unrelated = manager.CONFIG_DIR / 'keep-me.txt'
        unrelated.write_text('unrelated')
        with patch('builtins.input', return_value='no'), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(manager.uninstall())
        self.assertTrue(manager.BINARY.exists())
        with patch('builtins.input', return_value='UNINSTALL'), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(manager.uninstall())
        for path in (manager.BINARY, manager.COMMAND, manager.UNIT, manager.CONFIG, manager.LIB_DIR):
            self.assertFalse(path.exists(), path)
        self.assertTrue(unrelated.exists())
        self.assertFalse(self.active or self.enabled)


if __name__ == '__main__':
    unittest.main()
