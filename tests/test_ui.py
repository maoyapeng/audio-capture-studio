import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import soundfile as sf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from audio_capture.app import MainWindow, STYLE
from audio_capture.models import Device


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

    def test_manifest_failure_blocks_start_with_accurate_message(self):
        from audio_capture.models import ManifestSaveError
        with tempfile.TemporaryDirectory() as tmp:
            window = MainWindow(demo=True, config_dir=Path(tmp) / "config")
            window.scan_timer.stop()
            window.destination.setText(str(Path(tmp) / "recordings"))
            with patch("audio_capture.app.create_session", side_effect=ManifestSaveError("locked")), patch("audio_capture.app.QMessageBox.critical") as dialog:
                window.start_button.click()
            self.assertFalse(window.active)
            self.assertEqual(dialog.call_args.args[1], "无法保存采集信息")
            window.close()

    def test_final_manifest_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = MainWindow(demo=True, config_dir=Path(tmp) / "config")
            window.scan_timer.stop()
            device = window.devices[0]
            window.session_dir = Path(tmp)
            window.manifest = {"devices": {device.uid: {"state": "已保存"}}}
            window.active = {device.uid}
            with patch("audio_capture.app.save_manifest", side_effect=OSError("recovery path")), patch("audio_capture.app.QMessageBox.warning") as dialog:
                window.handle_event(device.uid, "finished", {})
            self.assertFalse(window.active)
            self.assertIn("采集信息保存失败", window.record_state.text())
            self.assertIn("recovery path", dialog.call_args.args[2])
            window.close()

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
            self.assertEqual(len(list(window.session_dir.glob("*/*.wav"))), 4)
            self.assertTrue(window.start_button.isEnabled())
            self.assertFalse(window.stop_button.isEnabled())
            window.close()
            self.pump(.05)

    def test_level_preview_updates_meters_without_creating_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = MainWindow(demo=True, config_dir=Path(tmp) / "config")
            window.scan_timer.stop()
            window.destination.setText(str(Path(tmp) / "recordings"))
            window.test_button.click()
            self.pump(.25)
            self.assertIsNotNone(window.level_test)
            self.assertGreater(window.meters[0][0].value(), 0)
            self.assertFalse(window.start_button.isEnabled())
            self.assertIsNone(window.session_dir)
            self.assertFalse((Path(tmp) / "recordings").exists())
            window.test_button.click()
            self.pump(.3)
            self.assertIsNone(window.level_test)
            self.assertTrue(window.start_button.isEnabled())
            self.assertEqual(window.meters[0][0].value(), 0)
            window.close()

    def test_only_confirmed_usb_and_one_driver_are_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = MainWindow(demo=True, config_dir=Path(tmp))
            window.devices = []
            raw = [
                dict(index=0, name="USB Mic", host="Windows WASAPI", channels=8, bus="usb"),
                dict(index=1, name="USB Mic", host="MME", channels=2, bus="usb"),
                dict(index=2, name="Realtek", host="Windows WASAPI", channels=2, bus="other"),
                dict(index=3, name="AirPods", host="Windows WASAPI", channels=1, bus="bluetooth"),
                dict(index=4, name="主声音捕获驱动程序", host="Windows DirectSound", channels=2, bus="unknown"),
            ]
            window._discovered(dict(audio=raw, adb=[], errors=[]))
            self.assertEqual(window.device_list.count(), 3)
            window.source_filter.setCurrentIndex(window.source_filter.findData("usb"))
            self.assertEqual(window.device_list.count(), 1)
            self.assertEqual(window.current_device().channels, 8)
            window.source_filter.setCurrentIndex(window.source_filter.findData("all"))
            self.assertEqual(window.device_list.count(), 3)
            window.device_list.setCurrentRow(1)
            self.assertNotIn("/  USB", window.device_title.text())
            self.assertFalse(window.connect_button.isVisible())
            # A changed index at hotplug must not lose the alias/selection.
            usb = next(d for d in window.devices if d.system_name == "USB Mic" and d.host_api == "Windows WASAPI")
            usb.name = "我的麦克风"
            raw[0]["index"] = 6
            window._discovered(dict(audio=raw, adb=[], errors=[]))
            self.assertEqual(usb.audio_index, 6)
            self.assertEqual(usb.name, "我的麦克风")
            window.host_filter.setCurrentIndex(window.host_filter.findData("__all__"))
            self.assertEqual(window.device_list.count(), 5)
            self.assertTrue(any("MME" in window.device_list.item(i).text() for i in range(5)))
            window.close()

    def test_local_panel_and_remote_terminal_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = MainWindow(demo=True, config_dir=Path(tmp))
            window.show()
            self.pump(.05)
            self.assertGreater(window.device_title.width(), 100)
            self.assertTrue(window.meter_area.isVisible())
            self.assertFalse(window.terminal.isVisible())
            self.assertFalse(window.command_widget.isVisible())
            uid = window.current_device().uid
            window.handle_event(uid, "levels", [.1] * 8)
            before = window.meters[0][0]
            window.refresh_list()
            self.assertIs(window.meters[0][0], before)
            self.assertGreater(before.value(), 0)
            window.resize(2400, 1200)
            self.pump(.05)
            self.assertLessEqual(window.sidebar.width(), 430)
            window.device_list.setCurrentRow(2)
            self.assertTrue(window.terminal.isVisible())
            self.assertTrue(window.command_widget.isVisible())
            self.assertFalse(window.meter_area.isVisible())
            window.close()

    def test_devices_on_different_drivers_record_together_after_filter_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = MainWindow(demo=True, config_dir=Path(tmp) / "config")
            window.scan_timer.stop()
            window.devices = []
            window._discovered(dict(audio=[
                dict(index=1, name="Nine channels", host="Windows WASAPI", channels=9, bus="usb", default_rate=16000),
                dict(index=2, name="Stereo", host="MME", channels=2, bus="unknown"),
            ], adb=[], errors=[]))
            self.assertEqual(window.device_list.count(), 2)
            self.assertEqual(window.devices[0].rate, 16000)
            window.devices[0].rate = 32000
            window.refresh_list()
            self.assertEqual(window.devices[0].rate, 32000)
            for device in window.devices:
                device.selected = True
            window.host_filter.setCurrentIndex(window.host_filter.findData("MME"))
            self.assertEqual(window.device_list.count(), 1)
            self.assertTrue(all(d.selected for d in window.devices))
            self.assertIn("隐藏 1", window.count_label.text())
            self.assertIn("Nine channels", window.selection_summary.text())
            self.assertIn("Stereo", window.selection_summary.text())
            window.destination.setText(str(Path(tmp) / "recordings"))
            window.start_button.click()
            self.pump(.25)
            self.assertEqual(len(window.active), 2)
            self.assertFalse(window.clear_selection_button.isEnabled())
            window.stop_button.click()
            self.pump(.5)
            self.assertFalse(window.active)
            records = json.loads((window.session_dir / "采集信息.json").read_text(encoding="utf-8"))["devices"]
            self.assertEqual(len(records), 2)
            for record in records.values():
                self.assertEqual(record["state"], "已保存")
                info = sf.info(record["file"])
                self.assertEqual(info.channels, record["device"]["channels"])
                self.assertGreater(info.frames, 0)
            window.clear_selection_button.click()
            self.assertFalse(any(d.selected for d in window.devices))
            window.close()


if __name__ == "__main__":
    unittest.main()
