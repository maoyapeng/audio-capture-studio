"""Physical-bus classification for Windows audio inputs.

PortAudio names are not evidence of USB transport. Match the Windows audio
endpoint container to a present USB MEDIA devnode. Never guess from a name.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess


WINDOWS_INVENTORY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$devices = @(Get-PnpDevice -PresentOnly)
$media = @($devices | Where-Object { $_.Class -eq 'MEDIA' })
$endpoints = @($devices | Where-Object {
    $_.Class -eq 'AudioEndpoint' -and $_.InstanceId -match '\{0\.0\.1\.'
})
function Container($id) {
    $p = Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_ContainerId' -ErrorAction SilentlyContinue
    if ($p -and $p.Data) { return $p.Data.ToString().ToLowerInvariant() }
    return ''
}
$byContainer = @{}
foreach ($device in $media) {
    $container = Container $device.InstanceId
    if (!$container -or $container -eq '00000000-0000-0000-0000-000000000000') { continue }
    $bus = 'other'
    if ($device.InstanceId -match '^USB\\VID_') { $bus = 'usb' }
    elseif ($device.InstanceId -match '^(BTH|BTHENUM|BTHHFENUM)\\') { $bus = 'bluetooth' }
    if (!$byContainer.ContainsKey($container)) { $byContainer[$container] = @() }
    $byContainer[$container] += @{ bus = $bus; hardware_id = $device.InstanceId }
}
$result = @()
foreach ($device in $endpoints) {
    $container = Container $device.InstanceId
    $matches = @()
    if ($container -and $byContainer.ContainsKey($container)) { $matches = @($byContainer[$container]) }
    $bus = 'unknown'
    $hardware = ''
    $usb = @($matches | Where-Object { $_.bus -eq 'usb' })
    $bt = @($matches | Where-Object { $_.bus -eq 'bluetooth' })
    if ($usb.Count -gt 0 -and $bt.Count -eq 0) { $bus = 'usb'; $hardware = $usb[0].hardware_id }
    elseif ($bt.Count -gt 0 -and $usb.Count -eq 0) { $bus = 'bluetooth'; $hardware = $bt[0].hardware_id }
    elseif ($matches.Count -gt 0 -and $usb.Count -eq 0 -and $bt.Count -eq 0) { $bus = 'other' }
    $result += @{ name = $device.FriendlyName; endpoint_id = $device.InstanceId;
                  container_id = $container; bus = $bus; hardware_id = $hardware }
}
ConvertTo-Json -InputObject $result -Depth 5 -Compress
"""


def windows_inventory() -> list[dict]:
    if os.name != "nt":
        return []
    encoded = base64.b64encode(WINDOWS_INVENTORY.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Windows 硬件识别失败；可切换到“全部本地音频”查看输入设备")
    inventory = json.loads(result.stdout.decode("utf-8-sig"))
    if not isinstance(inventory, list):
        raise ValueError("Windows 硬件信息格式不正确")
    return inventory


def name_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def annotate_inputs(inputs: list[dict], inventory: list[dict]) -> list[dict]:
    result = []
    for audio in inputs:
        item = {**audio, "bus": "unknown", "endpoint_id": "", "container_id": "", "hardware_id": ""}
        matches = [e for e in inventory if name_key(e.get("name", "")) == name_key(audio["name"])]
        # Same-name devices remain separate PortAudio entries; only share bus
        # classification when every possible endpoint agrees. Do not assign an ID.
        if matches and len({e.get("bus", "unknown") for e in matches}) == 1:
            item["bus"] = matches[0].get("bus", "unknown")
        if len(matches) == 1:
            for key in ("endpoint_id", "container_id", "hardware_id"):
                item[key] = matches[0].get(key, "")
        result.append(item)
    return result


def preferred_host(hosts: list[str]) -> str:
    if not hosts:
        return ""
    for keyword in ("WASAPI", "Core Audio", "ALSA", "DirectSound", "MME"):
        for host in hosts:
            if keyword.casefold() in host.casefold():
                return host
    return hosts[0]


def is_default_alias(name: str) -> bool:
    return name_key(name) in {
        "microsoft sound mapper - input", "primary sound capture driver",
        "主声音捕获驱动程序", "主音频捕获驱动程序",
    }
