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
