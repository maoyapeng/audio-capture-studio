import os
import unittest
import subprocess
from unittest.mock import patch

from audio_capture.discovery import annotate_inputs, is_default_alias, preferred_host, windows_inventory


class DiscoveryTests(unittest.TestCase):
    def test_name_is_not_usb_evidence(self):
        devices = annotate_inputs([{"name": "USB Audio Device", "host": "Windows WASAPI", "index": 0, "channels": 2}], [])
        self.assertEqual(devices[0]["bus"], "unknown")

    def test_usb_realtek_and_bluetooth_stay_distinct(self):
        names = ["麦克风 (USB Audio Device)", "麦克风阵列 (Realtek)", "耳机 (AirPods)"]
        inputs = [dict(index=i, name=n, host="Windows WASAPI", channels=2) for i, n in enumerate(names)]
        inventory = [dict(name=n, bus=b, endpoint_id=str(i))
                     for i, (n, b) in enumerate(zip(names, ["usb", "other", "bluetooth"]))]
        devices = annotate_inputs(inputs, inventory)
        self.assertEqual([d["bus"] for d in devices], ["usb", "other", "bluetooth"])

    def test_two_identical_usb_mics_are_not_collapsed_or_given_wrong_ids(self):
        inputs = [dict(name="USB Mic", index=i, host="Windows WASAPI", channels=8) for i in (3, 4)]
        inventory = [dict(name="USB Mic", bus="usb", endpoint_id=f"endpoint-{i}") for i in (1, 2)]
        devices = annotate_inputs(inputs, inventory)
        self.assertEqual(len(devices), 2)
        self.assertTrue(all(d["bus"] == "usb" and d["endpoint_id"] == "" for d in devices))

    def test_ambiguous_bus_is_not_claimed_to_be_usb(self):
        devices = annotate_inputs([dict(name="Mic", index=1, host="Windows WASAPI", channels=2)],
                                  [dict(name="Mic", bus="usb"), dict(name="Mic", bus="other")])
        self.assertEqual(devices[0]["bus"], "unknown")

    def test_driver_preference_and_default_aliases(self):
        self.assertEqual(preferred_host(["MME", "Windows DirectSound", "Windows WASAPI"]), "Windows WASAPI")
        self.assertTrue(is_default_alias("主声音捕获驱动程序"))
        self.assertTrue(is_default_alias("Microsoft Sound Mapper - Input"))
        self.assertFalse(is_default_alias("USB 麦克风"))

    def test_timeout_has_short_actionable_message(self):
        with patch("audio_capture.discovery.os.name", "nt"), \
             patch("audio_capture.discovery.subprocess.CREATE_NO_WINDOW", 0, create=True), \
             patch("audio_capture.discovery.subprocess.run", side_effect=subprocess.TimeoutExpired("encoded command", 8)):
            with self.assertRaisesRegex(RuntimeError, "硬件查询超时") as error:
                windows_inventory()
            self.assertNotIn("encoded", str(error.exception))

    @unittest.skipUnless(os.name == "nt", "Windows PnP integration")
    def test_windows_inventory_command_executes(self):
        self.assertIsInstance(windows_inventory(), list)


if __name__ == "__main__":
    unittest.main()
