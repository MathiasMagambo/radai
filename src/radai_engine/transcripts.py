from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

from .models import TranscriptRef

TIMESTAMP_RE = re.compile(
    r"^\s*(?:\d+\s*)?(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[,.]\d{1,3})?\s*--?>\s*(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[,.]\d{1,3})?.*$"
)
SRT_INDEX_RE = re.compile(r"^\s*\d+\s*$")
WEBVTT_RE = re.compile(r"^\s*WEBVTT\b", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


class TranscriptUnavailable(RuntimeError):
    pass


class TranscriptProvider(Protocol):
    def transcribe(self, audio_path: Path, text_path: Path) -> Path:
        """Create a transcript text file for audio_path and return text_path."""


@dataclass(frozen=True)
class TranscriptFile:
    source: TranscriptRef | None
    raw_path: Path | None
    text_path: Path


def fetch_transcript(ref: TranscriptRef, dest_dir: Path, *, timeout: float = 30.0) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(ref.url, headers={"User-Agent": "radai-engine/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    suffix = _suffix_for_mime(ref.mime)
    path = dest_dir / (_safe_name(ref.url) + suffix)
    path.write_bytes(body)
    return path


def normalize_transcript(raw_path: Path, mime: str, text_path: Path | None = None) -> Path:
    text_path = text_path or raw_path.with_suffix(raw_path.suffix + ".txt")
    raw_text = raw_path.read_text(encoding="utf-8", errors="replace")
    normalized = normalize_transcript_text(raw_text, mime)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(normalized, encoding="utf-8")
    return text_path


def normalize_transcript_text(raw_text: str, mime: str) -> str:
    mime = mime.split(";", 1)[0].strip().lower()
    if mime in {"text/vtt", "application/x-subrip"} or WEBVTT_RE.match(raw_text):
        return _normalize_timed_text(raw_text)
    if mime == "application/json":
        return _normalize_json_transcript(raw_text)
    if mime == "text/html":
        return _HTMLText().parse(raw_text)
    return _collapse_blank_lines(raw_text)


def ensure_transcript(
    audio_path: Path,
    refs: tuple[TranscriptRef, ...],
    dest_dir: Path,
    provider: TranscriptProvider | None = None,
) -> TranscriptFile:
    for ref in refs:
        raw_path = fetch_transcript(ref, dest_dir)
        text_path = normalize_transcript(raw_path, ref.mime)
        if text_path.read_text(encoding="utf-8").strip():
            return TranscriptFile(source=ref, raw_path=raw_path, text_path=text_path)
    if provider is None:
        raise TranscriptUnavailable("no usable transcript references and no transcription provider configured")
    text_path = dest_dir / (audio_path.stem + ".generated.txt")
    return TranscriptFile(source=None, raw_path=None, text_path=provider.transcribe(audio_path, text_path))


def _normalize_timed_text(raw_text: str) -> str:
    lines: list[str] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or WEBVTT_RE.match(stripped) or TIMESTAMP_RE.match(stripped) or SRT_INDEX_RE.match(stripped):
            continue
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            continue
        cleaned = TAG_RE.sub("", stripped).strip()
        if cleaned:
            lines.append(cleaned)
    return _collapse_blank_lines("\n".join(lines))


def _normalize_json_transcript(raw_text: str) -> str:
    data = json.loads(raw_text)
    parts: list[str] = []
    _collect_text_values(data, parts)
    return _collapse_blank_lines("\n".join(parts))


def _collect_text_values(value: object, parts: list[str]) -> None:
    if isinstance(value, dict):
        for key in ("text", "body", "transcript"):
            text = value.get(key)
            if isinstance(text, str):
                parts.append(text)
                return
        for nested in value.values():
            _collect_text_values(nested, parts)
    elif isinstance(value, list):
        for nested in value:
            _collect_text_values(nested, parts)


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def parse(self, raw_text: str) -> str:
        self.feed(raw_text)
        return _collapse_blank_lines(" ".join(self.parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "nav", "footer"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "nav", "footer"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data.strip():
            self.parts.append(data.strip())


def _collapse_blank_lines(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    compact: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line
        if blank and previous_blank:
            continue
        compact.append(line)
        previous_blank = blank
    return "\n".join(compact).strip() + ("\n" if compact else "")


def _suffix_for_mime(mime: str) -> str:
    mime = mime.split(";", 1)[0].strip().lower()
    return {
        "text/vtt": ".vtt",
        "application/x-subrip": ".srt",
        "application/json": ".json",
        "text/plain": ".txt",
        "text/html": ".html",
    }.get(mime, ".txt")


def _safe_name(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", url).strip("._")[-160:] or "transcript"
