"""Exercise menu-generated Iran/Kharej configurations with two real relay processes."""
import contextlib
import concurrent.futures
import importlib.util
import io
import json
from pathlib import Path
import socket
import socketserver
import struct
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
    def test_wire_matches_iperf3_control_and_data_layout_and_masks_payload(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            peer = stack.enter_context(socket.socket())
            peer.bind(('127.0.0.1', 0))
            peer.listen()
            peer.settimeout(5)
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                entry_port = reservation.getsockname()[1]
            config = Path(directory) / 'iran.json'
            config.write_text(json.dumps({'settings': {'obfuscation': 'iperf3', 'drain_timeout_secs': 1},
                                          'routes': [{'name': 'wire', 'listen': f'127.0.0.1:{entry_port}',
                                                      'target': f'127.0.0.1:{peer.getsockname()[1]}',
                                                      'obfuscation_role': 'client'}]}))
            log = stack.enter_context((Path(directory) / 'iran.log').open('w+'))
            process = subprocess.Popen([str(BINARY), '--config', str(config)], stdout=log, stderr=log)
            stack.callback(self.stop_process, process)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                self.assertIsNone(process.poll())
                try:
                    connected_client = stack.enter_context(socket.create_connection(
                        ('127.0.0.1', entry_port), timeout=5))
                    break
                except ConnectionRefusedError:
                    time.sleep(0.02)
            else:
                self.fail('Relay listener did not become ready')

            def exact(sock, size):
                data = bytearray()
                while len(data) < size:
                    chunk = sock.recv(size - len(data))
                    self.assertTrue(chunk, 'Unexpected EOF')
                    data.extend(chunk)
                return bytes(data)

            def read_json(sock):
                size = struct.unpack('!I', exact(sock, 4))[0]
                self.assertLessEqual(size, 4096)
                return json.loads(exact(sock, size))

            def fake_iperf_peer():
                with contextlib.ExitStack() as sockets:
                    control = sockets.enter_context(peer.accept()[0])
                    control.settimeout(5)
                    cookie = exact(control, 37)
                    self.assertEqual(cookie[-1:], b'\0')
                    self.assertTrue(all(byte in b'abcdefghijklmnopqrstuvwxyz234567' for byte in cookie[:-1]))
                    control.sendall(b'\x09')  # PARAM_EXCHANGE
                    parameters = read_json(control)
                    self.assertTrue(parameters['tcp'] and parameters['bidir'])
                    self.assertEqual(parameters['parallel'], 1)
                    control.sendall(b'\x0a')  # CREATE_STREAMS
                    streams = []
                    for _ in range(2):
                        stream = sockets.enter_context(peer.accept()[0])
                        stream.settimeout(5)
                        self.assertEqual(exact(stream, 37), cookie)
                        streams.append(stream)
                    control.sendall(b'\x01\x02')  # TEST_START, TEST_RUNNING
                    wire = bytearray()
                    while True:
                        chunk = streams[0].recv(65536)
                        if not chunk:
                            break
                        wire.extend(chunk)
                    streams[1].shutdown(socket.SHUT_WR)
                    self.assertEqual(exact(control, 1), b'\x04')  # TEST_END
                    control.sendall(b'\x0d')  # EXCHANGE_RESULTS
                    results = read_json(control)
                    self.assertEqual(results['streams'][0]['bytes'], len(wire))
                    self.assertEqual(results['streams'][1]['bytes'], 0)
                    encoded = json.dumps(results).encode()
                    control.sendall(struct.pack('!I', len(encoded)) + encoded + b'\x0e')
                    self.assertEqual(exact(control, 1), b'\x10')  # IPERF_DONE
                    return bytes(wire)

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                exchange = pool.submit(fake_iperf_peer)
                payload = b'GET /recognizable-header HTTP/1.1\r\nHost: example.test\r\n\r\n' * 4096
                with connected_client as client:
                    client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b'')
                wire = exchange.result(timeout=5)
            self.assertEqual(len(wire), len(payload))
            self.assertNotEqual(wire, payload)
            self.assertNotIn(b'GET /recognizable-header HTTP/1.1', wire)

    def test_two_kharej_routes_and_half_close_work_from_menu_configuration(self):
        self.exercise_routes('none')

    def test_obfuscated_routes_preserve_bytes_half_close_and_concurrent_sessions(self):
        self.exercise_routes('iperf3')

    def exercise_routes(self, obfuscation):
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
                    kharej_routes.append(manager.configure_route(obfuscation=obfuscation))
                answers = ['1', f'iran-{index}', '127.0.0.1', str(iran_port), '127.0.0.1', str(kharej_port), 'y']
                with patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
                    iran_routes.append(manager.configure_route(obfuscation=obfuscation))
            answers = ['2', 'deny-other-sources', '127.0.0.1', str(ports[4]), '',
                       str(destination_port), '192.0.2.1', 'y']
            with patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
                kharej_routes.append(manager.configure_route(obfuscation=obfuscation))
            for sock in reservations:
                sock.close()
            for role, routes in (('kharej', kharej_routes), ('iran', iran_routes)):
                config = Path(directory) / (role + '.json')
                config.write_text(json.dumps({'settings': {'drain_timeout_secs': 1,
                                                          'obfuscation': obfuscation}, 'routes': routes}))
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
            if obfuscation == 'iperf3':
                def exchange(index):
                    message = bytes([index]) * (131072 + index)
                    with socket.create_connection(('127.0.0.1', ports[(index % 2) * 2]), timeout=5) as client:
                        client.sendall(message)
                        client.shutdown(socket.SHUT_WR)
                        received = bytearray()
                        while True:
                            chunk = client.recv(65536)
                            if not chunk:
                                break
                            received.extend(chunk)
                    self.assertEqual(received, message + b'half-close-ok')
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(exchange, range(8)))
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
