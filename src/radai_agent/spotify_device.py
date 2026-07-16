from __future__ import annotations

from dataclasses import dataclass

from .spotify import SpotifyClient, SpotifyDevice, SpotifyError


@dataclass(frozen=True)
class DeviceSelection:
    configured_name: str
    device: SpotifyDevice


def find_configured_device(client: SpotifyClient, configured_name: str) -> DeviceSelection:
    devices = client.devices()
    exact = [device for device in devices if device.name == configured_name]
    if exact:
        return DeviceSelection(configured_name, exact[0])
    lowered = configured_name.lower()
    partial = [device for device in devices if lowered in device.name.lower()]
    if partial:
        return DeviceSelection(configured_name, partial[0])
    available = ", ".join(device.name for device in devices) or "none"
    raise SpotifyError(f"configured Spotify device {configured_name!r} not found; available: {available}")


def ensure_device_ready(client: SpotifyClient, configured_name: str, *, activate: bool = True) -> DeviceSelection:
    selection = find_configured_device(client, configured_name)
    if selection.device.is_restricted:
        raise SpotifyError(f"Spotify device {selection.device.name!r} is restricted")
    if activate and not selection.device.is_active:
        client.transfer_playback(selection.device.id, play=False)
    return selection
