"""Download and verify a pinned official Windows ADB runtime."""
import hashlib
import io
from pathlib import Path
import urllib.request
import zipfile

VERSION = "35.0.2"
SHA256 = "2975a3eac0b19182748d64195375ad056986561d994fffbdc64332a516300bb9"
FILES = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "libwinpthread-1.dll", "NOTICE.txt", "source.properties")


def main():
    url = f"https://dl.google.com/android/repository/platform-tools_r{VERSION}-win.zip"
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest() != SHA256:
        raise RuntimeError("ADB archive checksum mismatch")
    target = Path(__file__).resolve().parents[1] / "vendor" / "adb"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in FILES:
            (target / name).write_bytes(archive.read("platform-tools/" + name))
    print(f"Prepared official Platform Tools {VERSION}: {target}")


if __name__ == "__main__":
    main()
