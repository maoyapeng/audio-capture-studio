from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import os
import queue
import re
import shlex
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

from .models import Device, timestamp


def process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def adb_devices(executable: str = "adb") -> list[tuple[str, str]]:
    result = subprocess.run([executable, "devices", "-l"], capture_output=True,
                            text=True, timeout=8, **process_options())
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ADB 枚举失败")
    return [(parts[0], " ".join(parts[1:])) for line in result.stdout.splitlines()[1:]
            if len(parts := line.split()) >= 2]


class SSHTransport:
    def __init__(self, d: Device, emit):
        import paramiko
        self.d = d
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()

        class PinPolicy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
                if not d.fingerprint or fingerprint != d.fingerprint:
                    emit("hostkey", fingerprint)
                    raise RuntimeError(f"SSH 主机密钥尚未信任或不匹配：{fingerprint}；请核对后填写指纹")

        self.client.set_missing_host_key_policy(PinPolicy())
        try:
            self.client.connect(d.address, port=d.port, username=d.username or None,
                                password=d.password or None, key_filename=d.key_file or None,
                                timeout=8, auth_timeout=8, banner_timeout=8,
                                allow_agent=True, look_for_keys=True)
            key = self.client.get_transport().get_remote_server_key()
            actual = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
            if d.fingerprint and actual != d.fingerprint:
                raise RuntimeError("SSH 指纹与配置不匹配")
            self.channel = self.client.invoke_shell(term="xterm", width=120, height=40)
            self.channel.settimeout(.2)
        except Exception:
            self.client.close()
            raise

    def read(self):
        if self.channel.recv_ready():
            return self.channel.recv(8192)
        if self.channel.closed or not self.client.get_transport().is_active():
            raise ConnectionError("SSH 连接已断开")
        return b""

    def send(self, text):
        self.channel.sendall(text.encode(self.d.encoding))

    def stop_command(self, command):
        _, stdout, stderr = self.client.exec_command(command, timeout=8)
        stdout.channel.settimeout(8)
        out, err = stdout.read(), stderr.read()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(f"停止命令失败：{err.decode(self.d.encoding, errors='replace')}")
        return out.decode(self.d.encoding, errors="replace")

    def fetch(self, source, destination, emit):
        with self.client.open_sftp() as sftp:
            sftp.get_channel().settimeout(20)
            last = [0.0]

            def progress(done, total):
                if time.monotonic() - last[0] > .3 or done == total:
                    emit("state", f"取回文件 {done / max(1, total):.0%}")
                    last[0] = time.monotonic()
            sftp.get(source, str(destination), callback=progress)

    def close(self):
        self.client.close()


