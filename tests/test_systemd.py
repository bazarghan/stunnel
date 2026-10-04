"""Opt-in real Linux service test, intended only for disposable GitHub runners."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('systemd_manager', ROOT / 'scripts/manage.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)
ENABLED = (os.environ.get('STUNNEL_SYSTEMD_TEST') == '1' and sys.platform == 'linux'
           and os.geteuid() == 0 and Path('/run/systemd/system').is_dir())


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            self.request.sendall(data)


@unittest.skipUnless(ENABLED, 'Opt-in Linux/root systemd test on a disposable runner')
class SystemdTests(unittest.TestCase):
    def test_install_low_port_rollback_remove_and_uninstall(self):
        for path in (manager.BINARY, manager.COMMAND, manager.UNIT, manager.CONFIG_DIR, manager.LIB_DIR):
            self.assertFalse(path.exists(), 'Disposable-runner test refuses to replace existing files: ' + str(path))
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            destination = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Echo)
            destination.daemon_threads = True
            stack.callback(destination.server_close)
            thread = threading.Thread(target=destination.serve_forever, daemon=True)
            thread.start()
            stack.callback(destination.shutdown)
            config = Path(directory) / 'config.json'
            original = {'routes': [{'name': 'low-port', 'listen': '127.0.0.1:443',
                                   'target': '127.0.0.1:' + str(destination.server_address[1]),
                                   'allowed_ips': ['127.0.0.1']}]}
            config.write_text(json.dumps(original))
            installed = False
            try:
                result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--binary',
                                         str(ROOT / 'target/debug/stunnel-relay'), '--config',
                                         str(config), '--no-menu'], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                installed = True
                self.assertEqual(manager.CONFIG.stat().st_mode & 0o777, 0o600)
                self.check_echo(443)
                unit = subprocess.check_output(['systemctl', 'show', manager.SERVICE,
                                                '--property=DynamicUser', '--value'], text=True).strip()
                self.assertEqual(unit, 'yes')
                listed = subprocess.check_output([str(manager.COMMAND), 'list'], text=True)
                self.assertIn('low-port', listed)
                # A busy port must restore the previous configuration and working service.
                with socket.socket() as occupied:
                    occupied.bind(('127.0.0.1', 0))
                    occupied.listen()
                    port = occupied.getsockname()[1]
                    with self.assertRaises(manager.ManagementError):
                        manager.apply_change(lambda data: data['routes'].append({
                            'name': 'busy-port', 'listen': f'127.0.0.1:{port}',
                            'target': original['routes'][0]['target']}))
                self.assertEqual(manager.load_config(), original)
                self.check_echo(443)
                manager.apply_change(lambda data: data.update(routes=[]))
                self.assertNotEqual(manager.systemctl('is-active', check=False, quiet=True).returncode, 0)
                self.assertNotEqual(manager.systemctl('is-enabled', check=False, quiet=True).returncode, 0)
            finally:
                if installed:
                    with patch('builtins.input', return_value='UNINSTALL'), contextlib.redirect_stdout(io.StringIO()):
                        self.assertTrue(manager.uninstall())
            for path in (manager.BINARY, manager.COMMAND, manager.UNIT, manager.CONFIG_DIR, manager.LIB_DIR):
                self.assertFalse(path.exists(), str(path))

    @staticmethod
    def check_echo(port):
        with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
            client.sendall(b'systemd-credential-and-capability-ok')
            received = bytearray()
            while len(received) < len(b'systemd-credential-and-capability-ok'):
                chunk = client.recv(1024)
                if not chunk:
                    break
                received.extend(chunk)
            assert received == b'systemd-credential-and-capability-ok'


if __name__ == '__main__':
    unittest.main()
