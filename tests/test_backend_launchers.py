"""Run shell wrappers against a temporary argument recorder, never real services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows launcher wrappers')
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='backend launcher checks ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for filename in ('run.sh', 'run.ps1'):
            shutil.copyfile(ROOT / filename, self.root / filename)
        (self.root / 'start_backend.py').write_text(
            'import json, os, sys\nprint(json.dumps({"args": sys.argv[1:], "python": sys.executable}))\n'
            'sys.exit(int(os.environ.get("LAUNCHER_TEST_EXIT", "0")))\n', encoding='utf-8')
        self.env = dict(os.environ)
        for key in ('CROWDSIM_PYTHON', 'CROWDSIM_CONDA', 'CROWDSIM_CONDA_ENV'):
            self.env.pop(key, None)
        self.arguments = ['--check', '--restart-services', '--name', 'argument with spaces']
        self.python = Path(sys.executable).as_posix()

    def invoke(self, launcher):
        if launcher == 'run.ps1':
            shell = shutil.which('powershell')
            if not shell: self.skipTest('PowerShell unavailable')
            command = [shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(self.root / launcher)]
        else:
            shell = shutil.which('bash')
            if not shell: self.skipTest('Git Bash unavailable')
            command = [shell, '--noprofile', '--norc', str(self.root / launcher)]
        return subprocess.run([*command, *self.arguments], env=self.env, capture_output=True, text=True, timeout=20)

    def test_both_wrappers_use_literal_local_interpreter_and_forward_arguments(self):
        # Sourcing this file would execute touch. Credentials must remain data.
        (self.root / '.env').write_text(f'IGNORED=$(touch credential_was_executed)\nCROWDSIM_PYTHON="{self.python}"\n', encoding='utf-8')
        for launcher in ('run.sh', 'run.ps1'):
            with self.subTest(launcher=launcher):
                result = self.invoke(launcher)
                self.assertEqual(0, result.returncode, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(self.arguments, data['args'])
                self.assertEqual(Path(sys.executable), Path(data['python']))
                self.assertFalse((self.root / 'credential_was_executed').exists())

    def test_explicit_environment_overrides_local_config_and_preserves_exit_code(self):
        (self.root / '.env').write_text('CROWDSIM_PYTHON=missing-interpreter.exe\n', encoding='utf-8')
        self.env.update(CROWDSIM_PYTHON=self.python, LAUNCHER_TEST_EXIT='7')
        for launcher in ('run.sh', 'run.ps1'):
            with self.subTest(launcher=launcher):
                result = self.invoke(launcher)
                self.assertEqual(7, result.returncode, result.stderr)
                self.assertEqual(self.arguments, json.loads(result.stdout)['args'])


if __name__ == '__main__':
    unittest.main()
