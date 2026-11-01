from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

try:
    import sounddevice as sd
except (OSError, ImportError):
    sd = None

from .models import Device, timestamp, safe_name


def scan_audio(reinitialize: bool = False) -> list[dict]:
    if sd is None:
        raise RuntimeError("无法加载 PortAudio；请重新安装 sounddevice。Linux 需要系统 libportaudio2")
    # PortAudio caches endpoints. Only refresh when no audio workers exist.
    if reinitialize:
        sd._terminate()
        sd._initialize()
    hosts = sd.query_hostapis()
    return [dict(index=i, name=d["name"], channels=int(d["max_input_channels"]),
                 host=hosts[d["hostapi"]]["name"], default_rate=int(d["default_samplerate"]))
            for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def stream_options(device: Device) -> dict:
    if device.capture_mode == "exclusive":
        if "WASAPI" not in device.host_api:
            raise RuntimeError("独占采集需要选择 Windows WASAPI 驱动接口")
        return {"extra_settings": sd.WasapiSettings(exclusive=True, auto_convert=False)}
    return {}


def max_usable_channels(device: Device) -> int:
    """Do not silently record fewer channels: discover and report actual maximum."""
    if sd is None:
        raise RuntimeError("无法加载 PortAudio 音频后端")
    for count in range(device.channels, 0, -1):
        try:
            sd.check_input_settings(device=device.audio_index, channels=count,
                                    samplerate=device.rate, dtype="float32", **stream_options(device))
            return count
        except sd.PortAudioError:
            continue
    raise RuntimeError("所选采样率无法打开该设备，请修改采样率或驱动接口")


class AudioWorker(threading.Thread):
    def __init__(self, device: Device, directory: Path, emit, demo: bool = False):
        super().__init__(daemon=True, name=f"audio-{device.uid[:8]}")
        self.device, self.directory, self.emit, self.demo = device, directory, emit, demo
        self.stop_event = threading.Event()
        self.buffers = queue.Queue(maxsize=256)
        self.fatal = threading.Event()
        self.dropped = 0
        self.overflows = 0
        self.frames = 0
        self.stream = None

        self.last_callback = 0.0

    def stop(self):
        self.stop_event.set()

    def run(self):
        d = self.device
        path = self.directory / f"{safe_name(self.device.name)}.wav"
        try:
            if not self.demo:
                count = max_usable_channels(d)
                if count != d.channels:
                    raise RuntimeError(f"当前格式最多支持 {count} 通道，设备标称 {d.channels} 通道；请修改格式后重试")
            # RF64 is WAV-compatible and supports files larger than 4 GiB.
            with sf.SoundFile(path, mode="w", samplerate=d.rate, channels=d.channels,
                              subtype=f"PCM_{d.bits}", format="RF64") as output:
                self.emit("log", "演示信号开始（非麦克风数据）" if self.demo else
                          f"正在打开音频流：{d.system_name or d.name} / {d.host_api} / {d.capture_mode}；软件不做降噪、增益或混音")
                if self.demo:
                    self._emit_started()
                    self._demo(output)
                else:
                    self._record(output)
            result = {"frames": self.frames, "duration_seconds": self.frames / d.rate,
                      "dropped_blocks": self.dropped, "input_overflows": self.overflows,
                      "ended_at": timestamp(), "file": str(path)}
            if not self.frames:
                raise RuntimeError("未采集到音频帧；文件为空，请检查设备")
            info = sf.info(path)
            result["verified"] = {"channels": info.channels, "samplerate": info.samplerate,
                                  "frames": info.frames, "format": info.format}
            if self.fatal.is_set():
                result["partial"] = True
                self.emit("error", "输入中断、缓冲溢出或磁盘写入过慢；已保留收到的音频")
                self.emit("partial", result)
            else:
                self.emit("saved", result)
        except Exception as exc:
            self.emit("error", f"{d.name} / {d.host_api} / {d.channels} 通道 / {d.rate} Hz：{exc}。"
                      " 请检查采样率、设备连接、Windows 麦克风权限及是否被其他程序独占。")
        finally:
            self.emit("finished", {})

    def _emit_started(self):
        self.emit("started", {"started_at": timestamp(), "channels": self.device.channels,
                              "file": str(self.directory / f"{safe_name(self.device.name)}.wav")})

    def _demo(self, output):
        d = self.device
        block = max(256, d.rate // 20)
        phase = np.arange(d.channels)[None, :] * .4
        while not self.stop_event.is_set():
            start = time.monotonic()
            t = (np.arange(block) + self.frames)[:, None] / d.rate
            data = (.12 * np.sin(2 * np.pi * t * (220 + np.arange(d.channels)[None, :] * 55) + phase)).astype("float32")
            output.write(data)
            self.frames += block
            self.emit("levels", np.max(np.abs(data), axis=0).tolist())
            self.stop_event.wait(max(0, block / d.rate - (time.monotonic() - start)))

    def _record(self, output):
        d = self.device

        def callback(data, frames, timing, status):
            self.last_callback = time.monotonic()
            if status.input_overflow:
                self.overflows += 1
                self.fatal.set()
            if status:
                self.emit("log", f"音频驱动状态：{status}")
            try:
                self.buffers.put_nowait(data.copy())
            except queue.Full:
                self.dropped += 1
                self.fatal.set()

        self.last_callback = time.monotonic()
        with sd.InputStream(device=d.audio_index, samplerate=d.rate, channels=d.channels,
                            dtype="float32", blocksize=0,
                            callback=callback, **stream_options(d)) as stream:
            self.stream = stream
            self._emit_started()
            self.emit("log", f"采集中：{d.channels} 通道 / {d.rate} Hz / {d.bits} bit")
            last_meter = 0.0
            while not self.stop_event.is_set() and not self.fatal.is_set():
                try:
                    data = self.buffers.get(timeout=.2)
                    output.write(data)
                    self.frames += len(data)
                    if time.monotonic() - last_meter > .1:
                        self.emit("levels", np.max(np.abs(data), axis=0).tolist())
                        last_meter = time.monotonic()
                except queue.Empty:
                    if not stream.active or time.monotonic() - self.last_callback > 3:
                        self.fatal.set()
                        self.emit("log", "音频输入停止响应，可能设备已拔出")
            stream.stop()
            while not self.buffers.empty():
                data = self.buffers.get_nowait()
                output.write(data)
                self.frames += len(data)
        self.stream = None

class LevelTestWorker(AudioWorker):
    """Use the recording input path, discarding samples instead of writing files."""

    def __init__(self, device, emit, demo=False):
        super().__init__(device, Path(), emit, demo)

    def _emit_started(self):
        self.emit("started", {})

    def run(self):
        class Discard:
            def write(self, data):
                pass

        try:
            if self.demo:
                self._emit_started()
                self._demo(Discard())
            else:
                count = max_usable_channels(self.device)
                if count != self.device.channels:
                    raise RuntimeError(f"当前格式仅支持 {count} 通道，请调整采样率或驱动接口")
                self._record(Discard())
                if self.fatal.is_set():
                    raise RuntimeError("音频输入中断或缓冲溢出，请检查设备连接")
        except Exception as exc:
            self.emit("error", f"音量测试失败：{self.device.name} / {self.device.rate} Hz：{exc}")
        finally:
            self.emit("finished", {})
