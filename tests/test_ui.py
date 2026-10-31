import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from audio_capture.app import MainWindow, STYLE


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)

    def pump(self, duration):
        end = time.monotonic() + duration
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.01)

    def test_demo_start_stop_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = MainWindow(demo=True, config_dir=Path(tmp) / "config")
            window.scan_timer.stop()
            window.destination.setText(str(Path(tmp) / "recordings"))
            window.show()
            self.pump(.1)
            self.assertEqual(window.device_list.count(), 4)
            window.device_list.setCurrentRow(2)
            self.assertIn("SSH", window.device_title.text())
            window.start_button.click()
            self.pump(.3)
            self.assertEqual(len(window.active), 4)
            self.assertFalse(window.start_button.isEnabled())
            window.stop_button.click()
            self.pump(.5)
            self.assertFalse(window.active)
            data = json.loads((window.session_dir / "采集信息.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data["devices"]), 4)
            self.assertTrue(all(d["state"] == "已保存" for d in data["devices"].values()))
            self.assertEqual(len(list(window.session_dir.glob("*/audio.wav"))), 4)
            self.assertTrue(window.start_button.isEnabled())
            self.assertFalse(window.stop_button.isEnabled())
            window.close()
            self.pump(.05)


if __name__ == "__main__":
    unittest.main()
