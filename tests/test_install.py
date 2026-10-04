"""Run the actual bootstrap against isolated paths and simulated Linux/systemd."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.mockbin = self.root / 'mockbin'
        self.mockbin.mkdir()
        (self.root / 'run/systemd/system').mkdir(parents=True)
        (self.root / 'etc/systemd/system').mkdir(parents=True)
        (self.root / 'usr/local/bin').mkdir(parents=True)
        self.checkout = self.root / 'checkout'
        self.checkout.mkdir()
        (self.checkout / 'scripts').mkdir()
        (self.checkout / 'deploy').mkdir()
        shutil.copy(ROOT / 'scripts/manage.py', self.checkout / 'scripts/manage.py')
        shutil.copy(ROOT / 'deploy/stunnel-relay.service', self.checkout / 'deploy/stunnel-relay.service')
        script = (ROOT / 'install.sh').read_text()
        for path in ('/usr/local', '/etc/stunnel-relay', '/etc/systemd/system', '/run/systemd/system'):
            script = script.replace(path, str(self.root) + path)
        self.installer = self.checkout / 'install.sh'
        self.installer.write_text(script)
        self.state = self.root / 'state.json'
        self.state.write_text(json.dumps({'active': False, 'enabled': False, 'fail_restart': False, 'calls': []}))
        self.env = dict(os.environ, PATH=str(self.mockbin) + os.pathsep + os.environ['PATH'],
                        MOCK_STATE=str(self.state), MOCK_ROOT=str(self.root), FIXTURES=str(self.root / 'fixtures'))
        self.stub('id', 'print(0)')
        self.stub('uname', "import sys; print('Linux' if '-s' in sys.argv else 'x86_64')")
        self.stub('systemctl', '''import json, os, sys
from pathlib import Path
p = Path(os.environ['MOCK_STATE']); data = json.loads(p.read_text())
args = sys.argv[1:]; action = args[0]; status = 0
data['calls'].append(args)
if action == 'is-active': status = 0 if data['active'] else 3
elif action == 'is-enabled': status = 0 if data['enabled'] else 1
elif action == 'enable': data['enabled'] = True
elif action == 'disable':
    data['enabled'] = False
    if '--now' in args: data['active'] = False
elif action in ('start', 'restart'):
    if data['fail_restart']:
        data['fail_restart'] = False; data['active'] = False; status = 1
    else: data['active'] = True
elif action == 'stop': data['active'] = False
p.write_text(json.dumps(data)); sys.exit(status)
''')
        self.stub('sha256sum', '''import hashlib, sys
from pathlib import Path
line = Path(sys.argv[-1]).read_text().strip(); digest, name = line.split(None, 1)
sys.exit(0 if hashlib.sha256(Path(name.strip().lstrip('*')).read_bytes()).hexdigest() == digest else 1)
''')
        self.stub('curl', '''import os, shutil, sys
from pathlib import Path
args = sys.argv[1:]; dest = args[args.index('-o') + 1]
url = args[args.index('-o') - 1]
fixture = Path(os.environ['FIXTURES']) / url.rsplit('/', 1)[-1]
if not fixture.exists(): sys.exit(22)
shutil.copyfile(fixture, dest)
''')
        self.binary = self.root / 'relay'
        self.binary.write_text('#!' + sys.executable + '''
import json, sys
if '--version' in sys.argv: print('stunnel-relay 0.1.0')
else:
    data = json.load(open(sys.argv[-1]))
    assert data['routes'], 'no routes'
    assert len({r['name'] for r in data['routes']}) == len(data['routes'])
''')
        self.binary.chmod(0o755)
        self.config = self.root / 'input.json'
        self.config.write_text(json.dumps({'routes': [{'name': 'vpn', 'listen': '0.0.0.0:5500',
                                                      'target': '198.51.100.20:55000'}]}))
        self.stub('cargo', '''import os, shutil
from pathlib import Path
out = Path('target/release/stunnel-relay'); out.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(Path(os.environ['MOCK_ROOT']) / 'relay', out); out.chmod(0o755)
''')

    def stub(self, name, source):
        path = self.mockbin / name
        path.write_text('#!' + sys.executable + '\n' + source + '\n')
        path.chmod(0o755)

    def install(self, *args, success=True):
        result = subprocess.run(['bash', str(self.installer), '--no-menu', *map(str, args)],
                                env=self.env, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def state_data(self):
        return json.loads(self.state.read_text())

    def set_state(self, **changes):
        state = self.state_data(); state.update(changes)
        self.state.write_text(json.dumps(state))

    def prepare_release(self, bad_checksum=False):
        fixtures = Path(self.env['FIXTURES']); fixtures.mkdir(exist_ok=True)
        archive = fixtures / 'stunnel-relay-linux-x86_64.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            bundle.add(self.binary, arcname='stunnel-relay')
            bundle.add(self.checkout / 'scripts', arcname='scripts')
            bundle.add(self.checkout / 'deploy', arcname='deploy')
        digest = '0' * 64 if bad_checksum else hashlib.sha256(archive.read_bytes()).hexdigest()
        Path(str(archive) + '.sha256').write_text(f'{digest}  {archive.name}\n')

    def test_fresh_release_install_leaves_no_tunnels_and_opens_no_ports(self):
        self.prepare_release()
        self.install()
        data = json.loads((self.root / 'etc/stunnel-relay/config.json').read_text())
        self.assertEqual(data['routes'], [])
        self.assertTrue((self.root / 'usr/local/bin/stunnel').exists())
        self.assertTrue((self.root / 'usr/local/lib/stunnel-relay/manage.py').exists())
        self.assertFalse(self.state_data()['active'] or self.state_data()['enabled'])

    def test_local_install_and_reinstall_preserve_config_and_stopped_state(self):
        self.install('--binary', self.binary, '--config', self.config)
        self.assertTrue(self.state_data()['active'] and self.state_data()['enabled'])
        self.set_state(active=False, enabled=False)
        self.install('--binary', self.binary)
        self.assertEqual(json.loads((self.root / 'etc/stunnel-relay/config.json').read_text()),
                         json.loads(self.config.read_text()))
        self.assertFalse(self.state_data()['active'] or self.state_data()['enabled'])

    def test_failed_upgrade_restores_binary_config_and_service(self):
        self.install('--binary', self.binary, '--config', self.config)
        installed = self.root / 'usr/local/bin/stunnel-relay'
        original = installed.read_bytes()
        self.binary.write_text(self.binary.read_text() + '\n# updated binary\n')
        self.set_state(fail_restart=True)
        self.install('--binary', self.binary, '--config', self.config, success=False)
        self.assertEqual(installed.read_bytes(), original)
        self.assertTrue(self.state_data()['active'] and self.state_data()['enabled'])

    def test_checksum_failure_makes_no_installation(self):
        self.prepare_release(bad_checksum=True)
        self.install(success=False)
        self.assertFalse((self.root / 'usr/local/bin/stunnel').exists())
        self.assertFalse((self.root / 'etc/stunnel-relay/config.json').exists())

    def test_missing_explicit_release_does_not_fall_back_to_main(self):
        self.install('--version', 'v99.0.0', success=False)
        self.assertFalse((self.root / 'usr/local/bin/stunnel').exists())

    def test_missing_latest_release_builds_source_bundle(self):
        fixtures = Path(self.env['FIXTURES']); fixtures.mkdir()
        with tarfile.open(fixtures / 'main', 'w:gz') as bundle:
            bundle.add(self.checkout / 'scripts', arcname='stunnel-main/scripts')
            bundle.add(self.checkout / 'deploy', arcname='stunnel-main/deploy')
        self.install()
        self.assertEqual((self.root / 'usr/local/bin/stunnel-relay').read_bytes(), self.binary.read_bytes())

    def test_existing_other_stunnel_is_never_overwritten(self):
        path = self.root / 'usr/local/bin/stunnel'
        path.write_text('#!/bin/sh\necho other program\n')
        self.install('--binary', self.binary, success=False)
        self.assertIn('other program', path.read_text())


if __name__ == '__main__':
    unittest.main()
