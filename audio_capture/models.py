from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:90]
    if not value:
        return "unnamed"
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        value = "_" + value
    return value


@dataclass
class Device:
    name: str
    kind: str
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    channels: int = 2
    rate: int = 48000
    bits: int = 16
    address: str = ""
    port: int = 22
    username: str = ""
    password: str = field(default="", repr=False)
    key_file: str = ""
    fingerprint: str = ""
    command: str = ""
    stop_command: str = ""
    remote_file: str = "/tmp/audio.wav"
    http_url: str = ""
    adb_path: str = "adb"
    serial: str = ""
    encoding: str = "utf-8"
    selected: bool = True
    available: bool = True
    audio_index: int = -1
    system_name: str = ""
    host_api: str = ""
    capture_mode: str = "shared"
    bus: str = "unknown"
    endpoint_id: str = ""
    container_id: str = ""
    hardware_id: str = ""

    def public(self) -> dict:
        data = asdict(self)
        data.pop("password", None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Device":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "password"})


def create_session(base: Path, title: str, devices: list[Device], demo: bool) -> tuple[Path, dict]:
    base.mkdir(parents=True, exist_ok=True)
    folder = base / f"{safe_name(title)}_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    folder.mkdir()
    manifest = {"version": 1, "created_at": timestamp(), "demo": demo,
                "synchronization": "independent clocks; coordinated software start only",
                "devices": {d.uid: {"device": d.public(), "state": "待启动"} for d in devices}}
    save_manifest(folder, manifest)
    return folder, manifest


def save_manifest(folder: Path, manifest: dict) -> None:
    target = folder / "采集信息.json"
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)


def export_profile(devices: list[Device], selected_only: bool, task: str) -> dict:
    rows = []
    for device in devices:
        row = device.public()
        row["audio_index"] = -1
        row["available"] = False
        rows.append(row)
    return {"format": "AudioCaptureStudio-profile", "version": 1,
            "devices": rows, "selected_only": selected_only, "task": task}


def import_profile(data: dict) -> tuple[list[Device], bool, str]:
    if not isinstance(data, dict) or data.get("format") != "AudioCaptureStudio-profile" or data.get("version") != 1:
        raise ValueError("不是支持的 AudioCapture Studio 配置文件")
    rows = data.get("devices")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("设备列表格式错误或设备数量过多")
    selected_only, task = data.get("selected_only", False), data.get("task", "语音采集_001")
    if type(selected_only) is not bool or not isinstance(task, str):
        raise ValueError("显示设置或任务名称格式错误")
    devices = []
    defaults = Device("", "USB")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("设备配置必须是对象")
        for key, value in row.items():
            if key in Device.__dataclass_fields__ and key != "password" and type(value) is not type(getattr(defaults, key)):
                raise ValueError(f"设备字段类型错误：{key}")
        d = Device.from_dict(row)
        if not d.name.strip() or d.kind not in ("USB", "ADB", "SSH", "Telnet"):
            raise ValueError("设备名称或接入类型无效")
        if not 1 <= d.channels <= 256 or not 8000 <= d.rate <= 384000 or d.bits not in (16, 24, 32):
            raise ValueError(f"{d.name} 的音频参数无效")
        if not 1 <= d.port <= 65535 or d.capture_mode not in ("shared", "exclusive"):
            raise ValueError(f"{d.name} 的连接参数无效")
        import codecs
        codecs.lookup(d.encoding)
        # New IDs isolate imported templates from queued events of old workers.
        d.uid = uuid.uuid4().hex
        d.audio_index = -1
        d.available = d.kind != "USB"
        devices.append(d)
    return devices, selected_only, task
