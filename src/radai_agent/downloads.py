from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class DownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadResult:
    url: str
    path: Path
    bytes_written: int
    sha256: str
    content_type: str | None


AUDIO_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


def safe_filename(url: str, fallback_ext: str = ".bin") -> str:
    parsed = urlparse(url)
    basename = Path(parsed.path).name or "download"
    stem = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in basename).strip("._")
    if not stem:
        stem = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    if "." not in stem and fallback_ext:
        stem += fallback_ext
    return stem


def download_audio(url: str, dest_dir: Path, *, max_bytes: int = 1_500_000_000, timeout: float = 60.0) -> DownloadResult:
    dest_dir.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "radai-agent/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() or None
        if content_type and not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
            raise DownloadError(f"refusing non-audio content type {content_type!r}")
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > max_bytes:
            raise DownloadError("refusing download larger than configured maximum")
        suffix = AUDIO_EXTENSIONS.get(content_type or "") or Path(urlparse(url).path).suffix or ".bin"
        final_path = dest_dir / safe_filename(url, suffix)
        if final_path.exists():
            digest = _sha256_file(final_path)
            return DownloadResult(url, final_path, final_path.stat().st_size, digest, content_type)
        hasher = hashlib.sha256()
        written = 0
        fd, tmp_name = tempfile.mkstemp(prefix=final_path.name + ".", suffix=".tmp", dir=dest_dir)
        try:
            with os.fdopen(fd, "wb") as tmp:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise DownloadError("download exceeded configured maximum")
                    hasher.update(chunk)
                    tmp.write(chunk)
            os.replace(tmp_name, final_path)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return DownloadResult(url, final_path, written, hasher.hexdigest(), content_type)


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
