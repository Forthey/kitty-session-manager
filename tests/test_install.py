import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('install', Path(__file__).resolve().parents[1] / 'install.py')
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.home / 'config'), 'XDG_DATA_HOME': str(self.home / 'data')}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_dry_run_creates_nothing(self):
        with mock.patch('install.subprocess.run') as run:
            self.assertEqual(install.main(['--dry-run', '--yes']), 0)
            run.assert_not_called()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_missing_systemd_does_not_create_files(self):
        with mock.patch('install.subprocess.run', side_effect=OSError('systemd unavailable')):
            self.assertEqual(install.main(['--yes']), 1)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_foreign_command_is_preserved(self):
        path = self.home / '.local/bin/savesession'
        path.parent.mkdir(parents=True)
        path.write_text('mine')
        with mock.patch('install.subprocess.run'):
            self.assertEqual(install.main(['--yes']), 1)
        self.assertEqual(path.read_text(), 'mine')

if __name__ == '__main__':
    unittest.main()

class InstallationFlowTests(InstallerTests):
    def fake_command(self, args, **kwargs):
        import subprocess
        if '-m' in args and 'venv' in args:
            (Path(args[-1]) / 'bin').mkdir(parents=True)
            (Path(args[-1]) / 'bin/python').touch()
        return subprocess.CompletedProcess(args, 1 if 'is-enabled' in args or 'is-active' in args else 0, '', '')

    def test_install_preserves_config_and_reuses_previous_version(self):
        with mock.patch('install.subprocess.run', side_effect=self.fake_command):
            self.assertEqual(install.main(['--yes']), 0)
            config = self.home / 'config/kitty-session-manager/config.toml'
            config.write_text(config.read_text().replace('interval_seconds = 30', 'interval_seconds = 45'))
            self.assertEqual(install.main(['--yes']), 0)
            self.assertIn('interval_seconds = 45', config.read_text())
            self.assertIn('OnUnitActiveSec=45s', (self.home / 'config/systemd/user/kitty-savesession.timer').read_text())
            self.assertTrue((self.home / 'data/kitty-session-manager/previous').is_symlink())

    def test_install_failure_preserves_previous_commands(self):
        with mock.patch('install.subprocess.run', side_effect=self.fake_command):
            self.assertEqual(install.main(['--yes']), 0)
        command = self.home / '.local/bin/savesession'
        before = command.resolve()
        def fail(args, **kwargs):
            import subprocess
            if 'pip' in args:
                raise subprocess.CalledProcessError(1, args)
            return self.fake_command(args, **kwargs)
        with mock.patch('install.subprocess.run', side_effect=fail):
            self.assertEqual(install.main(['--yes']), 1)
        self.assertEqual(command.resolve(), before)

class ConfigValidationTests(InstallationFlowTests):
    def test_incomplete_config_does_not_change_existing_install(self):
        config = self.home / 'config/kitty-session-manager/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[autosave]\nenabled=true\ninterval_seconds=30\n[kitty]\nsocket_path="/tmp/x-{pid}"\n')
        with mock.patch('install.subprocess.run', side_effect=self.fake_command):
            self.assertEqual(install.main(['--yes']), 1)
        self.assertFalse((self.home / '.local/bin/savesession').exists())

class RollbackTests(InstallationFlowTests):
    def test_systemd_failure_restores_installed_files(self):
        with mock.patch('install.subprocess.run', side_effect=self.fake_command):
            self.assertEqual(install.main(['--yes']), 0)
        command = self.home / '.local/bin/savesession'
        old_target = command.resolve()
        timer = self.home / 'config/systemd/user/kitty-savesession.timer'
        old_timer = timer.read_bytes()
        failure = {'done': False}
        def fake(args, **kwargs):
            import subprocess
            if 'enable' in args and '--now' in args and not failure['done']:
                failure['done'] = True
                raise subprocess.CalledProcessError(1, args)
            return self.fake_command(args, **kwargs)
        with mock.patch('install.subprocess.run', side_effect=fake):
            self.assertEqual(install.main(['--yes']), 1)
        self.assertEqual(command.resolve(), old_target)
        self.assertEqual(timer.read_bytes(), old_timer)

class CancellationTests(InstallerTests):
    def test_cancel_reports_distinct_status(self):
        with mock.patch('builtins.input', return_value='n'):
            self.assertEqual(install.main([]), 2)
        self.assertEqual(list(self.home.iterdir()), [])
