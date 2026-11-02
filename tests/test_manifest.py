import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audio_capture.models import Device, ManifestSaveError, create_session, save_manifest


def locked():
    error = PermissionError('Windows file locked')
    error.winerror = 5
    return error


class ManifestTests(unittest.TestCase):
    def test_session_saves_once_with_existing_device_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('audio_capture.models.save_manifest', wraps=save_manifest) as save:
                folder, manifest = create_session(Path(tmp), '任务', [Device('麦克风', 'USB')], True)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(json.loads((folder / '采集信息.json').read_text(encoding='utf-8')), manifest)
            for record in manifest['devices'].values():
                self.assertTrue((folder / record['directory']).is_dir())

    def test_transient_lock_retries_and_closes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            save_manifest(folder, {'old': True})
            original = Path.replace
            calls = []
            def replace(source, target):
                calls.append(source)
                if len(calls) < 3:
                    raise locked()
                return original(source, target)
            with patch.object(Path, 'replace', replace), patch('audio_capture.models.time.sleep') as sleep:
                save_manifest(folder, {'new': True})
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(json.loads((folder / '采集信息.json').read_text()), {'new': True})
            self.assertEqual(len(list(folder.iterdir())), 1)

    def test_persistent_lock_preserves_old_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            save_manifest(folder, {'old': True})
            with patch.object(Path, 'replace', side_effect=locked()) as replace, patch('audio_capture.models.time.sleep'):
                with self.assertRaisesRegex(ManifestSaveError, '尝试 5 次'):
                    save_manifest(folder, {'new': True})
            self.assertEqual(replace.call_count, 5)
            self.assertEqual(json.loads((folder / '采集信息.json').read_text()), {'old': True})
            recovery = list(folder.glob('采集信息.recovery-*.json'))
            self.assertEqual(len(recovery), 1)
            self.assertEqual(json.loads(recovery[0].read_text()), {'new': True})

    def test_other_errors_are_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(Path, 'replace', side_effect=OSError('disk error')), patch('audio_capture.models.time.sleep') as sleep:
                with self.assertRaises(ManifestSaveError):
                    save_manifest(Path(tmp), {})
            sleep.assert_not_called()
