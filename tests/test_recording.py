import json
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from audio_capture.audio import AudioWorker, max_usable_channels
from audio_capture.models import Device, create_session, safe_name
from audio_capture.remote import RemoteWorker


class RecordingTests(unittest.TestCase):
    def test_demo_writes_independent_multichannel_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            devices = [Device("八通道", "USB", channels=8, bits=24), Device("双通道", "USB", channels=2)]
            folder, manifest = create_session(Path(tmp), "任务/001", devices, True)
            workers, events = [], {}
            for d in devices:
                directory = folder / d.uid
                directory.mkdir()
                events[d.uid] = []
                worker = AudioWorker(d, directory, lambda kind, data, uid=d.uid: events[uid].append((kind, data)), True)
                worker.start()
                workers.append(worker)
            time.sleep(.25)
            for worker in workers:
                worker.stop()
                worker.join(3)
                self.assertFalse(worker.is_alive())
            for d in devices:
                data, rate = sf.read(folder / d.uid / "audio.wav", always_2d=True)
                self.assertEqual(data.shape[1], d.channels)
                self.assertEqual(rate, 48000)
                self.assertGreater(len(data), 0)
                self.assertGreater(np.max(np.abs(data[:, 0] - data[:, 1])), .01)
                self.assertIn("saved", [e[0] for e in events[d.uid]])
            self.assertTrue(json.loads((folder / "采集信息.json").read_text(encoding="utf-8"))["demo"])

    def test_no_password_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Device("SSH", "SSH", password="secret-password")
            folder, _ = create_session(Path(tmp), "安全", [d], False)
            self.assertNotIn("secret-password", (folder / "采集信息.json").read_text(encoding="utf-8"))
            self.assertNotIn("password", d.public())

    def test_windows_safe_names(self):
        self.assertEqual(safe_name("CON"), "_CON")
        self.assertEqual(safe_name("a/b:c*"), "a_b_c_")

    def test_maximum_channels_is_probed(self):
        from types import SimpleNamespace
        counts = []

        class BackendError(Exception):
            pass

        def check(**kwargs):
            counts.append(kwargs["channels"])
            if kwargs["channels"] > 4:
                raise BackendError("unsupported")
        backend = SimpleNamespace(check_input_settings=check, PortAudioError=BackendError)
        with patch("audio_capture.audio.sd", backend):
            self.assertEqual(max_usable_channels(Device("test", "USB", channels=8)), 4)
        self.assertEqual(counts, [8, 7, 6, 5, 4])

    def test_mismatched_maximum_is_not_silently_reduced(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            worker = AudioWorker(Device("test", "USB", channels=8), Path(tmp), lambda *e: events.append(e))
            with patch("audio_capture.audio.max_usable_channels", return_value=4):
                worker.run()
            self.assertIn("error", [e[0] for e in events])
            self.assertNotIn("saved", [e[0] for e in events])
            self.assertFalse((Path(tmp) / "audio.wav").exists())

    def test_input_disconnect_preserves_partial_audio(self):
        from types import SimpleNamespace
        events = []

        class Stream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]
                self.active = True

            def __enter__(self):
                self.callback(np.full((2400, 2), .1, dtype="float32"), 2400, None,
                              SimpleNamespace(input_overflow=False, __bool__=lambda: False))
                self.active = False
                return self

            def stop(self):
                pass

            def __exit__(self, *_):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            backend = SimpleNamespace(InputStream=Stream)
            worker = AudioWorker(Device("断开设备", "USB", channels=2), Path(tmp), lambda *e: events.append(e))
            with patch("audio_capture.audio.sd", backend), patch("audio_capture.audio.max_usable_channels", return_value=2):
                worker.run()
            kinds = [e[0] for e in events]
            self.assertIn("partial", kinds)
            self.assertIn("error", kinds)
            self.assertNotIn("saved", kinds)
            self.assertEqual(sf.info(Path(tmp) / "audio.wav").frames, 2400)


class FakeTransport:
    channels = 2

    def __init__(self, d, emit):
        self.incoming = queue.Queue()
        self.d = d
        self.script = ""

    def read(self):
        try:
            return self.incoming.get_nowait()
        except queue.Empty:
            return b""

    def send(self, text):
        if text == "\x03":
            import re
            first, second = re.search(r"printf .*? '([^']+)' '([^']+)'", self.script).groups()
            self.incoming.put((first + second + ":130\r\n").encode())
        else:
            self.script = text
            # An echoed command must never be mistaken for completed capture.
            self.incoming.put(text.encode())

    def fetch(self, source, destination, emit):
        sf.write(destination, np.ones((4800, self.channels)) * .1, 48000, format="WAV", subtype="PCM_16")

    def close(self):
        pass


class RemoteTests(unittest.TestCase):
    def exercise(self, temp, channels):
        events = []
        d = Device("远程", "SSH", channels=channels, command="record {remote_file}")
        worker = RemoteWorker(d, lambda *e: events.append(e))
        with patch("audio_capture.remote.SSHTransport", FakeTransport):
            worker.start()
            end = time.monotonic() + 3
            while not worker.connected and time.monotonic() < end:
                time.sleep(.01)
            self.assertTrue(worker.connected)
            worker.post("begin", temp)
            time.sleep(.12)
            self.assertFalse(any(e[0] == "saved" for e in events))
            self.assertNotEqual(worker.remote_path, d.remote_file)
            self.assertIn("/tmp/audio_", worker.remote_path)
            worker.post("stop")
            end = time.monotonic() + 3
            while not any(e[0] == "finished" for e in events) and time.monotonic() < end:
                time.sleep(.01)
            worker.close()
            worker.join(2)
        return events

    def test_echo_is_not_completion_and_file_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = self.exercise(Path(tmp), 2)
            self.assertIn("saved", [e[0] for e in events])
            self.assertTrue((Path(tmp) / "audio.wav").is_file())

    def test_wrong_channel_file_is_not_marked_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = self.exercise(Path(tmp), 8)
            self.assertNotIn("saved", [e[0] for e in events])
            self.assertIn("error", [e[0] for e in events])
            self.assertTrue((Path(tmp) / "audio.download").is_file())


if __name__ == "__main__":
    unittest.main()
