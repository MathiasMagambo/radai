from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class EpisodeStatus(StrEnum):
    NEW = "new"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    PLANNED = "planned"
    PLAYED = "played"
    ERROR = "error"


class TranscriptStatus(StrEnum):
    FOUND = "found"
    DOWNLOADED = "downloaded"
    NORMALIZED = "normalized"
    MISSING = "missing"
    ERROR = "error"


class WindowKind(StrEnum):
    PODCAST = "podcast"
    MUSIC = "music"


class SessionStatus(StrEnum):
    PLANNED = "planned"
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class RecordingStatus(StrEnum):
    NONE = "none"
    RECORDING = "recording"
    PENDING_DECISION = "pending_decision"
    KEPT = "kept"
    DISCARDED = "discarded"


@dataclass(frozen=True)
class PodcastFeed:
    url: str
    title: str = ""
    enabled: bool = True
    id: int | None = None
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class TranscriptRef:
    url: str
    mime: str
    source: str
    language: str | None = None
    rel: str | None = None


@dataclass(frozen=True)
class PodcastEpisode:
    feed_url: str
    guid: str
    title: str
    audio_url: str
    published_at: str | None = None
    description: str = ""
    audio_mime: str | None = None
    audio_bytes: int | None = None
    link: str | None = None
    transcripts: tuple[TranscriptRef, ...] = ()
    id: int | None = None
    feed_id: int | None = None
    local_audio_path: Path | None = None
    status: EpisodeStatus = EpisodeStatus.NEW

    @property
    def stable_key(self) -> str:
        return self.guid or self.audio_url or f"{self.title}|{self.published_at or ''}"


@dataclass(frozen=True)
class ParsedFeed:
    url: str
    title: str
    episodes: tuple[PodcastEpisode, ...]
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class CutRange:
    start_sec: float
    end_sec: float
    reason: str

    def __post_init__(self) -> None:
        if self.start_sec < 0 or self.end_sec <= self.start_sec:
            raise ValueError("cut range must have 0 <= start < end")


@dataclass(frozen=True)
class MusicInsertion:
    after_sec: float
    window_sec: float
    mood: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.after_sec < 0:
            raise ValueError("insertion time must be non-negative")
        if self.window_sec <= 0:
            raise ValueError("insertion window must be positive")


@dataclass(frozen=True)
class DeepSeekPlan:
    episode_id: str
    ad_cuts: tuple[CutRange, ...] = ()
    music_insertions: tuple[MusicInsertion, ...] = ()
    confidence: float = 0.0
    warnings: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class MusicSource:
    source: str
    name: str
    spotify_uri: str
    spotify_type: str
    enabled: bool = True
    id: int | None = None


@dataclass(frozen=True)
class PlaybackWindow:
    kind: WindowKind
    planned_start_sec: float
    planned_end_sec: float
    episode_id: int | None = None
    source_id: int | None = None
    media_path: Path | None = None

    @property
    def duration_sec(self) -> float:
        return self.planned_end_sec - self.planned_start_sec


@dataclass(frozen=True)
class StreamSession:
    id: int
    status: SessionStatus
    podcast_window_sec: int
    music_window_sec: int
    music_mode: str
    icecast_url: str
    recording_path: Path | None
    recording_status: RecordingStatus
