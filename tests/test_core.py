import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import ksession

class CoreTests(unittest.TestCase):
    def test_config_requires_file(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'XDG_CONFIG_HOME': d}):
            with self.assertRaisesRegex(ValueError, 'config'):
                ksession.load_config()

    def test_transaction_rolls_back_on_rotation_failure(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'XDG_DATA_HOME': d}):
            folder = ksession.storage_dir()
            (folder / '1.json').write_text('old 1')
            (folder / '2.json').write_text('old 2')
            real_unlink = Path.unlink
            def unlink(path, *args, **kwargs):
                if path.name == '1.json':
                    raise OSError('injected')
                return real_unlink(path, *args, **kwargs)
            with patch.object(Path, 'unlink', unlink):
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    ksession.save_many([{'kitty_pid': 3}], 1)
            self.assertEqual((folder / '1.json').read_text(), 'old 1')
            self.assertEqual((folder / '2.json').read_text(), 'old 2')
            self.assertFalse((folder / '3.json').exists())

class CliTests(unittest.TestCase):
    def test_n_without_all_is_rejected(self):
        import subprocess, sys
        p = subprocess.run([sys.executable, 'savesession', '-n', '1'], capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('--all', p.stderr)

    def test_background_error_reports_and_suppresses_repeat_notification(self):
        import background
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'XDG_STATE_HOME': d}), patch.object(background, 'notify') as notify:
            background.failed('save', RuntimeError('failure'))
            background.failed('save', RuntimeError('failure'))
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(len(list((Path(d)/'kitty-session-manager'/'errors').glob('*.txt'))), 2)
            background.succeeded()
            background.failed('save', RuntimeError('failure'))
            self.assertEqual(notify.call_count, 2)

class RestoreTests(unittest.TestCase):
    def test_failure_rolls_back_new_window(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader('loadsession_test', 'loadsession')
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        session = {'os_windows': [{'tabs': [{'windows': [{}, {}]}]}]}
        def launch(sock, typ, cwd, title, match=None):
            if typ == 'window':
                raise RuntimeError('creation failed')
            return 42
        with patch.object(module, '_launch', side_effect=launch), patch.object(module, '_kitty') as kitty:
            with self.assertRaisesRegex(RuntimeError, 'creation failed'):
                module.restore_new_window('socket', session)
            self.assertTrue(any('close-tab' in call.args for call in kitty.call_args_list))

    def _load(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader('loadsession_test', 'loadsession')
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_focus_uses_window_id_match(self):
        module = self._load()
        session = {'os_windows': [{'tabs': [{'windows': [{}], 'is_active': True}]}]}
        with patch.object(module, '_launch', return_value=38), patch.object(module, '_kitty') as kitty:
            module.restore_new_window('socket', session)
        focus = [c.args for c in kitty.call_args_list if 'focus-window' in c.args]
        self.assertEqual(focus, [('socket', 'focus-window', '--match', 'id:38')])

    def test_focus_failure_keeps_restored_tabs(self):
        module = self._load()
        session = {'os_windows': [{'tabs': [{'windows': [{}], 'is_active': True}]}]}
        def kitty(sock, *args):
            if 'focus-window' in args:
                raise RuntimeError('focus failed')
            return ''
        with patch.object(module, '_launch', return_value=38), patch.object(module, '_kitty', side_effect=kitty) as k:
            self.assertEqual(module.restore_add('socket', session, 1), [38])
        self.assertFalse(any('close-tab' in c.args for c in k.call_args_list))

class IntegrationTests(unittest.TestCase):
    def test_all_rejects_over_limit_before_changing_snapshots(self):
        import subprocess, sys
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            conf = root/'config'/'kitty-session-manager'
            conf.mkdir(parents=True)
            template = (Path('config.toml').read_text().replace('/tmp/mykitty-{pid}', str(root/'sock-{pid}')))
            (conf/'config.toml').write_text(template)
            for pid in (1, 2, 3):
                (root/f'sock-{pid}').touch()
            env = dict(os.environ, XDG_CONFIG_HOME=str(root/'config'), XDG_DATA_HOME=str(root/'data'))
            p = subprocess.run([sys.executable, 'savesession', '--all'], env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('лимит', p.stderr)
            self.assertFalse((root/'data'/'kitty-sessions').exists())

    def test_all_failure_preserves_all_old_files(self):
        import subprocess, sys
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            conf = root/'config'/'kitty-session-manager'
            conf.mkdir(parents=True)
            fake = root/'kitty'
            fake.write_text('#!/bin/sh\ncase "$3" in *sock-2) exit 1;; esac\necho \'[{"tabs": [{}, {}]}]\'\n')
            fake.chmod(0o755)
            template = Path('config.toml').read_text().replace('/tmp/mykitty-{pid}', str(root/'sock-{pid}')).replace('executable = ""', f'executable = "{fake}"')
            (conf/'config.toml').write_text(template)
            for pid in (1, 2):
                (root/f'sock-{pid}').touch()
            storage = root/'data'/'kitty-sessions'
            storage.mkdir(parents=True)
            (storage/'1.json').write_text('old')
            env = dict(os.environ, XDG_CONFIG_HOME=str(root/'config'), XDG_DATA_HOME=str(root/'data'))
            p = subprocess.run([sys.executable, 'savesession', '--all'], env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual((storage/'1.json').read_text(), 'old')
            self.assertFalse((storage/'2.json').exists())

class MoreTests(unittest.TestCase):
    def test_config_validation_and_custom_socket(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'XDG_CONFIG_HOME': d}):
            folder = Path(d)/'kitty-session-manager'
            folder.mkdir()
            contents = Path('config.toml').read_text()
            (folder/'config.toml').write_text(contents.replace('/tmp/mykitty-{pid}', '/tmp/prefix-{pid}.sock'))
            config = ksession.load_config()
            ksession.configure(config)
            self.assertEqual(ksession.socket_for_pid(42), '/tmp/prefix-42.sock')
            (folder/'config.toml').write_text(contents.replace('interval_seconds = 30', 'interval_seconds = 0'))
            with self.assertRaisesRegex(ValueError, 'autosave.interval_seconds'):
                ksession.load_config()

    def test_report_retention(self):
        import background
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'XDG_STATE_HOME': d}), patch.object(background, 'notify'):
            for _ in range(55):
                background.failed('save', RuntimeError('failure'))
            self.assertEqual(len(list((Path(d)/'kitty-session-manager'/'errors').glob('*.txt'))), 50)

    def test_replace_failure_keeps_new_and_reports_closed(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader('loadsession_replace', 'loadsession')
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        windows = [{'tabs': [{'id': 1, 'windows': [{'id': 11}]}, {'id': 2, 'windows': [{'id': 22}]}, {'id': 3, 'windows': [{'id': 33}]}]}]
        def close(sock, *args):
            if 'id:2' in args:
                raise RuntimeError('close failed')
        with patch.object(module.ksession, 'run_ls', return_value=windows), patch.object(module, 'restore_add', return_value=[55]), patch.object(module, '_kitty', side_effect=close):
            with self.assertRaisesRegex(RuntimeError, r'Закрыты: \[1\].*Остались: \[2, 3\]'):
                module.restore_replace('sock', {}, 33)

class CompatibilityTests(unittest.TestCase):
    def test_version_one_sessions_sorted_and_bad_json_skipped(self):
        import json
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'XDG_DATA_HOME': d}):
            folder = ksession.storage_dir()
            (folder/'10.json').write_text(json.dumps({'version': 1, 'saved_at_epoch': 10, 'os_windows': []}))
            (folder/'20.json').write_text(json.dumps({'version': 1, 'saved_at_epoch': 20, 'os_windows': []}))
            (folder/'bad.json').write_text('{')
            result = ksession.load_sessions()
            self.assertEqual([s['saved_at_epoch'] for s in result], [20, 10])

    def test_repeated_save_retries_rotation(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'XDG_DATA_HOME': d}):
            folder = ksession.storage_dir()
            (folder/'1.json').write_text('old')
            with patch.object(Path, 'unlink', side_effect=OSError('blocked')):
                with self.assertRaises(RuntimeError):
                    ksession.save_many([{'kitty_pid': 2}], 1)
            ksession.save_many([{'kitty_pid': 2}], 1)
            self.assertEqual([p.name for p in folder.glob('*.json')], ['2.json'])

