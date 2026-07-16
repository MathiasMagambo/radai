from __future__ import annotations

import json
import socket
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .models import RecordingStatus


class StreamError(RuntimeError):
    pass


@dataclass(frozen=True)
class StreamStatus:
    reachable: bool
    url: str
    status_code: int | None = None
    content_type: str | None = None
    error: str | None = None


class IcecastClient:
    def __init__(self, stream_url: str, status_url: str | None = None) -> None:
        self.stream_url = stream_url
        self.status_url = status_url

    def check_stream(self, *, timeout: float = 5.0) -> StreamStatus:
        request = urllib.request.Request(self.stream_url, method="GET", headers={"User-Agent": "radai-agent/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return StreamStatus(
                    reachable=True,
                    url=self.stream_url,
                    status_code=response.status,
                    content_type=response.headers.get("Content-Type"),
                )
        except Exception as exc:
            return StreamStatus(reachable=False, url=self.stream_url, error=str(exc))

    def read_status_json(self, *, timeout: float = 5.0) -> dict:
        if not self.status_url:
            raise StreamError("Icecast status URL is not configured")
        request = urllib.request.Request(self.status_url, headers={"User-Agent": "radai-agent/0.1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))


class LiquidsoapControl:
    """Minimal line-oriented control surface for a Liquidsoap telnet/unix socket."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def available(self) -> bool:
        return self.socket_path.exists()

    def command(self, command: str, *, timeout: float = 5.0) -> str:
        if "\n" in command or "\r" in command:
            raise StreamError("Liquidsoap command must be one line")
        if not self.socket_path.exists():
            raise StreamError(f"Liquidsoap socket not found: {self.socket_path}")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(self.socket_path))
            client.sendall(command.encode("utf-8") + b"\n")
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"END" in chunk or chunk.endswith(b"\n"):
                    break
        return b"".join(chunks).decode("utf-8", errors="replace")

    def push_file(self, queue_name: str, media_path: Path) -> str:
        return self.command(f"{queue_name}.push {media_path}")

    def skip(self, queue_name: str) -> str:
        return self.command(f"{queue_name}.skip")


@dataclass(frozen=True)
class RecordingDecision:
    path: Path
    status: RecordingStatus


def next_recording_path(recordings_dir: Path, session_id: int, suffix: str = ".mp3") -> Path:
    recordings_dir.mkdir(parents=True, exist_ok=True)
    return recordings_dir / f"session-{session_id:06d}{suffix}"


def keep_recording(path: Path) -> RecordingDecision:
    if not path.exists():
        raise StreamError(f"recording not found: {path}")
    return RecordingDecision(path=path, status=RecordingStatus.KEPT)


def discard_recording(path: Path) -> RecordingDecision:
    path.unlink(missing_ok=True)
    return RecordingDecision(path=path, status=RecordingStatus.DISCARDED)


def recording_command(stream_url: str, output_path: Path) -> tuple[str, ...]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return (
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        stream_url,
        "-c",
        "copy",
        str(output_path),
    )
