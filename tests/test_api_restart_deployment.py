"""Run the real deployment scripts against isolated OS command adapters.

No host sudo rules, service files, or services are changed by these tests.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy/systemd'


class ApiRestartDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        shutil.copytree(DEPLOY, self.source)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.trace = self.root / 'commands.jsonl'
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'TEST_ROOT': str(self.root / 'installed'), 'TEST_TRACE': str(self.trace)}
        self.command('id', '''import os, sys
if len(sys.argv) == 2:
    print(os.environ.get('TEST_SETUP_UID', '0'))
elif os.environ.get('TEST_MISSING_ACCOUNT'):
    sys.exit(1)
else:
    print(os.environ.get('TEST_API_UID', '1001'))
''')
        self.command('systemctl', '''import json, os, sys
with open(os.environ['TEST_TRACE'], 'a') as stream:
    stream.write(json.dumps({'command': 'systemctl', 'args': sys.argv[1:]}) + '\\n')
if sys.argv[1] == 'show':
    values = {'LoadState': os.environ.get('TEST_LOAD_STATE', 'loaded'),
              'DynamicUser': os.environ.get('TEST_DYNAMIC_USER', 'no'),
              'User': os.environ.get('TEST_API_USER', 'boondock')}
    print(values[sys.argv[2].split('=', 1)[1]])
''')
        self.command('visudo', '''import json, os, pathlib, sys
with open(os.environ['TEST_TRACE'], 'a') as stream:
    stream.write(json.dumps({'command': 'visudo', 'rule': pathlib.Path(sys.argv[-1]).read_text()}) + '\\n')
sys.exit(int(os.environ.get('TEST_VISUDO_EXIT', '0')))
''')
        self.command('install', '''import json, os, pathlib, shutil, sys
args = sys.argv[1:]
directories = False
options = {}
paths = []
index = 0
while index < len(args):
    arg = args[index]
    if arg == '-d':
        directories = True
    elif arg in ('-o', '-g', '-m'):
        index += 1
        options[arg] = args[index]
    else:
        paths.append(arg)
    index += 1
with open(os.environ['TEST_TRACE'], 'a') as stream:
    stream.write(json.dumps({'command': 'install', 'options': options, 'paths': paths}) + '\\n')
def target(name):
    return pathlib.Path(os.environ['TEST_ROOT']) / name.lstrip('/')
if directories:
    for name in paths:
        target(name).mkdir(parents=True, exist_ok=True)
        target(name).chmod(int(options['-m'], 8))
else:
    destination = target(paths[-1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(paths[0], destination)
    destination.chmod(int(options['-m'], 8))
''')

    def command(self, name, source):
        script = self.bin / name
        script.write_text('#!/usr/bin/env python3\n' + source)
        script.chmod(0o755)

    def setup(self, unit='custom-api.service', **environment):
        return subprocess.run(['bash', str(self.source / 'install-api-restart.sh'), unit],
                              env={**self.env, **environment}, capture_output=True, text=True)

    def events(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def installed(self, name):
        return self.root / 'installed' / name.lstrip('/')

    def test_setup_delivers_real_helper_fixed_unit_and_narrow_sudo_permission(self):
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        helper = self.installed('/usr/local/sbin/boondock-edge-api-restart')
        unit = self.installed('/etc/systemd/system/boondock-edge-api-restart.service')
        sudoers = self.installed('/etc/sudoers.d/boondock-edge-api-restart')
        self.assertEqual(helper.read_bytes(), (DEPLOY / 'boondock-edge-api-restart').read_bytes())
        self.assertIn('ExecStart=/usr/bin/sleep 3\n', unit.read_text())
        self.assertIn('ExecStart=/usr/bin/systemctl restart custom-api.service\n', unit.read_text())
        self.assertNotIn('@API_UNIT@', unit.read_text())
        self.assertEqual(sudoers.read_text(), 'boondock ALL=(root) NOPASSWD: /usr/local/sbin/boondock-edge-api-restart ""\n')
        self.assertEqual([path.stat().st_mode & 0o777 for path in (helper, unit, sudoers)], [0o755, 0o644, 0o440])
        installs = [event for event in self.events() if event['command'] == 'install']
        self.assertTrue(all(event['options']['-o'] == 'root' and event['options']['-g'] == 'root' for event in installs))
        self.assertEqual(self.events()[-1], {'command': 'systemctl', 'args': ['daemon-reload']})
        self.assertFalse(any(event['command'] == 'systemctl' and 'restart' in event['args'] for event in self.events()))

    def test_invalid_sudo_rule_stops_before_installing_any_files(self):
        result = self.setup(TEST_VISUDO_EXIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(event['command'] == 'install' for event in self.events()))

    def test_rejects_root_dynamic_missing_or_unnamed_service_accounts(self):
        for environment in ({'TEST_API_USER': 'root', 'TEST_API_UID': '0'},
                            {'TEST_DYNAMIC_USER': 'yes'}, {'TEST_API_USER': ''},
                            {'TEST_MISSING_ACCOUNT': 'yes'}):
            with self.subTest(environment=environment):
                result = self.setup(**environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.installed('/usr/local/sbin/boondock-edge-api-restart').exists())

    def test_rejects_missing_unit_and_nonroot_setup(self):
        for environment in ({'TEST_LOAD_STATE': 'not-found'}, {'TEST_SETUP_UID': '1000'}):
            with self.subTest(environment=environment):
                self.assertNotEqual(self.setup(**environment).returncode, 0)

    def test_rejects_unit_name_command_and_systemd_specifier_injection(self):
        for name in ('--host=attacker', 'api.service\nExecStart=/bin/false', 'api%u.service', 'api.service;false'):
            with self.subTest(name=name):
                self.assertNotEqual(self.setup(unit=name).returncode, 0)
                self.assertFalse(self.installed('/etc/systemd/system/boondock-edge-api-restart.service').exists())

    def test_missing_companion_helper_is_an_error(self):
        (self.source / 'boondock-edge-api-restart').unlink()
        result = self.setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing restart helper', result.stderr)

    def run_helper(self, *arguments, uid='0'):
        # Substitute only the two absolute OS command paths, keeping the actual
        # helper logic unchanged. Production always uses its fixed absolute paths.
        source = (DEPLOY / 'boondock-edge-api-restart').read_text()
        source = source.replace('/usr/bin/id', str(self.bin / 'id')).replace('/usr/bin/systemctl', str(self.bin / 'systemctl'))
        helper = self.root / 'helper-under-test'
        helper.write_text(source)
        return subprocess.run(['sh', str(helper), *arguments], env={**self.env, 'TEST_SETUP_UID': uid},
                              capture_output=True, text=True)

    def test_helper_queues_only_the_fixed_unit_without_waiting(self):
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), [{'command': 'systemctl', 'args': ['start', '--no-block', 'boondock-edge-api-restart.service']}])

    def test_helper_rejects_arguments_and_unprivileged_direct_execution(self):
        self.assertEqual(self.run_helper('another.service').returncode, 64)
        self.assertEqual(self.run_helper('--command=/bin/false').returncode, 64)
        self.assertEqual(self.run_helper(uid='1000').returncode, 77)
        self.assertEqual(self.events(), [])

    def test_actual_release_archive_contains_working_restart_setup(self):
        workflow = (DEPLOY.parents[1] / '.github/workflows/Publish-Release.yml').read_text()
        step = workflow.split('      - name: Assemble API package\n', 1)[1].split('\n      - name:', 1)[0]
        block = step.split('        run: |\n', 1)[1]
        script = '\n'.join(line[10:] if line.startswith('          ') else line for line in block.splitlines())
        checkout = self.root / 'checkout'
        checkout.mkdir()
        (checkout / 'app').mkdir()
        (checkout / 'migrations').mkdir()
        shutil.copytree(DEPLOY, checkout / 'deploy/systemd')
        for filename in ('run.py', 'config.py', 'manage.py', 'requirements.txt', 'build-info.json', 'release_metadata.py'):
            (checkout / filename).write_text('{}\n')
        environment = {**self.env, 'TAG': 'beta', 'RUNNER_TEMP': str(self.root / 'runner'), 'GITHUB_ENV': str(self.root / 'outputs')}
        result = subprocess.run(['bash', '-c', script], cwd=checkout, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        extracted = self.root / 'extracted'
        with tarfile.open(checkout / 'boondock-edge-api-beta.tar.gz') as archive:
            names = archive.getnames()
            for filename in ('install-api-restart.sh', 'boondock-edge-api-restart', 'boondock-edge-api-restart.service.in'):
                self.assertIn('api/deploy/systemd/' + filename, names)
            archive.extractall(extracted, filter='data')
        self.source = extracted / 'api/deploy/systemd'
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('restart custom-api.service', self.installed('/etc/systemd/system/boondock-edge-api-restart.service').read_text())


if __name__ == '__main__':
    unittest.main()
