"""Exercise menu-generated Iran/Kharej configurations with two real relay processes."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'target/debug/stunnel-relay'
spec = importlib.util.spec_from_file_location('integration_manager', ROOT / 'scripts/manage.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            while True:
                data = self.request.recv(65536)
                if not data:
                    self.request.sendall(b'half-close-ok')
                    return
                self.request.sendall(data)
        except (ConnectionError, OSError):
            pass


@unittest.skipUnless(BINARY.exists(), 'Build the relay with cargo build --locked first')
class RelayIntegrationTests(unittest.TestCase):
    def test_two_kharej_routes_and_half_close_work_from_menu_configuration(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            destination = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Echo)
            destination.daemon_threads = True
            stack.callback(destination.server_close)
            thread = threading.Thread(target=destination.serve_forever, daemon=True)
            thread.start()
            stack.callback(destination.shutdown)
            destination_port = destination.server_address[1]
            reservations = [socket.socket() for _ in range(5)]
            for sock in reservations:
                sock.bind(('127.0.0.1', 0))
            ports = [sock.getsockname()[1] for sock in reservations]
            iran_routes, kharej_routes = [], []
            for index in range(2):
                iran_port, kharej_port = ports[index * 2:index * 2 + 2]
                answers = ['2', f'kharej-{index}', '127.0.0.1', str(kharej_port), '',
                           str(destination_port), '127.0.0.1', 'y']
                with patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
                    kharej_routes.append(manager.configure_route())
                answers = ['1', f'iran-{index}', '127.0.0.1', str(iran_port), '127.0.0.1', str(kharej_port), 'y']
                with patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
                    iran_routes.append(manager.configure_route())
            answers = ['2', 'deny-other-sources', '127.0.0.1', str(ports[4]), '',
                       str(destination_port), '192.0.2.1', 'y']
            with patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
                kharej_routes.append(manager.configure_route())
            for sock in reservations:
                sock.close()
            for role, routes in (('kharej', kharej_routes), ('iran', iran_routes)):
                config = Path(directory) / (role + '.json')
                config.write_text(json.dumps({'settings': {'drain_timeout_secs': 1}, 'routes': routes}))
                checked = subprocess.run([str(BINARY), '--check', '--config', str(config)], capture_output=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)
                log = stack.enter_context((Path(directory) / (role + '.log')).open('w+'))
                process = subprocess.Popen([str(BINARY), '--config', str(config)], stdout=log, stderr=log)
                stack.callback(self.stop_process, process)
                self.wait_for_listener(process, manager.split_address(routes[0]['listen'])[1])
            payload = bytes(range(256)) * 2048
            for index in range(2):
                with socket.create_connection(('127.0.0.1', ports[index * 2]), timeout=5) as client:
                    client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                    received = bytearray()
                    while True:
                        chunk = client.recv(65536)
                        if not chunk:
                            break
                        received.extend(chunk)
                self.assertEqual(received, payload + b'half-close-ok')
            # The Kharej endpoint must reject a different loopback source IP.
            with socket.socket() as client:
                client.settimeout(5)
                client.connect(('127.0.0.1', ports[4]))
                try:
                    client.sendall(b'not-allowed')
                    received = client.recv(64)
                except ConnectionResetError:
                    received = b''
                self.assertEqual(received, b'')

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def wait_for_listener(self, process, port):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), 'Relay exited before binding')
            try:
                with socket.create_connection(('127.0.0.1', int(port)), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.02)
        self.fail('Relay listener did not become ready')


if __name__ == '__main__':
    unittest.main()