class ADBTransport:
    def __init__(self, d: Device, emit):
        self.d = d
        if not d.serial:
            raise ValueError("请选择 ADB 序列号")
        self.proc = subprocess.Popen([d.adb_path, "-s", d.serial, "shell", "-tt"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, bufsize=0, **process_options())
        self.queue = queue.Queue()
        self.reader = threading.Thread(target=self._read_pipe, daemon=True)
        self.reader.start()

    def _read_pipe(self):
        try:
            while chunk := self.proc.stdout.read(4096):
                self.queue.put(chunk)
        finally:
            self.queue.put(None)

    def read(self):
        try:
            data = self.queue.get_nowait()
            if data is None:
                raise ConnectionError("ADB shell 已断开，请检查授权和 USB 连接")
            return data
        except queue.Empty:
            return b""

    def send(self, text):
        self.proc.stdin.write(text.encode(self.d.encoding))
        self.proc.stdin.flush()

    def stop_command(self, command):
        result = subprocess.run([self.d.adb_path, "-s", self.d.serial, "shell", command],
                                capture_output=True, timeout=10, **process_options())
        if result.returncode:
            raise RuntimeError(result.stderr.decode(self.d.encoding, errors="replace"))
        return result.stdout.decode(self.d.encoding, errors="replace")

    def fetch(self, source, destination, emit):
        emit("state", "正在执行 adb pull")
        result = subprocess.run([self.d.adb_path, "-s", self.d.serial, "pull", source, str(destination)],
                                capture_output=True, timeout=300, **process_options())
        if result.returncode:
            raise RuntimeError(result.stderr.decode(self.d.encoding, errors="replace"))
        emit("log", result.stdout.decode(self.d.encoding, errors="replace"))

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.proc.stdout.close()
        self.proc.stdin.close()


class TelnetTransport:
    def __init__(self, d: Device, emit):
        import telnetlib3
        self.d = d
        self.loop = asyncio.new_event_loop()
        try:
            self.reader, self.writer = self.loop.run_until_complete(asyncio.wait_for(
                telnetlib3.open_connection(d.address, d.port, encoding=d.encoding,
                                           encoding_errors="replace", connect_minwait=.1,
                                           connect_maxwait=1), timeout=8))
        except Exception:
            self.loop.close()
            raise
        self.history = ""
        self.sent_user = self.sent_password = False
        self.logged_in = not d.username

    def read(self):
        try:
            data = self.loop.run_until_complete(asyncio.wait_for(self.reader.read(4096), .03))
        except asyncio.TimeoutError:
            return ""
        if not data:
            raise ConnectionError("Telnet 连接已断开")
        self.history = (self.history + data)[-2048:]
        if self.d.username and not self.sent_user and re.search(r"(?:login|username)\s*:\s*$", self.history, re.I):
            self.send(self.d.username + "\r\n")
            self.sent_user = True
            self.history = ""
        elif self.sent_user and not self.sent_password and re.search(r"password\s*:\s*$", self.history, re.I):
            self.send(self.d.password + "\r\n")
            self.sent_password = True
            self.history = ""
        if re.search(r"(?:^|[\r\n])[^\r\n]*[#$>]\s*$", self.history):
            self.logged_in = True
        return data

    def send(self, text):
        self.writer.write(text)
        self.loop.run_until_complete(self.writer.drain())

    def fetch(self, source, destination, emit):
        if not self.d.http_url:
            raise RuntimeError("Telnet 未配置 HTTP 下载地址；远端文件需手动取回")
        if not self.d.http_url.startswith(("http://", "https://")):
            raise ValueError("下载地址必须使用 http:// 或 https://")
        emit("state", "正在通过 HTTP 取回文件")
        with urllib.request.urlopen(self.d.http_url, timeout=20) as response, destination.open("wb") as output:
            while chunk := response.read(1024 * 256):
                output.write(chunk)

    def close(self):
        self.writer.close()
        self.loop.run_until_complete(asyncio.sleep(.05))
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()


class RemoteWorker(threading.Thread):
    def __init__(self, device: Device, emit):
        super().__init__(daemon=True, name=f"remote-{device.uid[:8]}")
        self.device, self.emit = device, emit
        self.commands = queue.Queue()
        self.quit = threading.Event()
        self.connected = False
        self.recording = False
        self.directory = None
        self.token = ""
        self.tail = ""
        self.stop_deadline = None
        self.transport = None
        self.remote_path = ""

    def post(self, action: str, payload=None):
        self.commands.put((action, payload))

    def close(self):
        self.quit.set()

    def run(self):
        decoder = codecs.getincrementaldecoder(self.device.encoding)(errors="replace")
        try:
            self.emit("state", "连接中")
            transport_class = {"SSH": SSHTransport, "ADB": ADBTransport, "Telnet": TelnetTransport}[self.device.kind]
            self.transport = transport_class(self.device, self.emit)
            self.connected = True
            self.emit("state", "已连接" if self.device.kind != "Telnet" else "已连接，等待登录")
            self.emit("log", f"已连接 {self.device.name}；终端尚需确认设备 shell 已就绪")
            while not self.quit.is_set():
                data = self.transport.read()
                if data:
                    text = data if isinstance(data, str) else decoder.decode(data)
                    self.emit("log", text)
                    self.tail = (self.tail + text)[-65536:]
                    if self.recording:
                        match = re.search(re.escape(self.token) + r":(\d+)", self.tail)
                        if match:
                            self._complete(int(match.group(1)))
                if self.stop_deadline and time.monotonic() > self.stop_deadline:
                    self.stop_deadline = None
                    self.recording = False
                    self.emit("error", "未收到采集程序退出标记，未下载文件。请检查终端和远端进程，必要时手动停止")
                    self.emit("finished", {})
                try:
                    action, payload = self.commands.get(timeout=.02)
                except queue.Empty:
                    continue
                try:
                    self._action(action, payload)
                except Exception as exc:
                    self.emit("error", str(exc))
                    if action in ("begin", "stop", "retry"):
                        self.recording = False
                        self.emit("finished", {})
        except Exception as exc:
            self.emit("error", str(exc))
            if self.recording:
                self.emit("finished", {})
        finally:
            self.connected = False
            if self.transport:
                self.transport.close()
            self.emit("disconnected", {})

    def _action(self, action, payload):
        d = self.device
        if action == "send":
            # Do not persist manually entered commands; they may contain credentials.
            self.transport.send(payload + "\n")
        elif action == "interrupt":
            self.transport.send("\x03")
        elif action == "begin":
            if self.recording:
                raise RuntimeError("该设备已在采集中")
            if d.kind == "Telnet" and not self.transport.logged_in:
                raise RuntimeError("Telnet 尚未检测到 shell 提示符；请先登录并确认终端就绪")
            if not d.command.strip():
                raise ValueError("请先配置启动命令")
            if "{remote_file}" not in d.command:
                raise ValueError("启动命令须包含 {remote_file}，用于每次采集生成独立远端文件，防止取回旧文件")
            self.directory = Path(payload)
            base = PurePosixPath(d.remote_file)
            self.remote_path = str(base.with_name(base.stem + "_" + uuid.uuid4().hex[:12] + base.suffix))
            self.token = "__ACS_DONE_" + uuid.uuid4().hex + "__"
            first, second = self.token[:6], self.token[6:]
            # Splitting the marker avoids matching terminal echo as process completion.
            command = d.command.replace("{remote_file}", shlex.quote(self.remote_path))
            script = f"(\n{command}\n); __acs_rc=$?; printf '%s%s:%s\\n' '{first}' '{second}' \"$__acs_rc\"\n"
            self.tail = ""
            self.recording = True
            self.stop_deadline = None
            try:
                self.transport.send(script)
            except Exception:
                self.recording = False
                raise
            self.emit("started", {"started_at": timestamp(), "remote_file": self.remote_path})
            self.emit("log", f"\n本次远端文件：{self.remote_path}\n")
            self.emit("state", "采集命令已发送")
        elif action == "stop" and self.recording:
            self.emit("state", "等待采集进程退出")
            if d.stop_command.strip():
                if d.kind == "Telnet":
                    raise RuntimeError("初版 Telnet 请使用 Ctrl+C 停止方式，不支持独立停止命令")
                output = self.transport.stop_command(d.stop_command.replace("{remote_file}", shlex.quote(self.remote_path)))
                self.emit("log", output)
            else:
                self.transport.send("\x03")
            self.stop_deadline = time.monotonic() + 20
        elif action == "retry":
            if not self.directory:
                raise RuntimeError("尚无可取回的录制文件")
            self._download()
            self.emit("finished", {})

    def _complete(self, returncode):
        self.recording = False
        self.stop_deadline = None
        self.emit("log", f"采集命令已退出，返回码 {returncode}")
        try:
            self._download(returncode)
        except Exception as exc:
            self.emit("error", f"文件取回 / 校验失败：{exc}；远端文件保留，可重试取回")
        finally:
            self.emit("finished", {})

    def _download(self, returncode=0):
        import soundfile as sf
        self.emit("state", "取回文件中")
        temp = self.directory / "audio.download"
        target = self.directory / "audio.wav"
        if target.exists():
            raise RuntimeError("本次文件已存在，禁止覆盖；请在文件管理器检查")
        if self.device.kind == "Telnet":
            from urllib.parse import quote
            original_url = self.transport.d.http_url
            self.transport.d.http_url = original_url.replace("{filename}", quote(PurePosixPath(self.remote_path).name)).replace("{remote_file}", quote(self.remote_path, safe="/"))
            try:
                self.transport.fetch(self.remote_path, temp, self.emit)
            finally:
                self.transport.d.http_url = original_url
        else:
            self.transport.fetch(self.remote_path, temp, self.emit)
        info = sf.info(temp)
        if not info.frames or info.channels != self.device.channels or info.samplerate != self.device.rate:
            raise RuntimeError(f"文件参数不符：{info.channels} ch / {info.samplerate} Hz / {info.frames} 帧；下载内容保留为 audio.download")
        expected = f"PCM_{self.device.bits}"
        if info.subtype != expected or info.format not in ("WAV", "WAVEX", "RF64"):
            raise RuntimeError(f"需要 {expected} 多通道 WAV，实际 {info.format}/{info.subtype}；下载内容保留")
        temp.replace(target)
        event = "saved" if returncode in (0, 130, 143) else "partial"
        self.emit(event, {"file": str(target), "frames": info.frames, "channels": info.channels,
                          "samplerate": info.samplerate, "returncode": returncode,
                          "duration_seconds": info.duration, "ended_at": timestamp(),
                          "verified": True})
