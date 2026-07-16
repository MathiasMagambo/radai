from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .models import PlaybackWindow, WindowKind


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class PodcastSegment:
    episode_id: int
    media_path: Path
    duration_sec: float
    offset_sec: float = 0.0


def build_time_window_schedule(
    segments: Sequence[PodcastSegment],
    *,
    podcast_window_sec: int = 1200,
    music_window_sec: int = 600,
    source_id: int | None = None,
) -> tuple[PlaybackWindow, ...]:
    if podcast_window_sec <= 0 or music_window_sec <= 0:
        raise ScheduleError("podcast and music window lengths must be positive")
    windows: list[PlaybackWindow] = []
    cursor = 0.0
    remaining_segments = list(segments)
    current_segment_index = 0
    while current_segment_index < len(remaining_segments):
        segment = remaining_segments[current_segment_index]
        if segment.duration_sec <= 0:
            current_segment_index += 1
            continue
        podcast_duration = min(float(podcast_window_sec), segment.duration_sec)
        windows.append(
            PlaybackWindow(
                kind=WindowKind.PODCAST,
                planned_start_sec=cursor,
                planned_end_sec=cursor + podcast_duration,
                episode_id=segment.episode_id,
                media_path=segment.media_path,
            )
        )
        cursor += podcast_duration
        remaining_duration = segment.duration_sec - podcast_duration
        if remaining_duration > 0:
            remaining_segments[current_segment_index] = PodcastSegment(
                episode_id=segment.episode_id,
                media_path=segment.media_path,
                duration_sec=remaining_duration,
                offset_sec=segment.offset_sec + podcast_duration,
            )
            windows.append(
                PlaybackWindow(
                    kind=WindowKind.MUSIC,
                    planned_start_sec=cursor,
                    planned_end_sec=cursor + float(music_window_sec),
                    source_id=source_id,
                )
            )
            cursor += float(music_window_sec)
        else:
            current_segment_index += 1
            if current_segment_index < len(remaining_segments):
                windows.append(
                    PlaybackWindow(
                        kind=WindowKind.MUSIC,
                        planned_start_sec=cursor,
                        planned_end_sec=cursor + float(music_window_sec),
                        source_id=source_id,
                    )
                )
                cursor += float(music_window_sec)
    return tuple(windows)


@dataclass
class SessionState:
    windows: tuple[PlaybackWindow, ...]
    active_index: int = 0

    def active_window(self) -> PlaybackWindow | None:
        if 0 <= self.active_index < len(self.windows):
            return self.windows[self.active_index]
        return None

    def advance(self) -> PlaybackWindow | None:
        self.active_index += 1
        return self.active_window()

    @property
    def finished(self) -> bool:
        return self.active_index >= len(self.windows)