class EdgeTests(unittest.TestCase):
    def test_target_survives_future_mtime_old_snapshot(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'XDG_DATA_HOME': d}):
            old = ksession.storage_dir()/'1.json'
            old.write_text('old')
            os.utime(old, (4000000000, 4000000000))
            ksession.save_many([{'kitty_pid': 2}], 1)
            self.assertTrue((old.parent/'2.json').exists())
            self.assertFalse(old.exists())

    def test_invalid_section_has_parameter_error(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'XDG_CONFIG_HOME': d}):
            p = Path(d)/'kitty-session-manager'
            p.mkdir()
            (p/'config.toml').write_text('kitty = "wrong"\n')
            with self.assertRaisesRegex(ValueError, 'kitty'):
                ksession.load_config()

    def test_notification_failure_still_marks_failure_series(self):
        import background
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'XDG_STATE_HOME': d}), patch.object(background, 'notify', side_effect=RuntimeError('notify error')):
            background.failed('save', RuntimeError('failure'))
            self.assertTrue((Path(d)/'kitty-session-manager'/'failure-active').exists())

    def test_rollback_reports_remaining_tab(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader('loadsession_rollback', 'loadsession')
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        live = [{'tabs': [{'windows': [{'id': 42}]}]}]
        with patch.object(module, '_kitty', side_effect=RuntimeError('cannot close')), patch.object(module.ksession, 'run_ls', return_value=live):
            with self.assertRaisesRegex(RuntimeError, 'откат неполный: 42'):
                module._rollback('sock', [42], RuntimeError('creation error'))

    def test_rollback_accepts_already_absent_tab(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader('loadsession_absent', 'loadsession')
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        with patch.object(module, '_kitty', side_effect=RuntimeError('already absent')), patch.object(module.ksession, 'run_ls', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'откат выполнен'):
                module._rollback('sock', [42], RuntimeError('creation error'))
